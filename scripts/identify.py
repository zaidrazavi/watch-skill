#!/usr/bin/env python3
"""Music / sound identification (Shazam-style) for /watch — OPT-IN only.

This is the one feature in /watch that is **not** local: identifying a named
track needs a fingerprint catalog, which only exists behind an online service.
It uses `shazamio` (an unofficial, key-free Shazam client) to recognize the
track playing in the clip. Because it sends a short audio fingerprint off the
machine, it runs **only when explicitly requested** (`watch.py --identify`),
never by default — the rest of /watch stays fully offline.

Scope: Shazam identifies **music** (commercial/known songs). It does not name
arbitrary sound effects or original/unreleased audio; those come back as "no
match". If the skill was asked "what's this sound effect", a no-match is itself
the answer (it is not a catalogued song).

Fail-open: if `shazamio` is not installed, or the network/recognition fails, it
returns an "unavailable"/"no match" result and the rest of the report is
unaffected.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


def identify_available() -> bool:
    """True when shazamio can be imported."""
    try:
        import shazamio  # noqa: F401
        return True
    except Exception:
        return False


async def _recognize(audio_path: str) -> dict:
    from shazamio import Shazam
    shazam = Shazam()
    # Newer shazamio exposes recognize(path); older used recognize_song(path).
    if hasattr(shazam, "recognize"):
        return await shazam.recognize(audio_path)
    return await shazam.recognize_song(audio_path)


def _parse(out: dict) -> dict:
    track = (out or {}).get("track") or {}
    if not track:
        return {"available": True, "matched": False}
    # Pull a few stable, useful fields; everything else stays in `raw` light form.
    genres = track.get("genres") or {}
    sections = track.get("sections") or []
    meta = {}
    for sec in sections:
        for item in sec.get("metadata", []) or []:
            if item.get("title") and item.get("text"):
                meta[item["title"]] = item["text"]
    return {
        "available": True,
        "matched": True,
        "title": track.get("title"),
        "artist": track.get("subtitle"),
        "genre": genres.get("primary"),
        "shazam_url": track.get("url"),
        "album": meta.get("Album"),
        "released": meta.get("Released"),
        "label": meta.get("Label"),
    }


def identify_track(audio_path: str) -> dict:
    """Recognize the track in ``audio_path`` via Shazam. Never raises.

    Returns one of:
      {available: False, hint}                         — shazamio not installed
      {available: True, matched: False}                — ran, no catalog match
      {available: True, matched: True, title, artist…} — identified
    """
    if not identify_available():
        return {"available": False, "hint": "pip install shazamio"}
    if not audio_path or not Path(audio_path).exists():
        return {"available": True, "matched": False, "error": "audio missing"}
    try:
        out = asyncio.run(_recognize(str(Path(audio_path).resolve())))
    except Exception as exc:
        return {"available": True, "matched": False, "error": f"{type(exc).__name__}: {exc}"}
    return _parse(out)


def format_identify_report(idf: dict) -> str:
    """Render the identification result as a markdown block."""
    if not idf or not idf.get("available"):
        return "_Track identification unavailable (`pip install shazamio` to enable)._"
    if not idf.get("matched"):
        err = idf.get("error")
        base = "_No catalogued music matched"
        base += " — likely original audio, a sound effect, or an unreleased track."
        return base + (f" ({err})_" if err else "_")
    lines = [f"- **Track:** {idf.get('title') or '?'} — {idf.get('artist') or '?'}"]
    if idf.get("album"):
        lines.append(f"- **Album:** {idf['album']}" + (f" ({idf['released']})" if idf.get("released") else ""))
    if idf.get("genre"):
        lines.append(f"- **Genre:** {idf['genre']}")
    if idf.get("label"):
        lines.append(f"- **Label:** {idf['label']}")
    if idf.get("shazam_url"):
        lines.append(f"- **Shazam:** {idf['shazam_url']}")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: identify.py <audio-path>", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(identify_track(sys.argv[1]), indent=2, ensure_ascii=False))
