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
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


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


def _identify_shazam(audio_path: str) -> dict:
    if not identify_available():
        return {"available": False, "provider": "shazam", "hint": "pip install shazamio"}
    try:
        out = asyncio.run(_recognize(str(Path(audio_path).resolve())))
    except Exception as exc:
        return {"available": True, "provider": "shazam", "matched": False,
                "error": f"{type(exc).__name__}: {exc}"}
    res = _parse(out)
    res["provider"] = "shazam"
    return res


# --- ACRCloud: broader library/royalty-free coverage, plus custom buckets -----
# ACRCloud indexes far more than Shazam (incl. UGC/library catalogs) and lets you
# match against your OWN uploaded tracks (a "custom bucket"). It needs a free
# account and three keys in ~/.config/watch/.env:
#   ACRCLOUD_HOST, ACRCLOUD_ACCESS_KEY, ACRCLOUD_ACCESS_SECRET

def _acr_cfg(name: str) -> str | None:
    val = os.environ.get(name)
    if val and val.strip():
        return val.strip()
    try:
        from config import read_env_file
        return read_env_file().get(name)
    except Exception:
        return None


def acrcloud_configured() -> bool:
    return all(_acr_cfg(k) for k in
               ("ACRCLOUD_HOST", "ACRCLOUD_ACCESS_KEY", "ACRCLOUD_ACCESS_SECRET"))


def _identify_acrcloud(audio_path: str) -> dict:
    host, key, secret = (_acr_cfg("ACRCLOUD_HOST"),
                         _acr_cfg("ACRCLOUD_ACCESS_KEY"),
                         _acr_cfg("ACRCLOUD_ACCESS_SECRET"))
    try:
        import requests
    except Exception:
        return {"available": True, "provider": "acrcloud", "matched": False,
                "error": "requests not installed"}
    ts = str(int(time.time()))
    string_to_sign = "\n".join(["POST", "/v1/identify", key, "audio", "1", ts])
    sign = base64.b64encode(
        hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha1).digest()
    ).decode()
    try:
        data = Path(audio_path).read_bytes()
        # ACRCloud identifies off the first ~12s; cap the upload to stay light.
        sample = data[: 2 * 1024 * 1024]
        resp = requests.post(
            f"https://{host}/v1/identify",
            files={"sample": ("sample.mp3", sample)},
            data={"access_key": key, "data_type": "audio", "signature_version": "1",
                  "signature": sign, "sample_bytes": str(len(sample)), "timestamp": ts},
            timeout=25,
        )
        out = resp.json()
    except Exception as exc:
        return {"available": True, "provider": "acrcloud", "matched": False,
                "error": f"{type(exc).__name__}: {exc}"}
    code = (out.get("status") or {}).get("code")
    if code != 0:
        # code 1001 = "no result" (ran fine, no match); others are real errors.
        msg = (out.get("status") or {}).get("msg")
        return {"available": True, "provider": "acrcloud", "matched": False,
                "error": None if code == 1001 else msg}
    meta = out.get("metadata") or {}
    custom = meta.get("custom_files") or []
    if custom:
        m = custom[0]
        return {"available": True, "provider": "acrcloud-custom", "matched": True,
                "title": m.get("title"), "artist": m.get("artist") or m.get("album")}
    music = meta.get("music") or []
    if not music:
        return {"available": True, "provider": "acrcloud", "matched": False}
    m = music[0]
    artists = ", ".join(a.get("name", "") for a in (m.get("artists") or [])) or None
    return {
        "available": True, "provider": "acrcloud", "matched": True,
        "title": m.get("title"), "artist": artists,
        "album": (m.get("album") or {}).get("name"),
        "genre": ", ".join(g.get("name", "") for g in (m.get("genres") or [])) or None,
        "released": m.get("release_date"),
    }


def identify_track(audio_path: str) -> dict:
    """Recognize the track in ``audio_path``. Never raises.

    Tries providers in order of catalog breadth: ACRCloud first when configured
    (broadest, incl. royalty-free/library and your own custom bucket), then
    Shazam. Returns the first match; if none match, returns a no-match that lists
    the providers tried.
    """
    if not audio_path or not Path(audio_path).exists():
        return {"available": True, "matched": False, "error": "audio missing"}

    tried: list[str] = []
    any_available = False

    if acrcloud_configured():
        acr = _identify_acrcloud(audio_path)
        any_available = any_available or acr.get("available", False)
        if acr.get("matched"):
            return acr
        tried.append(acr.get("provider", "acrcloud"))

    shz = _identify_shazam(audio_path)
    if shz.get("available"):
        any_available = True
        if shz.get("matched"):
            return shz
        tried.append("shazam")
    elif not acrcloud_configured():
        # Neither backend usable.
        return {"available": False, "hint": "pip install shazamio (or set ACRCLOUD_* keys)"}

    return {"available": any_available, "matched": False, "providers_tried": tried}


def format_identify_report(idf: dict) -> str:
    """Render the identification result as a markdown block."""
    if not idf or not idf.get("available"):
        return "_Track identification unavailable (`pip install shazamio` to enable)._"
    if not idf.get("matched"):
        err = idf.get("error")
        tried = idf.get("providers_tried") or []
        base = "_No catalogued music matched"
        if tried:
            base += f" ({'+'.join(tried)})"
        base += " — likely royalty-free/library music, original audio, or a sound effect."
        return base + (f" [{err}]_" if err else "_")
    prov = idf.get("provider")
    lines = [f"- **Track:** {idf.get('title') or '?'} — {idf.get('artist') or '?'}"
             + (f"  _(via {prov})_" if prov else "")]
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
