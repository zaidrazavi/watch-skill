#!/usr/bin/env python3
"""Precise scene/cut detection for /watch via PySceneDetect.

The frame-extraction scene filter in frames.py is tuned to pick *frames to show
Claude*, so it only catches hard cuts and falls back to uniform sampling on
gradual footage (crossfades, slideshows, slow pans) — which makes pacing coarse.
This module runs [PySceneDetect](https://www.scenedetect.com) separately, purely
for metrics, to get an **exact** list of cuts and shot boundaries, including the
gradual transitions the ffmpeg filter misses (AdaptiveDetector uses a rolling
average, so dissolves and fades register).

Local and free (OpenCV decode, no network). Fail-open: if PySceneDetect/OpenCV
isn't installed, or detection errors, it returns ``available: False`` and pacing
falls back to the frame engine's cut data.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def scenes_available() -> bool:
    try:
        import scenedetect  # noqa: F401
        import cv2  # noqa: F401
        return True
    except Exception:
        return False


def detect_scenes(
    video_path: str,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    min_scene_len_s: float = 0.3,
) -> dict:
    """Return exact cut timestamps + shot list for ``video_path``.

    ``{available, engine, scene_count, cuts: [t,…], shots: [{start,end,dur},…],
       shot_start_times: [t,…]}``. ``cuts`` are the boundary times (one per shot
       after the first); ``shot_start_times`` includes the opening 0.0 so pacing
       can treat them as candidate timestamps directly.
    """
    result = {"available": False, "engine": None, "scene_count": 0,
              "cuts": [], "shots": [], "shot_start_times": []}
    if not scenes_available() or not video_path or not Path(video_path).exists():
        return result
    try:
        from scenedetect import open_video, SceneManager, AdaptiveDetector
    except Exception:
        return result
    try:
        video = open_video(str(Path(video_path).resolve()))
        fps = float(video.frame_rate or 0) or 30.0
        min_len_frames = max(1, int(round(min_scene_len_s * fps)))
        sm = SceneManager()
        sm.auto_downscale = True  # keep it fast on big frames
        # AdaptiveDetector defaults (adaptive_threshold 3.0, min_content_val 15)
        # catch hard cuts AND gradual transitions via a rolling average; only the
        # minimum shot length is tightened so sub-0.3s flickers don't over-count.
        sm.add_detector(AdaptiveDetector(min_scene_len=min_len_frames))
        sm.detect_scenes(video, show_progress=False)
        scene_list = sm.get_scene_list()
    except Exception as exc:  # fail-open on any decode/detector error
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    lo = start_seconds if start_seconds is not None else 0.0
    hi = end_seconds if end_seconds is not None else float("inf")

    shots: list[dict] = []
    for s, e in scene_list:
        st, en = s.seconds, e.seconds
        if en <= lo or st >= hi:
            continue
        st = max(st, lo)
        en = min(en, hi)
        if en - st <= 0:
            continue
        shots.append({"start": round(st, 2), "end": round(en, 2), "dur": round(en - st, 2)})

    if not shots:
        # PySceneDetect returns an empty list when the whole clip is one shot.
        result.update({"available": True, "engine": "pyscenedetect-adaptive",
                       "scene_count": 1, "cuts": [], "shots": [], "shot_start_times": [round(lo, 2)]})
        return result

    shot_starts = [sh["start"] for sh in shots]
    cuts = shot_starts[1:]  # every shot boundary after the first is a cut
    result.update({
        "available": True,
        "engine": "pyscenedetect-adaptive",
        "scene_count": len(shots),
        "cuts": cuts,
        "shots": shots,
        "shot_start_times": shot_starts,
    })
    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: scenes.py <video-path> [start] [end]", file=sys.stderr)
        raise SystemExit(2)
    st = float(sys.argv[2]) if len(sys.argv) > 2 else None
    en = float(sys.argv[3]) if len(sys.argv) > 3 else None
    if not scenes_available():
        print(json.dumps({"available": False, "hint": "pip install scenedetect"}, indent=2))
        raise SystemExit(0)
    print(json.dumps(detect_scenes(sys.argv[1], st, en), indent=2))
