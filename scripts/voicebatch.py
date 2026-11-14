#!/usr/bin/env python3
"""Batch voice check + calibration for /watch.

Two jobs, both local:

  voicebatch.py calibrate <real_dir> [--ai <ai_dir>] [--no-stems]
      Score a folder of your KNOWN-REAL voices (and optionally a folder of known
      AI/cloned ones), learn the score ranges, pick a decision threshold, and save
      it to ~/.config/watch/voice_calibration.json. After this, every voice check
      returns a confident REAL / AI verdict tuned to YOUR voices.

  voicebatch.py scan <dir> [--no-stems] [--csv out.csv]
      Run the voice check on every video/audio file in a folder and print a table
      (file -> verdict, AI score, warmth, HNR). Flags the ones that read as AI so
      a human only spot-checks those.

Each file is scored on its isolated vocal stem by default (Demucs) for a clean
read; pass --no-stems to score the raw mix (faster, less accurate). Everything
runs on the machine; nothing is uploaded.
"""
from __future__ import annotations

import csv as csvmod
import json
import statistics
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import voicecheck as vc  # noqa: E402
from voicecheck import CALIBRATION_FILE  # noqa: E402

MEDIA_EXTS = {".mp4", ".mkv", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv",
              ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}


def _list_media(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*")
                  if p.is_file() and p.suffix.lower() in MEDIA_EXTS)


def _prepare_voice_audio(src: Path, work: Path, use_stems: bool) -> str | None:
    """Return a path to clean voice audio for scoring (vocal stem, or 16k mono mix)."""
    from local_whisper import extract_audio
    audio = work / "audio.mp3"
    try:
        extract_audio(str(src), audio)
    except SystemExit:
        return None
    if not use_stems:
        return str(audio)
    try:
        import stems
        if stems.stems_available():
            res = stems.separate(str(audio), work / "stems")
            if res and res.get("vocals"):
                return res["vocals"]
    except Exception:
        pass
    return str(audio)  # fall back to the mix if stems unavailable


def score_file(src: Path, use_stems: bool, calibration: dict | None) -> dict:
    """Score one media file → {file, ai_prob, verdict, warmth, hnr, ...}."""
    with tempfile.TemporaryDirectory(prefix="vbatch-") as td:
        voice = _prepare_voice_audio(src, Path(td), use_stems)
        if not voice:
            return {"file": src.name, "ok": False, "error": "no audio"}
        result = vc.voice_check(voice, run_ai=True, calibration=calibration)
    ai = result.get("ai") or {}
    ver = result.get("verdict") or {}
    ac = result.get("acoustics") or {}
    wm = result.get("warmth") or {}
    return {
        "file": src.name,
        "ok": ai.get("available", False),
        "ai_prob": ai.get("ai_probability"),
        "verdict": ver.get("verdict"),
        "confidence_pct": ver.get("confidence_pct"),
        "warmth": wm.get("warmth_label"),
        "hnr_db": ac.get("hnr_db"),
        "jitter_pct": ac.get("jitter_pct"),
        "shimmer_pct": ac.get("shimmer_pct"),
        "pitch_hz": ac.get("f0_mean_hz"),
    }


def _p(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((pct / 100) * (len(s) - 1)))))
    return s[k]


def calibrate(real_dir: Path, ai_dir: Path | None, use_stems: bool) -> dict:
    real_files = _list_media(real_dir)
    if not real_files:
        raise SystemExit(f"No media files found in {real_dir}")
    print(f"[voicebatch] scoring {len(real_files)} REAL file(s)…", file=sys.stderr)
    real_scores = []
    for i, f in enumerate(real_files, 1):
        r = score_file(f, use_stems, None)
        if r.get("ai_prob") is not None:
            real_scores.append(r["ai_prob"])
        print(f"  [{i}/{len(real_files)}] {f.name}: {r.get('ai_prob')}", file=sys.stderr)

    ai_scores: list[float] = []
    if ai_dir:
        ai_files = _list_media(ai_dir)
        print(f"[voicebatch] scoring {len(ai_files)} known-AI file(s)…", file=sys.stderr)
        for i, f in enumerate(ai_files, 1):
            r = score_file(f, use_stems, None)
            if r.get("ai_prob") is not None:
                ai_scores.append(r["ai_prob"])
            print(f"  [{i}/{len(ai_files)}] {f.name}: {r.get('ai_prob')}", file=sys.stderr)

    if not real_scores:
        raise SystemExit("Could not score any real files (is the AI detector installed?).")

    real_hi = _p(real_scores, 90)
    if ai_scores:
        ai_lo = _p(ai_scores, 10)
        threshold = round((real_hi + ai_lo) / 2, 3)
    else:
        # Only-real calibration: put the line just above the real range.
        threshold = round(min(0.9, max(real_hi + 0.08, 0.35)), 3)

    cal = {
        "threshold": threshold,
        "model": vc._AI_MODEL,
        "real": {"n": len(real_scores), "min": round(min(real_scores), 3),
                 "max": round(max(real_scores), 3),
                 "mean": round(statistics.mean(real_scores), 3), "p90": round(real_hi, 3)},
        "ai": ({"n": len(ai_scores), "min": round(min(ai_scores), 3),
                "max": round(max(ai_scores), 3),
                "mean": round(statistics.mean(ai_scores), 3)} if ai_scores else None),
        "used_stems": use_stems,
    }
    CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    CALIBRATION_FILE.write_text(json.dumps(cal, indent=2), encoding="utf-8")
    print(f"[voicebatch] calibration saved → {CALIBRATION_FILE}", file=sys.stderr)
    return cal


def scan(target: Path, use_stems: bool, csv_out: Path | None) -> list[dict]:
    files = _list_media(target) if target.is_dir() else [target]
    if not files:
        raise SystemExit(f"No media files found in {target}")
    cal = vc.load_calibration()
    if not cal:
        print("[voicebatch] no calibration found — using the default threshold. "
              "Run `calibrate` on your real reels first for reliable verdicts.", file=sys.stderr)
    rows = []
    print(f"[voicebatch] scanning {len(files)} file(s)…", file=sys.stderr)
    for i, f in enumerate(files, 1):
        r = score_file(f, use_stems, cal)
        rows.append(r)
        print(f"  [{i}/{len(files)}] {f.name}: {r.get('verdict')} "
              f"({r.get('ai_prob')})", file=sys.stderr)
    _print_table(rows, cal)
    if csv_out:
        with open(csv_out, "w", newline="", encoding="utf-8") as fh:
            w = csvmod.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"[voicebatch] wrote {csv_out}", file=sys.stderr)
    return rows


def _print_table(rows: list[dict], cal: dict | None) -> None:
    print()
    print("# Voice batch results")
    print()
    cal_note = (f"calibrated (threshold {cal['threshold']}, real range "
                f"{cal['real']['min']}–{cal['real']['max']})" if cal else
                "UNCALIBRATED (default threshold) — run `calibrate` for reliable verdicts")
    print(f"_{cal_note}._")
    print()
    print("| # | File | Verdict | AI score | Conf | Warmth | HNR |")
    print("|---|------|---------|----------|------|--------|-----|")
    for i, r in enumerate(rows, 1):
        v = r.get("verdict") or "—"
        flag = " ⚠️" if v == "AI" else ""
        print(f"| {i} | {r['file'][:40]} | {v}{flag} | {r.get('ai_prob')} | "
              f"{r.get('confidence_pct')} | {r.get('warmth') or '—'} | {r.get('hnr_db')} |")
    ai_n = sum(1 for r in rows if r.get("verdict") == "AI")
    print()
    print(f"**{ai_n} of {len(rows)} flagged as AI-generated.**")


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in ("calibrate", "scan"):
        print("usage:\n  voicebatch.py calibrate <real_dir> [--ai <ai_dir>] [--no-stems]"
              "\n  voicebatch.py scan <dir> [--no-stems] [--csv out.csv]", file=sys.stderr)
        return 2
    cmd = args[0]
    use_stems = "--no-stems" not in args
    if cmd == "calibrate":
        real = Path(args[1]).expanduser()
        ai = None
        if "--ai" in args:
            ai = Path(args[args.index("--ai") + 1]).expanduser()
        cal = calibrate(real, ai, use_stems)
        print(json.dumps(cal, indent=2))
        return 0
    # scan
    target = Path(args[1]).expanduser()
    csv_out = None
    if "--csv" in args:
        csv_out = Path(args[args.index("--csv") + 1]).expanduser()
    scan(target, use_stems, csv_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
