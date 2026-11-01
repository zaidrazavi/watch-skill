#!/usr/bin/env python3
"""Transcribe a video/audio file fully locally with faster-whisper.

100% offline, zero API key, zero cost. Extracts mono 16kHz audio via ffmpeg,
then runs a CTranslate2-based Whisper model (faster-whisper, MIT-licensed) on
CPU or GPU. Returns segments in the same {start, end, text} shape as
transcribe.parse_vtt so the rest of the pipeline (filter_range,
format_transcript) doesn't care where the transcript came from — and adds a
per-segment `words` list (word-level timestamps) for precise ad-hook analysis.

The model is downloaded and cached once via faster-whisper's built-in Hugging
Face cache (~/.cache/huggingface); every run after the first is fully offline.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


VALID_MODELS = ("tiny", "base", "small", "medium", "large-v3")
DEFAULT_MODEL_CPU = "medium"
DEFAULT_MODEL_GPU = "large-v3"

# Approximate on-disk model sizes, only used for the one-time download notice.
_MODEL_SIZE_HINT = {
    "tiny": "~75MB",
    "base": "~145MB",
    "small": "~490MB",
    "medium": "~1.5GB",
    "large-v3": "~3GB",
}

_DOTENV_PATHS = [
    Path.home() / ".config" / "watch" / ".env",
    Path.cwd() / ".env",
]


def _read_dotenv_value(name: str) -> str | None:
    """Read a bare KEY=value from the environment, then the watch .env files."""
    env_value = os.environ.get(name)
    if env_value and env_value.strip():
        return env_value.strip()

    for path in _DOTENV_PATHS:
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() != name:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                    value = value[1:-1]
                else:
                    # Strip a trailing ` # comment` from an unquoted value.
                    for i, ch in enumerate(value):
                        if ch == "#" and i > 0 and value[i - 1] in " \t":
                            value = value[:i].rstrip()
                            break
                return value or None
        except OSError:
            continue
    return None


def detect_device() -> tuple[str, str]:
    """Return (device, compute_type). CUDA GPU if present, else quantized CPU."""
    try:
        import ctranslate2  # bundled with faster-whisper

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def resolve_model(override: str | None = None, device: str | None = None) -> str:
    """Pick the model size.

    Priority: explicit override → WHISPER_MODEL in env/.env → hardware default
    (large-v3 on GPU, medium on CPU). Invalid values fall back to the default.
    """
    if device is None:
        device, _ = detect_device()
    default = DEFAULT_MODEL_GPU if device == "cuda" else DEFAULT_MODEL_CPU

    candidate = override or _read_dotenv_value("WHISPER_MODEL") or default
    candidate = candidate.strip()
    if candidate not in VALID_MODELS:
        print(
            f"[watch] WHISPER_MODEL='{candidate}' is not one of "
            f"{'|'.join(VALID_MODELS)} — falling back to '{default}'",
            file=sys.stderr,
        )
        candidate = default
    return candidate


def extract_audio(video_path: str, out_path: Path) -> Path:
    """Extract mono 16kHz 64kbps mp3 — the exact params the old pipeline used."""
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(Path(video_path).resolve()),
        "-vn",
        "-acodec", "libmp3lame",
        "-ar", "16000",
        "-ac", "1",
        "-b:a", "64k",
        str(out_path.resolve()),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg audio extraction failed: {result.stderr.strip()}")
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise SystemExit("ffmpeg produced no audio — video may have no audio track")
    return out_path


def _model_is_cached(model_size: str) -> bool:
    """Best-effort check for whether faster-whisper already has this model.

    faster-whisper pulls `Systran/faster-whisper-<size>` into the HF hub cache,
    which stores it as a `models--Systran--faster-whisper-<size>` directory.
    Only used to decide whether to print the one-time download notice.
    """
    cache_root = os.environ.get("HF_HOME")
    hub_dirs = []
    if cache_root:
        hub_dirs.append(Path(cache_root) / "hub")
    hub_dirs.append(Path.home() / ".cache" / "huggingface" / "hub")

    repo_dir = f"models--Systran--faster-whisper-{model_size}"
    for hub in hub_dirs:
        if (hub / repo_dir).exists():
            return True
    return False


def transcribe_video(
    video_path: str,
    audio_out: Path,
    model_size: str | None = None,
) -> tuple[list[dict], str]:
    """Run the full flow: extract audio → local Whisper → parse segments.

    Returns (segments, backend_label). Each segment is
    {start, end, text, words} where `words` is a list of
    {word, start, end} for word-level timing. Raises SystemExit on failure.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        setup_py = Path(__file__).resolve().parent / "setup.py"
        raise SystemExit(
            "faster-whisper is not installed. Install it with "
            "`pip install faster-whisper`, or run "
            f"`python3 {setup_py}` to set up /watch."
        )

    device, compute_type = detect_device()
    model_size = resolve_model(model_size, device=device)

    print(f"[watch] extracting audio for local transcription…", file=sys.stderr)
    audio_path = extract_audio(video_path, audio_out)
    audio_bytes = audio_path.stat().st_size

    if not _model_is_cached(model_size):
        size_hint = _MODEL_SIZE_HINT.get(model_size, "")
        print(
            f"[watch] downloading {model_size} model ({size_hint}), "
            "one-time only — cached for all future runs…",
            file=sys.stderr,
        )

    def _run(dev: str, ctype: str) -> tuple[list[dict], object]:
        """Load the model on `dev` and fully materialize the transcript.

        Materializing inside this function (not lazily) means CUDA runtime
        failures surface here, where the caller can fall back to CPU.
        """
        print(
            f"[watch] audio: {audio_bytes / 1024:.0f} kB — transcribing locally with "
            f"faster-whisper '{model_size}' ({dev}/{ctype})…",
            file=sys.stderr,
        )
        model = WhisperModel(model_size, device=dev, compute_type=ctype)
        segments_iter, run_info = model.transcribe(
            str(audio_path.resolve()),
            vad_filter=True,        # skip silence: faster + no hallucinated text in gaps
            word_timestamps=True,   # word-level timing for precise ad-hook analysis
            # language auto-detected (not hardcoded to English)
        )
        collected: list[dict] = []
        for seg in segments_iter:
            text = (seg.text or "").strip()
            if not text:
                continue
            words: list[dict] = []
            for w in (seg.words or []):
                word_text = (w.word or "").strip()
                if not word_text:
                    continue
                words.append({
                    "word": word_text,
                    "start": round(float(w.start), 3),
                    "end": round(float(w.end), 3),
                })
            collected.append({
                "start": round(float(seg.start), 2),
                "end": round(float(seg.end), 2),
                "text": text,
                "words": words,
            })
        return collected, run_info

    try:
        segments, info = _run(device, compute_type)
    except Exception as exc:
        # A CUDA device can be *visible* without the CUDA/cuDNN runtime libs
        # actually being loadable. Rather than fail, fall back to CPU so every
        # machine on the team works out of the box.
        if device == "cuda":
            print(
                f"[watch] CUDA transcription failed ({type(exc).__name__}: {exc}) — "
                "falling back to CPU (int8)…",
                file=sys.stderr,
            )
            device, compute_type = "cpu", "int8"
            try:
                segments, info = _run(device, compute_type)
            except Exception as cpu_exc:
                raise SystemExit(f"local transcription failed: {cpu_exc}")
        else:
            raise SystemExit(f"local transcription failed: {exc}")

    if not segments:
        raise SystemExit("faster-whisper returned no transcript segments")

    lang = getattr(info, "language", None)
    lang_note = f", language={lang}" if lang else ""
    print(
        f"[watch] transcribed {len(segments)} segments via faster-whisper "
        f"'{model_size}'{lang_note}",
        file=sys.stderr,
    )
    backend_label = f"faster-whisper {model_size} ({device})"
    return segments, backend_label


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "usage: local_whisper.py <video-or-audio-path> [<audio-out.mp3>] "
            "[--model tiny|base|small|medium|large-v3]",
            file=sys.stderr,
        )
        raise SystemExit(2)

    video = sys.argv[1]
    audio_out = (
        Path(sys.argv[2])
        if len(sys.argv) > 2 and not sys.argv[2].startswith("--")
        else Path("audio.mp3")
    )
    model_override = None
    if "--model" in sys.argv:
        model_override = sys.argv[sys.argv.index("--model") + 1]

    segs, backend = transcribe_video(video, audio_out, model_size=model_override)
    print(json.dumps({"backend": backend, "segments": segs}, indent=2))
