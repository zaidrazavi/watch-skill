#!/usr/bin/env python3
"""Local audio source separation (stems) for /watch via Demucs.

Splits an audio track into an isolated **vocals** stem and a **music** (no-vocals)
stem, fully locally with Demucs (htdemucs, MIT-licensed, open weights). This
powers two things ad research actually wants:

- a clean vocal track to transcribe (sung lyrics or voice buried under a loud bed
  come through far better than from the raw mix), and
- a speech/music/singing timeline (which second is carried by a voice vs the
  backing track), by comparing the energy of the two stems.

Demucs needs PyTorch, so it is the one heavy optional dependency in /watch. The
whole module is fail-open: if Demucs (or torch) is not installed, or separation
errors out, it returns ``None`` and the pipeline proceeds on the original mix.
Nothing leaves the machine.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_MODEL = "htdemucs"


def stems_available() -> bool:
    """True when Demucs (and therefore torch) can be imported."""
    try:
        import demucs  # noqa: F401
        return True
    except Exception:
        return False


def _detect_device() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _find_outputs(out_root: Path, model: str, track_stem: str) -> dict | None:
    """Locate Demucs' two-stem output for a track.

    Demucs writes `<out_root>/<model>/<track_stem>/{vocals,no_vocals}.<ext>`.
    """
    track_dir = out_root / model / track_stem
    if not track_dir.exists():
        # Fall back to any single subdirectory under the model dir.
        model_dir = out_root / model
        if model_dir.exists():
            subdirs = [p for p in model_dir.iterdir() if p.is_dir()]
            if len(subdirs) == 1:
                track_dir = subdirs[0]
    vocals = next(iter(track_dir.glob("vocals.*")), None) if track_dir.exists() else None
    music = next(iter(track_dir.glob("no_vocals.*")), None) if track_dir.exists() else None
    if vocals and music and vocals.exists() and music.exists():
        return {"vocals": str(vocals), "music": str(music)}
    return None


def separate(
    audio_path: str,
    out_dir: Path,
    model: str = DEFAULT_MODEL,
    device: str | None = None,
    as_mp3: bool = True,
) -> dict | None:
    """Separate ``audio_path`` into vocals + music (no_vocals) stems.

    Returns ``{"vocals": path, "music": path, "device": dev, "model": model}`` or
    ``None`` on any failure (engine missing, separation error, outputs not found).
    Tries the detected device first, then falls back to CPU.
    """
    if not stems_available():
        return None
    src = Path(audio_path)
    if not src.exists():
        return None
    out_dir.mkdir(parents=True, exist_ok=True)

    devices = [device] if device else [_detect_device()]
    if "cpu" not in devices:
        devices.append("cpu")  # fall back to CPU if GPU separation fails

    track_stem = src.stem
    for dev in devices:
        cmd = [
            sys.executable, "-m", "demucs",
            "-n", model,
            "--two-stems", "vocals",
            "-d", dev,
            "-o", str(out_dir.resolve()),
        ]
        if as_mp3:
            cmd += ["--mp3"]
        cmd += ["--", str(src.resolve())]
        print(f"[watch] separating stems with Demucs '{model}' ({dev})… "
              "(first run downloads the model, one-time)", file=sys.stderr)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as exc:
            print(f"[watch] demucs could not start on {dev}: {exc}", file=sys.stderr)
            continue
        if result.returncode != 0:
            tail = (result.stderr or "").strip().splitlines()[-1:] or [""]
            print(f"[watch] demucs failed on {dev}: {tail[0]}", file=sys.stderr)
            continue
        found = _find_outputs(out_dir, model, track_stem)
        if found:
            found.update({"device": dev, "model": model})
            print(f"[watch] stems ready: vocals + music ({dev})", file=sys.stderr)
            return found
        print(f"[watch] demucs produced no stem files on {dev}", file=sys.stderr)
    return None


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: stems.py <audio-path> <out-dir> [--model htdemucs] [--device cpu|cuda]",
              file=sys.stderr)
        raise SystemExit(2)
    audio = sys.argv[1]
    out = Path(sys.argv[2])
    model = DEFAULT_MODEL
    device = None
    args = sys.argv[3:]
    if "--model" in args:
        model = args[args.index("--model") + 1]
    if "--device" in args:
        device = args[args.index("--device") + 1]
    if not stems_available():
        print(json.dumps({"available": False, "hint": "pip install demucs"}, indent=2))
        raise SystemExit(0)
    res = separate(audio, out, model=model, device=device)
    print(json.dumps(res or {"available": True, "result": None}, indent=2))
