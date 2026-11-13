#!/usr/bin/env python3
"""On-screen text OCR for /watch — reads burned-in captions, CTAs, prices.

Most ads and reels bake their most important words straight into the pixels
(subtitles, "LINK IN BIO", "50% OFF", a price, a product name). Scene frames at
512px often render those too small for Claude to read reliably, so this module
runs a dedicated OCR pass over the extracted frames and attaches the recognized
text to each one.

100% local and free, same ethos as the rest of /watch:
- **Primary engine: RapidOCR** (`rapidocr-onnxruntime`) — pip-installable on
  every platform, bundles small ONNX models, runs on CPU via onnxruntime, and
  never touches the network after install. No system binary, no API key.
- **Fallback engine: pytesseract** — used only if RapidOCR is absent but a
  Tesseract binary is on PATH.
- If neither is available the module is a no-op: it returns an "unavailable"
  result and the main pipeline proceeds with frames + transcript as before.

The pass is fail-open throughout: a single frame that fails to OCR is skipped,
not fatal.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


_RAPIDOCR = None          # cached RapidOCR engine instance
_ENGINE_KIND: str | None = None  # "rapidocr" | "tesseract" | None (resolved once)


def _resolve_engine() -> str | None:
    """Return which OCR engine is usable, loading it once. Cached across calls."""
    global _RAPIDOCR, _ENGINE_KIND
    if _ENGINE_KIND is not None:
        return _ENGINE_KIND if _ENGINE_KIND != "none" else None

    # Prefer RapidOCR (pure pip, offline, cross-platform).
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        _RAPIDOCR = RapidOCR()
        _ENGINE_KIND = "rapidocr"
        return _ENGINE_KIND
    except Exception:
        pass

    # Fallback: pytesseract, only if the tesseract binary is actually present.
    try:
        import shutil
        import pytesseract  # type: ignore  # noqa: F401
        if shutil.which("tesseract"):
            _ENGINE_KIND = "tesseract"
            return _ENGINE_KIND
    except Exception:
        pass

    _ENGINE_KIND = "none"
    return None


def engine_available() -> bool:
    return _resolve_engine() is not None


def engine_name() -> str:
    kind = _resolve_engine()
    return kind or "none"


def _ocr_one_rapidocr(path: str, min_conf: float) -> list[dict]:
    assert _RAPIDOCR is not None
    try:
        result, _elapse = _RAPIDOCR(path)
    except Exception:
        return []
    if not result:
        return []
    out: list[dict] = []
    for item in result:
        # RapidOCR returns [box, text, score] per detected line.
        try:
            _box, text, score = item
        except (ValueError, TypeError):
            continue
        text = (text or "").strip()
        conf = float(score) if score is not None else 0.0
        if text and conf >= min_conf:
            out.append({"text": text, "conf": round(conf, 3)})
    return out


def _ocr_one_tesseract(path: str, min_conf: float) -> list[dict]:
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except Exception:
        return []
    try:
        data = pytesseract.image_to_data(
            Image.open(path), output_type=pytesseract.Output.DICT
        )
    except Exception:
        return []
    out: list[dict] = []
    for text, conf in zip(data.get("text", []), data.get("conf", [])):
        text = (text or "").strip()
        try:
            c = float(conf) / 100.0
        except (TypeError, ValueError):
            c = 0.0
        if text and c >= min_conf:
            out.append({"text": text, "conf": round(c, 3)})
    return out


def _dedupe_lines(lines: list[dict]) -> list[dict]:
    """Drop exact-duplicate text lines (keep the highest confidence)."""
    best: dict[str, dict] = {}
    for ln in lines:
        key = ln["text"].lower()
        if key not in best or ln["conf"] > best[key]["conf"]:
            best[key] = ln
    return list(best.values())


def ocr_frame(path: str, min_conf: float = 0.5) -> list[dict]:
    """OCR a single image, returning a list of {text, conf} lines (may be empty)."""
    kind = _resolve_engine()
    if kind == "rapidocr":
        return _dedupe_lines(_ocr_one_rapidocr(path, min_conf))
    if kind == "tesseract":
        return _dedupe_lines(_ocr_one_tesseract(path, min_conf))
    return []


def ocr_frames(
    frames: list[dict],
    min_conf: float = 0.5,
    max_frames: int | None = None,
) -> dict:
    """OCR each frame in ``frames`` (a list of dicts with a ``path`` key).

    Mutates each frame dict in place, adding:
      - ``ocr_text``: the joined recognized text (newline-separated), or ""
      - ``ocr_lines``: the list of {text, conf} entries
    and returns a summary dict ``{available, engine, frames_with_text, total_lines}``.

    ``max_frames`` optionally caps how many frames are OCR'd (newest ad research
    cares most about the opening frames, so callers can prioritize those); when
    set, frames beyond the cap get an empty ``ocr_text`` without being processed.
    """
    kind = _resolve_engine()
    summary = {
        "available": kind is not None,
        "engine": kind or "none",
        "frames_with_text": 0,
        "total_lines": 0,
        "processed": 0,
    }
    if kind is None:
        for fr in frames:
            fr.setdefault("ocr_text", "")
            fr.setdefault("ocr_lines", [])
        return summary

    processed = 0
    for fr in frames:
        if max_frames is not None and processed >= max_frames:
            fr.setdefault("ocr_text", "")
            fr.setdefault("ocr_lines", [])
            continue
        path = fr.get("path")
        lines = ocr_frame(path, min_conf=min_conf) if path else []
        processed += 1
        fr["ocr_lines"] = lines
        fr["ocr_text"] = "\n".join(ln["text"] for ln in lines)
        if lines:
            summary["frames_with_text"] += 1
            summary["total_lines"] += len(lines)
    summary["processed"] = processed
    return summary


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: ocr.py <image-path> [<image-path> ...] [--min-conf C]", file=sys.stderr)
        raise SystemExit(2)
    args = sys.argv[1:]
    min_conf = 0.5
    if "--min-conf" in args:
        idx = args.index("--min-conf")
        min_conf = float(args[idx + 1])
        args = args[:idx] + args[idx + 2:]
    if not engine_available():
        print(json.dumps({"available": False, "engine": "none",
                          "hint": "pip install rapidocr-onnxruntime"}, indent=2))
        raise SystemExit(0)
    frames = [{"path": p} for p in args]
    summary = ocr_frames(frames, min_conf=min_conf)
    print(json.dumps({"summary": summary, "frames": frames}, indent=2))
