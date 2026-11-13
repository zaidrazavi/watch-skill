#!/usr/bin/env python3
"""Speech / music / singing / silence timeline for /watch.

Labels every second of the clip, producing a segmented timeline like:

    [00:00–00:04] music
    [00:04–00:019] speech
    [00:19–00:23] singing

It works at two fidelities:

- **With stems** (from stems.py): it compares the per-second energy of the
  isolated *vocals* stem and *music* stem, and cross-references the speech
  transcript, so it can tell speech (a voice with recognized words) from singing
  (vocal energy over a music bed that the speech pass didn't transcribe) from
  pure music (backing track, no vocal) from silence.
- **Without stems:** it falls back to a coarser speech / music-or-sfx / silence
  split from the mix energy plus transcript word coverage.

Heuristic, ffmpeg-only (no model beyond the optional Demucs that produced the
stems), and fail-open: a missing curve just yields a coarser or empty timeline.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from audio import _loudness_curve, _spoken_intervals, _overlap  # noqa: E402


def format_time_label(seconds: float) -> str:
    total = int(round(seconds))
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# dBFS floors (RMS). Below the silence floor a window is treated as silent; a
# stem above its presence floor is treated as active in that window.
SILENCE_FLOOR = -50.0
VOCAL_FLOOR = -45.0
MUSIC_FLOOR = -45.0


def _curve_dict(curve: list[list[float]]) -> dict[int, float]:
    """Map second-index → rms_dbfs, skipping None entries."""
    out: dict[int, float] = {}
    for t, rms in curve:
        if rms is not None:
            out[int(round(t))] = rms
    return out


def build_timeline(
    mix_path: str,
    duration: float,
    transcript_segments: list[dict] | None = None,
    vocals_path: str | None = None,
    music_path: str | None = None,
    window_s: float = 1.0,
) -> dict:
    """Return a segmented speech/music/singing/silence timeline.

    ``{available, stems_used, segments: [{start,end,label}], summary: {label: seconds}}``
    """
    result = {"available": False, "stems_used": False, "segments": [], "summary": {}}
    if not mix_path or not Path(mix_path).exists() or not duration or duration <= 0:
        return result

    mix = _curve_dict(_loudness_curve(mix_path, window_s=window_s))
    if not mix:
        return result

    stems_used = bool(vocals_path and music_path
                      and Path(vocals_path).exists() and Path(music_path).exists())
    voc = _curve_dict(_loudness_curve(vocals_path, window_s=window_s)) if stems_used else {}
    mus = _curve_dict(_loudness_curve(music_path, window_s=window_s)) if stems_used else {}
    spoken = _spoken_intervals(transcript_segments)

    n = int(round(duration))
    labels: list[str] = []
    for t in range(max(1, n)):
        mix_rms = mix.get(t)
        has_word = _overlap(spoken, t, t + window_s) > 0.15 * window_s

        if mix_rms is None or mix_rms < SILENCE_FLOOR:
            labels.append("silence")
            continue

        if stems_used:
            v = voc.get(t, -120.0)
            m = mus.get(t, -120.0)
            vocals_present = v > VOCAL_FLOOR
            music_present = m > MUSIC_FLOOR
            if not vocals_present and not music_present:
                labels.append("sfx")
            elif vocals_present and has_word:
                labels.append("speech")
            elif vocals_present and music_present:
                labels.append("singing")
            elif vocals_present:
                labels.append("speech")  # a voice, even if the pass found no words
            elif music_present:
                labels.append("music")
            else:
                labels.append("sfx")
        else:
            if has_word:
                labels.append("speech")
            else:
                labels.append("music/sfx")

    # Merge consecutive same-label windows into segments.
    segments: list[dict] = []
    for i, lab in enumerate(labels):
        start = i * window_s
        end = min(duration, (i + 1) * window_s)
        if segments and segments[-1]["label"] == lab:
            segments[-1]["end"] = round(end, 2)
        else:
            segments.append({"start": round(start, 2), "end": round(end, 2), "label": lab})

    summary: dict[str, float] = {}
    for seg in segments:
        summary[seg["label"]] = round(summary.get(seg["label"], 0.0) + (seg["end"] - seg["start"]), 2)

    result.update({
        "available": True,
        "stems_used": stems_used,
        "segments": segments,
        "summary": summary,
    })
    return result


def format_timeline(tl: dict, max_segments: int = 40) -> str:
    """Render the timeline as a markdown block."""
    if not tl or not tl.get("available") or not tl.get("segments"):
        return "_Timeline unavailable._"
    lines: list[str] = []
    total = sum(tl["summary"].values()) or 1.0
    mix = ", ".join(
        f"{lab} {secs / total * 100:.0f}%"
        for lab, secs in sorted(tl["summary"].items(), key=lambda kv: -kv[1])
    )
    src = "vocals/music stems" if tl.get("stems_used") else "mix energy + transcript"
    lines.append(f"- **Breakdown ({src}):** {mix}")
    segs = tl["segments"]
    shown = segs if len(segs) <= max_segments else segs[:max_segments]
    lines.append("")
    lines.append("```")
    for seg in shown:
        lines.append(f"[{format_time_label(seg['start'])}–{format_time_label(seg['end'])}] {seg['label']}")
    if len(segs) > max_segments:
        lines.append(f"… {len(segs) - max_segments} more segments")
    lines.append("```")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: timeline.py <mix-audio> <duration> [vocals] [music]", file=sys.stderr)
        raise SystemExit(2)
    mix = sys.argv[1]
    dur = float(sys.argv[2])
    vocals = sys.argv[3] if len(sys.argv) > 3 else None
    music = sys.argv[4] if len(sys.argv) > 4 else None
    print(json.dumps(build_timeline(mix, dur, vocals_path=vocals, music_path=music), indent=2))
