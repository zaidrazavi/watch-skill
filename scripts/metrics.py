#!/usr/bin/env python3
"""Cut / pacing metrics for /watch, derived from scene-cut timestamps.

Ad research lives and dies on pacing: how many cuts are crammed into the hook,
how fast the whole ad moves, the length of the average shot. The scene engine in
frames.py already detects every cut; this module just turns that list of
timestamps into numbers (and is a no-op when no cut data exists, e.g. the uniform
fallback on a static screen recording).
"""
from __future__ import annotations

import json
import sys


def compute_pacing(
    candidate_timestamps: list[float] | None,
    duration: float,
    engine: str = "scene",
    hook_seconds: float = 3.0,
    hook_start: float = 0.0,
) -> dict:
    """Return pacing metrics from a sorted list of shot-start timestamps.

    ``candidate_timestamps`` is every detected shot boundary (first frame + each
    scene change, or keyframes for the efficient engine). The first entry is the
    opening frame, not a cut, so cuts = boundaries - 1.
    """
    result = {
        "available": False,
        "engine": engine,
        "cuts": None,
        "shots": None,
        "cuts_per_second": None,
        "avg_shot_seconds": None,
        "hook_cuts": None,
        "fastest_cut_gap_s": None,
        "pace_label": None,
    }
    if not candidate_timestamps or engine not in ("scene", "keyframe"):
        return result

    ts = sorted(t for t in candidate_timestamps if t is not None)
    if not ts:
        return result

    shots = len(ts)
    cuts = max(0, shots - 1)
    hook_end = hook_start + hook_seconds
    hook_cuts = sum(1 for t in ts if hook_start < t <= hook_end + 1e-6)

    gaps = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    fastest_gap = round(min(gaps), 2) if gaps else None

    cps = round(cuts / duration, 2) if duration and duration > 0 else None
    avg_shot = round(duration / shots, 2) if duration and duration > 0 and shots else None

    pace_label = None
    if cps is not None:
        if cps >= 1.0:
            pace_label = "very fast (frenetic cutting)"
        elif cps >= 0.5:
            pace_label = "fast"
        elif cps >= 0.2:
            pace_label = "moderate"
        else:
            pace_label = "slow / static"

    result.update({
        "available": True,
        "cuts": cuts,
        "shots": shots,
        "cuts_per_second": cps,
        "avg_shot_seconds": avg_shot,
        "hook_cuts": hook_cuts,
        "fastest_cut_gap_s": fastest_gap,
        "pace_label": pace_label,
    })
    return result


def format_pacing_report(p: dict) -> str:
    """Render pacing metrics as a markdown block."""
    if not p or not p.get("available"):
        return "_Pacing metrics unavailable (no scene-cut data at this detail mode)._"
    lines = [
        f"- **Cuts:** {p['cuts']} across {p['shots']} shots "
        f"({p['cuts_per_second']}/sec — {p['pace_label']})",
        f"- **Average shot length:** {p['avg_shot_seconds']}s",
        f"- **Cuts in hook (0:00–0:03):** {p['hook_cuts']}",
    ]
    if p.get("fastest_cut_gap_s") is not None:
        lines.append(f"- **Fastest cut:** {p['fastest_cut_gap_s']}s between shots")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: metrics.py <duration> <ts1,ts2,...>", file=sys.stderr)
        raise SystemExit(2)
    duration = float(sys.argv[1])
    timestamps = [float(t) for t in sys.argv[2].split(",") if t.strip()]
    print(json.dumps(compute_pacing(timestamps, duration), indent=2))
