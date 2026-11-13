#!/usr/bin/env python3
"""Download a video via yt-dlp, or resolve a local file path — robustly.

Social platforms (Instagram, TikTok, X…) increasingly block, rate-limit, or
login-gate anonymous requests, so a bare yt-dlp call often fails to open a link.
This module hardens every yt-dlp call with:

- **TLS impersonation** (`--impersonate chrome`, via curl_cffi) so requests look
  like a real browser — the single biggest fix for "it won't open / got blocked".
- **Retries + backoff** (`--retries`, `--extractor-retries`, `--retry-sleep`) and
  a second, slower pass that sleeps between requests to beat rate limits.
- **Optional browser cookies** (`WATCH_COOKIES_BROWSER` / `WATCH_COOKIES_FILE`) so
  login-gated or private-to-you posts download using your own logged-in session.
- **Actionable errors**: on failure it classifies the cause (login/rate-limit/
  geo/unavailable/outdated-extractor) and tells the user the exact fix.

It also extracts engagement metadata (views/likes/comments, caption, hashtags)
so ad research gets the post's real performance signals, not just the pixels.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).parent.resolve()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv"}


def is_url(source: str) -> bool:
    if source.startswith("-"):
        return False
    parsed = urlparse(source)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _get_dl_config() -> dict:
    try:
        from config import get_config
        return get_config()
    except Exception:
        return {}


def _impersonate_supported() -> bool:
    """curl_cffi backs yt-dlp's --impersonate; present → impersonation works."""
    try:
        import curl_cffi  # noqa: F401
        return True
    except Exception:
        return False


def _robustness_args(config: dict | None, aggressive: bool = False) -> list[str]:
    """Common yt-dlp hardening flags built from config."""
    config = config or {}
    args = [
        "--retries", "5",
        "--fragment-retries", "5",
        "--extractor-retries", "3",
        "--retry-sleep", "linear=1::5",
    ]
    if str(config.get("impersonate", "on")) != "off" and _impersonate_supported():
        args += ["--impersonate", "chrome"]
    cookies_file = (config.get("cookies_file") or "").strip()
    cookies_browser = (config.get("cookies_browser") or "").strip()
    if cookies_file:
        args += ["--cookies", cookies_file]
    elif cookies_browser:
        args += ["--cookies-from-browser", cookies_browser]
    if aggressive:
        args += ["--sleep-requests", "2", "--min-sleep-interval", "1", "--max-sleep-interval", "5"]
    return args


def _classify_error(stderr: str) -> str | None:
    """Map a yt-dlp failure to one actionable sentence for the user."""
    s = (stderr or "").lower()
    cfg_hint = ("set `WATCH_COOKIES_BROWSER=chrome` (or edge/firefox/brave) in "
                "~/.config/watch/.env so yt-dlp uses your logged-in browser session")
    if any(w in s for w in ("login required", "sign in", "requested content is not available",
                            "use --cookies", "account", "authenticat")):
        return f"The post needs a logged-in session — {cfg_hint}."
    if any(w in s for w in ("429", "too many requests", "rate-limit", "rate limit")):
        return ("Rate-limited by the platform. Robust retries were already applied; "
                f"wait a minute and retry, or {cfg_hint}.")
    if "private" in s:
        return "The post is private — only an account that follows it can download it (needs cookies)."
    if any(w in s for w in ("geo", "not available in your country", "region", "not available from your location")):
        return "The post is geo-blocked in your region."
    if any(w in s for w in ("unavailable", "removed", "deleted", "has been removed")):
        return "The post appears to be unavailable (removed, deleted, or taken down)."
    if any(w in s for w in ("unable to extract", "unsupported url", "no video formats", "extractor")):
        return ("The extractor may be outdated or the URL isn't supported — "
                "update yt-dlp with `yt-dlp -U` (or `pip install -U yt-dlp`) and retry.")
    return None


def _run_ytdlp(cmd: list[str]) -> tuple[int, str]:
    """Run yt-dlp, streaming a trimmed view to stderr and returning its stderr."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        return 1, f"could not launch yt-dlp: {exc}"
    # Surface yt-dlp's own progress/errors to our stderr for visibility.
    if result.stdout:
        sys.stderr.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    sys.stderr.flush()
    return result.returncode, (result.stderr or "") + (result.stdout or "")


def resolve_local(path: str) -> dict:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise SystemExit(f"File not found: {p}")
    if p.suffix.lower() not in VIDEO_EXTS:
        print(
            f"[watch] warning: {p.suffix} is not a known video extension, proceeding anyway",
            file=sys.stderr,
        )
    return {
        "video_path": str(p),
        "subtitle_path": None,
        "info": {"title": p.name, "url": str(p)},
        "downloaded": False,
    }


def _pick_subtitle(out_dir: Path) -> Path | None:
    candidates = sorted(out_dir.glob("video*.vtt"))
    if not candidates:
        return None
    preferred = [
        c for c in candidates
        if any(marker in c.name for marker in (".en.", ".en-US.", ".en-GB.", ".en-orig."))
    ]
    return preferred[0] if preferred else candidates[0]


def _pick_video(out_dir: Path) -> Path | None:
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".opus"):
        for candidate in out_dir.glob(f"video*{ext}"):
            return candidate
    for candidate in out_dir.glob("video.*"):
        if candidate.suffix.lower() in VIDEO_EXTS:
            return candidate
    return None


def fetch_captions(url: str, out_dir: Path, config: dict | None = None) -> dict:
    """Fetch metadata and best available VTT captions without downloading video."""
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not installed. Install with: brew install yt-dlp")
    config = config if config is not None else _get_dl_config()

    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")
    base = [
        "yt-dlp",
        "--skip-download",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "en.*",
        "--sub-format", "vtt",
        "--convert-subs", "vtt",
        "--no-playlist",
        "--ignore-errors",
        "-o", output_template,
    ]
    # Metadata fetch is best-effort; harden it but don't fail the whole run on it.
    _run_ytdlp(base + _robustness_args(config) + ["--", url])
    if not (out_dir / "video.info.json").exists():
        _run_ytdlp(base + _robustness_args(config, aggressive=True) + ["--", url])

    subtitle = _pick_subtitle(out_dir)
    info = _read_info(out_dir / "video.info.json", url)
    return {
        "video_path": None,
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": False,
    }


def _hashtags(description: str | None) -> list[str]:
    if not description:
        return []
    return re.findall(r"#\w+", description)


def _read_info(info_path: Path, url: str) -> dict:
    info: dict = {}
    if info_path.exists():
        try:
            raw = json.loads(info_path.read_text(encoding="utf-8"))
            desc = raw.get("description")
            info = {
                "title": raw.get("title"),
                "uploader": raw.get("uploader") or raw.get("channel"),
                "duration": raw.get("duration"),
                "url": raw.get("webpage_url") or url,
                "track": raw.get("track"),
                "artist": raw.get("artist"),
                "album": raw.get("album"),
                # Engagement / ad-research signals (whichever the platform exposes).
                "view_count": raw.get("view_count"),
                "like_count": raw.get("like_count"),
                "comment_count": raw.get("comment_count"),
                "repost_count": raw.get("repost_count"),
                "upload_date": raw.get("upload_date"),
                "channel_follower_count": raw.get("channel_follower_count"),
                "caption": desc,
                "hashtags": _hashtags(desc) or (raw.get("tags") or []),
            }
        except Exception as exc:
            print(f"[watch] info.json parse failed: {exc}", file=sys.stderr)
            info = {"url": url}
    return info


def download_url(url: str, out_dir: Path, audio_only: bool = False, config: dict | None = None) -> dict:
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not installed. Install with: brew install yt-dlp")
    config = config if config is not None else _get_dl_config()

    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")
    fmt = "ba/bestaudio" if audio_only else "bv*[height<=720]+ba/b[height<=720]/bv+ba/b"
    base = [
        "yt-dlp",
        "-N", "8",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "en.*",
        "--sub-format", "vtt",
        "--convert-subs", "vtt",
        "--no-playlist",
        "--ignore-errors",
        "-o", output_template,
    ]

    # Attempt 1: hardened (impersonation + retries + cookies if configured).
    # Attempt 2 (only if nothing downloaded): add request sleeps to beat rate limits.
    last_stderr = ""
    for aggressive in (False, True):
        if aggressive:
            if _pick_video(out_dir) is not None:
                break
            print("[watch] first attempt failed — retrying with slower, rate-limit-safe settings…",
                  file=sys.stderr)
        rc, stderr = _run_ytdlp(base + _robustness_args(config, aggressive=aggressive) + ["--", url])
        last_stderr = stderr
        if _pick_video(out_dir) is not None:
            break

    video = _pick_video(out_dir)
    if video is None:
        hint = _classify_error(last_stderr)
        msg = f"yt-dlp could not download a video from {url}."
        if hint:
            msg += f"\n[watch] Likely cause: {hint}"
        else:
            msg += ("\n[watch] If this keeps happening, update yt-dlp (`yt-dlp -U`) or set "
                    "`WATCH_COOKIES_BROWSER=chrome` in ~/.config/watch/.env to use your browser session.")
        raise SystemExit(msg)

    subtitle = _pick_subtitle(out_dir)
    info = _read_info(out_dir / "video.info.json", url)
    return {
        "video_path": str(video),
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": True,
    }


def download(source: str, out_dir: Path, audio_only: bool = False, config: dict | None = None) -> dict:
    if is_url(source):
        return download_url(source, out_dir, audio_only=audio_only, config=config)
    return resolve_local(source)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: download.py <url-or-path> <out-dir>", file=sys.stderr)
        raise SystemExit(2)
    result = download(sys.argv[1], Path(sys.argv[2]))
    print(json.dumps(result, indent=2))
