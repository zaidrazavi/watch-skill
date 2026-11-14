#!/usr/bin/env python3
"""Shared /watch configuration helpers."""
from __future__ import annotations

import os
from pathlib import Path


CONFIG_DIR = Path.home() / ".config" / "watch"
CONFIG_FILE = CONFIG_DIR / ".env"

DEFAULT_DETAIL = "balanced"

DETAILS = {"transcript", "efficient", "balanced", "token-burner"}

# OCR / audio defaults. "auto" lets /watch decide at runtime (OCR on when an
# engine is installed and frames exist; audio on when the media has an audio
# track). Users can pin "on"/"off" in ~/.config/watch/.env.
TOGGLES = {"auto", "on", "off"}
DEFAULT_OCR = "auto"
DEFAULT_AUDIO = "auto"
# Stem separation is heavy (Demucs + torch), so it stays OFF unless explicitly
# turned on with --stems/--lyrics or WATCH_STEMS=on.
STEMS_TOGGLES = {"on", "off"}
DEFAULT_STEMS = "off"


def read_env_file(path: Path | None = None) -> dict[str, str]:
    if path is None:
        path = CONFIG_FILE
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]
        else:
            # Strip an inline comment (a '#' preceded by whitespace) from an
            # unquoted value. Without this, `WATCH_DETAIL=balanced  # note`
            # parses as "balanced  # note", fails validation, and silently
            # falls back to the default. Keeps '#' inside quotes / API keys.
            for i, ch in enumerate(value):
                if ch == "#" and i > 0 and value[i - 1] in " \t":
                    value = value[:i].rstrip()
                    break
        values[key.strip()] = value
    return values


def get_config() -> dict[str, object]:
    file_values = read_env_file()

    detail = (
        os.environ.get("WATCH_DETAIL")
        or file_values.get("WATCH_DETAIL")
        or DEFAULT_DETAIL
    )
    if detail not in DETAILS:
        detail = DEFAULT_DETAIL

    ocr = (os.environ.get("WATCH_OCR") or file_values.get("WATCH_OCR") or DEFAULT_OCR).lower()
    if ocr not in TOGGLES:
        ocr = DEFAULT_OCR
    audio = (os.environ.get("WATCH_AUDIO") or file_values.get("WATCH_AUDIO") or DEFAULT_AUDIO).lower()
    if audio not in TOGGLES:
        audio = DEFAULT_AUDIO

    stems = (os.environ.get("WATCH_STEMS") or file_values.get("WATCH_STEMS") or DEFAULT_STEMS).lower()
    if stems not in STEMS_TOGGLES:
        stems = DEFAULT_STEMS

    voice_check = (os.environ.get("WATCH_VOICE_CHECK")
                   or file_values.get("WATCH_VOICE_CHECK") or "off").lower()
    if voice_check not in ("on", "off"):
        voice_check = "off"

    # Download-robustness settings (for sites that block/rate-limit/login-gate).
    cookies_browser = (os.environ.get("WATCH_COOKIES_BROWSER")
                       or file_values.get("WATCH_COOKIES_BROWSER") or "").strip()
    cookies_file = (os.environ.get("WATCH_COOKIES_FILE")
                    or file_values.get("WATCH_COOKIES_FILE") or "").strip()
    impersonate = (os.environ.get("WATCH_IMPERSONATE")
                   or file_values.get("WATCH_IMPERSONATE") or "on").strip().lower()
    if impersonate not in ("on", "off"):
        impersonate = "on"

    return {
        "detail": detail,
        "ocr": ocr,
        "audio": audio,
        "stems": stems,
        "cookies_browser": cookies_browser,
        "cookies_file": cookies_file,
        "impersonate": impersonate,
        "config_file": str(CONFIG_FILE),
    }


def frame_cap(detail: str) -> int | None:
    if detail == "efficient":
        return 50
    if detail == "balanced":
        return 100
    if detail == "token-burner":
        return None
    if detail == "transcript":
        return None
    return 100
