#!/usr/bin/env python3
"""Audio intelligence for /watch — loudness, silence, and a speech/music read.

Everything here runs through ffmpeg filters only (ebur128, volumedetect,
silencedetect), so there is no extra dependency beyond the ffmpeg /watch already
requires — and nothing leaves the machine. The whole module is fail-open: any
ffmpeg error or parse miss returns a partial/empty result with ``available`` set
accordingly, so audio analysis never breaks the main frames+transcript pipeline.

The speech/music/silence split is a heuristic that *combines* the audio envelope
with the transcript: a window that is both audible and covered by spoken words is
"speech"; audible but wordless is "music/sfx"; quiet is "silence". That keeps it
honest (no claim of true speaker diarization, which would need a heavy gated
model) while still answering the question ad research actually cares about — is
the hook carried by a voice, by music, or by a beat drop.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path


HOOK_SECONDS = 3.0

# astats RMS envelope is printed by ametadata as paired lines:
#   [Parsed_ametadata_3 @ 0x..] frame:2  pts:32000  pts_time:2
#   [Parsed_ametadata_3 @ 0x..] lavfi.astats.Overall.RMS_level=-19.41
_PTS_TIME_RE = re.compile(r"pts_time:([0-9.]+)")
_RMS_LEVEL_RE = re.compile(r"astats\.Overall\.RMS_level=(-?[0-9.]+|-?inf|nan)")
_MEAN_VOL_RE = re.compile(r"mean_volume:\s*(-?[0-9.]+)\s*dB")
_MAX_VOL_RE = re.compile(r"max_volume:\s*(-?[0-9.]+)\s*dB")
# silencedetect emits a start line then an end line (with duration).
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _run_ffmpeg_filter(audio_path: str, af: str, extra_in: list[str] | None = None) -> str:
    """Run `ffmpeg -i audio -af <af> -f null -` and return combined stderr.

    ffmpeg writes filter metadata (ebur128/volumedetect/silencedetect) to stderr.
    Returns "" on any failure so every caller degrades gracefully.
    """
    if shutil.which("ffmpeg") is None:
        return ""
    cmd = ["ffmpeg", "-hide_banner", "-nostats"]
    if extra_in:
        cmd += extra_in
    cmd += ["-i", str(Path(audio_path).resolve()), "-af", af, "-f", "null", "-"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return ""
    return result.stderr or ""


def _volumedetect(audio_path: str, start: float | None = None, end: float | None = None) -> dict:
    extra: list[str] = []
    if start is not None:
        extra += ["-ss", f"{start:.3f}"]
    if end is not None:
        extra += ["-to", f"{end:.3f}"]
    err = _run_ffmpeg_filter(audio_path, "volumedetect", extra_in=extra or None)
    mean = _MEAN_VOL_RE.search(err)
    peak = _MAX_VOL_RE.search(err)
    return {
        "mean_volume_db": _finite(mean.group(1)) if mean else None,
        "max_volume_db": _finite(peak.group(1)) if peak else None,
    }


def _loudnorm_integrated(audio_path: str) -> dict:
    """Integrated loudness (LUFS) + true peak via a single loudnorm analysis pass."""
    err = _run_ffmpeg_filter(audio_path, "loudnorm=print_format=json")
    # The JSON block is the last {...} ffmpeg prints to stderr.
    brace = err.rfind("{")
    if brace == -1:
        return {"integrated_lufs": None, "true_peak_db": None}
    try:
        data = json.loads(err[brace:err.rfind("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return {"integrated_lufs": None, "true_peak_db": None}
    return {
        "integrated_lufs": _finite(data.get("input_i")),
        "true_peak_db": _finite(data.get("input_tp")),
    }


def _loudness_curve(audio_path: str, window_s: float = 1.0) -> list[list[float]]:
    """Per-window RMS envelope [[t, rms_dbfs], ...] from one astats pass.

    Resamples to 16 kHz and resets stats every ``window_s`` seconds so each
    emitted frame is one window with a ``pts_time`` and an ``RMS_level`` line.
    More portable than ebur128's continuous readout, which several ffmpeg builds
    do not print to stderr.
    """
    n = max(1, int(round(16000 * window_s)))
    af = (
        f"aresample=16000,asetnsamples=n={n}:p=0,"
        "astats=metadata=1:reset=1,"
        "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level"
    )
    err = _run_ffmpeg_filter(audio_path, af)
    out: list[list[float]] = []
    pending_t: float | None = None
    for line in err.splitlines():
        tm = _PTS_TIME_RE.search(line)
        if tm:
            pending_t = _finite(tm.group(1))
            continue
        rm = _RMS_LEVEL_RE.search(line)
        if rm and pending_t is not None:
            rms = _finite(rm.group(1))
            out.append([round(pending_t, 2), round(rms, 1) if rms is not None else None])
            pending_t = None
    return out


def _silences(audio_path: str, threshold_db: float = -30.0, min_dur: float = 0.3) -> list[list[float]]:
    af = f"silencedetect=noise={threshold_db}dB:d={min_dur}"
    err = _run_ffmpeg_filter(audio_path, af)
    segments: list[list[float]] = []
    pending_start: float | None = None
    for line in err.splitlines():
        s = _SILENCE_START_RE.search(line)
        if s:
            pending_start = _finite(s.group(1))
            continue
        e = _SILENCE_END_RE.search(line)
        if e:
            end = _finite(e.group(1))
            start = pending_start if pending_start is not None else (
                end - _finite(e.group(2)) if end is not None else None
            )
            if start is not None and end is not None and end > start:
                segments.append([round(max(0.0, start), 2), round(end, 2)])
            pending_start = None
    return segments


def _spoken_intervals(transcript_segments: list[dict] | None) -> list[tuple[float, float]]:
    """Merge transcript word/segment times into non-overlapping spoken spans."""
    if not transcript_segments:
        return []
    spans: list[tuple[float, float]] = []
    for seg in transcript_segments:
        words = seg.get("words") or []
        if words:
            for w in words:
                st, en = _finite(w.get("start")), _finite(w.get("end"))
                if st is not None and en is not None and en > st:
                    spans.append((st, en))
        else:
            st, en = _finite(seg.get("start")), _finite(seg.get("end"))
            if st is not None and en is not None and en > st:
                spans.append((st, en))
    if not spans:
        return []
    spans.sort()
    merged = [spans[0]]
    for st, en in spans[1:]:
        if st <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], en))
        else:
            merged.append((st, en))
    return merged


def _overlap(spans: list[tuple[float, float]], lo: float, hi: float) -> float:
    """Total overlap (seconds) of [lo, hi] with a list of non-overlapping spans."""
    total = 0.0
    for st, en in spans:
        total += max(0.0, min(en, hi) - max(st, lo))
    return total


def analyze_audio(
    audio_path: str,
    duration: float,
    transcript_segments: list[dict] | None = None,
    hook_seconds: float = HOOK_SECONDS,
    hook_start: float = 0.0,
) -> dict:
    """Full audio read: loudness, silence, and a speech/music/silence content mix.

    ``audio_path`` should be a decodable audio or video file (the mono 16 kHz clip
    /watch extracts for whisper is ideal and cheap). Returns a dict that is always
    JSON-serializable and never raises.
    """
    result: dict = {
        "available": False,
        "duration": round(duration, 2) if duration else None,
        "integrated_lufs": None,
        "true_peak_db": None,
        "mean_volume_db": None,
        "max_volume_db": None,
        "silence": {"total_silence_s": 0.0, "audible_ratio": None, "segments": []},
        "loudness_curve": [],
        "content_mix": {"speech_ratio": None, "music_or_sfx_ratio": None, "silence_ratio": None},
        "hook": {},
    }
    if not audio_path or not Path(audio_path).exists() or shutil.which("ffmpeg") is None:
        return result

    vd = _volumedetect(audio_path)
    result["mean_volume_db"] = vd["mean_volume_db"]
    result["max_volume_db"] = vd["max_volume_db"]
    result.update(_loudnorm_integrated(audio_path))
    result["loudness_curve"] = _loudness_curve(audio_path)

    silences = _silences(audio_path)
    total_silence = sum(e - s for s, e in silences)
    result["silence"]["segments"] = silences
    result["silence"]["total_silence_s"] = round(total_silence, 2)
    if duration and duration > 0:
        result["silence"]["audible_ratio"] = round(max(0.0, 1 - total_silence / duration), 3)

    spoken = _spoken_intervals(transcript_segments)

    # Content mix over the whole clip: speech = audible AND spoken; music/sfx =
    # audible but no words; silence = below the silence threshold.
    if duration and duration > 0:
        speech_s = _overlap(spoken, 0.0, duration)
        audible_s = max(0.0, duration - total_silence)
        music_s = max(0.0, audible_s - speech_s)
        result["content_mix"] = {
            "speech_ratio": round(speech_s / duration, 3),
            "music_or_sfx_ratio": round(music_s / duration, 3),
            "silence_ratio": round(total_silence / duration, 3),
        }

    # Hook window read — the part ad research cares about most.
    h_lo = hook_start
    h_hi = hook_start + hook_seconds
    hook_vd = _volumedetect(audio_path, start=h_lo, end=h_hi)
    hook_curve = [p for p in result["loudness_curve"] if p[1] is not None and h_lo <= p[0] <= h_hi]
    hook_mean_rms = round(sum(p[1] for p in hook_curve) / len(hook_curve), 1) if hook_curve else None
    hook_peak_rms = round(max(p[1] for p in hook_curve), 1) if hook_curve else None
    hook_speech = _overlap(spoken, h_lo, h_hi)
    hook_speech_cov = round(hook_speech / hook_seconds, 3) if hook_seconds > 0 else None
    hook_silence = sum(max(0.0, min(e, h_hi) - max(s, h_lo)) for s, e in silences)
    hook_silence_ratio = hook_silence / hook_seconds if hook_seconds > 0 else 0.0

    if hook_silence_ratio > 0.6:
        content = "silent / near-silent"
    elif hook_speech_cov is not None and hook_speech_cov >= 0.4:
        content = "speech-led" if (1 - hook_silence_ratio - (hook_speech_cov or 0)) < 0.25 else "speech over music"
    elif hook_speech_cov is not None and hook_speech_cov > 0.05:
        content = "mixed (some speech)"
    else:
        content = "music / sfx (no speech)"

    loud_vs_rest = None
    if hook_vd["mean_volume_db"] is not None and result["mean_volume_db"] is not None:
        loud_vs_rest = round(hook_vd["mean_volume_db"] - result["mean_volume_db"], 1)

    result["hook"] = {
        "window": [round(h_lo, 2), round(h_hi, 2)],
        "mean_volume_db": hook_vd["mean_volume_db"],
        "max_volume_db": hook_vd["max_volume_db"],
        "mean_rms_db": hook_mean_rms,
        "peak_rms_db": hook_peak_rms,
        "louder_than_rest_db": loud_vs_rest,
        "speech_coverage": hook_speech_cov,
        "content": content,
    }
    result["available"] = True
    return result


def format_audio_report(a: dict) -> str:
    """Render the audio dict as a markdown block for the /watch report."""
    if not a or not a.get("available"):
        return "_Audio analysis unavailable._"
    lines: list[str] = []
    hook = a.get("hook") or {}
    if hook:
        lines.append(f"- **Hook audio:** {hook.get('content', 'unknown')}")
        if hook.get("speech_coverage") is not None:
            lines.append(f"  - Speech coverage in hook: {hook['speech_coverage'] * 100:.0f}%")
        if hook.get("louder_than_rest_db") is not None:
            sign = "+" if hook["louder_than_rest_db"] >= 0 else ""
            lines.append(f"  - Hook loudness vs rest: {sign}{hook['louder_than_rest_db']} dB")
        if hook.get("mean_rms_db") is not None:
            lines.append(f"  - Hook RMS loudness: avg {hook['mean_rms_db']} dBFS, peak {hook.get('peak_rms_db')} dBFS")
    mix = a.get("content_mix") or {}
    if mix.get("speech_ratio") is not None:
        lines.append(
            f"- **Content mix (whole clip):** "
            f"{mix['speech_ratio'] * 100:.0f}% speech, "
            f"{mix['music_or_sfx_ratio'] * 100:.0f}% music/sfx, "
            f"{mix['silence_ratio'] * 100:.0f}% silence"
        )
    if a.get("integrated_lufs") is not None:
        lines.append(f"- **Integrated loudness:** {a['integrated_lufs']} LUFS (true peak {a.get('true_peak_db')} dBTP)")
    elif a.get("mean_volume_db") is not None:
        lines.append(f"- **Volume:** mean {a['mean_volume_db']} dB, peak {a.get('max_volume_db')} dB")
    sil = a.get("silence") or {}
    if sil.get("segments"):
        lines.append(f"- **Silence:** {sil['total_silence_s']}s total across {len(sil['segments'])} gap(s)")
    return "\n".join(lines) if lines else "_No audio metrics extracted._"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: audio.py <audio-or-video-path> [duration_seconds]", file=sys.stderr)
        raise SystemExit(2)
    path = sys.argv[1]
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    if not dur:
        try:
            from frames import get_metadata
            dur = get_metadata(path)["duration_seconds"]
        except Exception:
            dur = 0.0
    print(json.dumps(analyze_audio(path, dur), indent=2))
