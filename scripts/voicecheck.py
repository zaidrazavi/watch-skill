#!/usr/bin/env python3
"""Voice check for /watch — AI-likelihood, acoustics, and a warmth proxy.

Claude can't *hear* a voice, but a voice's "warmth", smoothness, and whether it's
AI-generated all leave measurable acoustic fingerprints. This module reports them:

1. **Warmth / tone proxy** (ffmpeg only, no deps) — spectral centroid, rolloff and
   flatness. A lower centroid + lower flatness = more low-mid, tonal energy, which
   is what listeners call "warm"; a high centroid = "bright/harsh". It's a proxy
   for the *feeling* of warmth, derived from the spectrum — not a human opinion.
2. **Voice acoustics** (Praat via `parselmouth`, optional) — mean pitch (F0),
   jitter, shimmer, and HNR (harmonics-to-noise). High HNR + low jitter/shimmer =
   a clear, stable, smooth voice; the opposite = rough/raspy/robotic.
3. **AI-voice likelihood** (a wav2vec2 synthetic-speech detector via
   `transformers`, optional/heavy) — a 0..1 probability the voice is AI-generated,
   the signal that reliably separates real speakers from cloned/synthetic ones.

Everything is local and fail-open: each part degrades to "unavailable" if its
dependency is missing, and nothing here ever leaves the machine.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

_CENTROID_RE = re.compile(r"aspectralstats\.\d+\.centroid=([0-9.]+)")
_ROLLOFF_RE = re.compile(r"aspectralstats\.\d+\.rolloff=([0-9.]+)")
_FLATNESS_RE = re.compile(r"aspectralstats\.\d+\.flatness=([0-9.]+)")


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def warmth_metrics(audio_path: str) -> dict:
    """Spectral warmth/tone proxy from ffmpeg aspectralstats (no extra deps)."""
    result = {"available": False, "centroid_hz": None, "rolloff_hz": None,
              "flatness": None, "warmth_label": None}
    if shutil.which("ffmpeg") is None or not Path(audio_path).exists():
        return result
    af = "aspectralstats=measure=centroid+rolloff+flatness,ametadata=mode=print"
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(Path(audio_path).resolve()),
             "-af", af, "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return result
    err = r.stderr or ""
    centroid = _mean([float(m.group(1)) for m in _CENTROID_RE.finditer(err)])
    rolloff = _mean([float(m.group(1)) for m in _ROLLOFF_RE.finditer(err)])
    flatness = _mean([float(m.group(1)) for m in _FLATNESS_RE.finditer(err)])
    if centroid is None:
        return result
    # Warmth read from the spectral centroid (voice, mono): low = warm.
    if centroid < 900:
        label = "warm (low-mid dominant)"
    elif centroid < 1600:
        label = "balanced"
    elif centroid < 2600:
        label = "bright"
    else:
        label = "harsh / thin (very bright)"
    result.update({"available": True, "centroid_hz": centroid, "rolloff_hz": rolloff,
                   "flatness": flatness, "warmth_label": label})
    return result


def voice_acoustics(audio_path: str) -> dict:
    """Praat acoustics via parselmouth: F0, jitter, shimmer, HNR. Fail-open."""
    result = {"available": False, "f0_mean_hz": None, "jitter_pct": None,
              "shimmer_pct": None, "hnr_db": None, "smoothness_label": None}
    try:
        import parselmouth
        from parselmouth.praat import call
    except Exception:
        return result
    try:
        snd = parselmouth.Sound(str(Path(audio_path).resolve()))
        pitch = snd.to_pitch()
        f0_vals = [v for v in pitch.selected_array["frequency"] if v > 0]
        f0_mean = round(sum(f0_vals) / len(f0_vals), 1) if f0_vals else None
        point_process = call(snd, "To PointProcess (periodic, cc)", 75, 500)
        jitter = call(point_process, "Get jitter (local)", 0, 0, 0.0001, 0.02, 1.3)
        shimmer = call([snd, point_process], "Get shimmer (local)", 0, 0, 0.0001, 0.02, 1.3, 1.6)
        harmonicity = call(snd, "To Harmonicity (cc)", 0.01, 75, 0.1, 1.0)
        hnr = call(harmonicity, "Get mean", 0, 0)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    jitter_pct = round(jitter * 100, 3) if jitter == jitter else None       # NaN-safe
    shimmer_pct = round(shimmer * 100, 3) if shimmer == shimmer else None
    hnr_db = round(hnr, 2) if hnr == hnr else None
    # Smoothness read: high HNR + low jitter/shimmer = clear, stable, "smooth".
    label = None
    if hnr_db is not None:
        if hnr_db >= 18 and (jitter_pct or 0) < 1.0:
            label = "clear & smooth"
        elif hnr_db >= 12:
            label = "moderate"
        else:
            label = "rough / noisy / breathy"
    result.update({"available": True, "f0_mean_hz": f0_mean, "jitter_pct": jitter_pct,
                   "shimmer_pct": shimmer_pct, "hnr_db": hnr_db, "smoothness_label": label})
    return result


_AI_MODEL = "MelodyMachine/Deepfake-audio-detection-V2"
_ai_pipe = None


def ai_detect_available() -> bool:
    try:
        import transformers  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def ai_voice_score(audio_path: str, model: str = _AI_MODEL) -> dict:
    """Probability the voice is AI-generated, via a wav2vec2 detector. Fail-open."""
    global _ai_pipe
    result = {"available": False, "ai_probability": None, "label": None, "model": model}
    if not ai_detect_available():
        return result
    try:
        if _ai_pipe is None:
            from transformers import pipeline
            _ai_pipe = pipeline("audio-classification", model=model)
        out = _ai_pipe(str(Path(audio_path).resolve()))
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    # Normalize: find the probability mass on the "fake/AI/spoof" class.
    ai_prob = None
    for item in out or []:
        lab = str(item.get("label", "")).lower()
        if any(k in lab for k in ("fake", "spoof", "ai", "synthetic", "deepfake", "generated")):
            ai_prob = float(item.get("score", 0.0))
            break
    if ai_prob is None and out:
        # Fall back: assume a binary real/fake head; 1 - top "real" score.
        real = next((float(i["score"]) for i in out
                     if any(k in str(i.get("label", "")).lower() for k in ("real", "human", "bonafide", "genuine"))), None)
        ai_prob = round(1 - real, 3) if real is not None else None
    if ai_prob is None:
        return result
    ai_prob = round(ai_prob, 3)
    if ai_prob >= 0.6:
        label = "likely AI-generated"
    elif ai_prob >= 0.35:
        label = "uncertain (possible AI artifacts)"
    else:
        label = "likely real / human-recorded"
    result.update({"available": True, "ai_probability": ai_prob, "label": label, "raw": out})
    return result


def voice_check(audio_path: str, run_ai: bool = True) -> dict:
    """Full voice check: warmth + acoustics + (optional) AI-likelihood."""
    return {
        "warmth": warmth_metrics(audio_path),
        "acoustics": voice_acoustics(audio_path),
        "ai": ai_voice_score(audio_path) if run_ai else {"available": False, "skipped": True},
    }


def format_voicecheck_report(vc: dict) -> str:
    lines: list[str] = []
    ai = vc.get("ai") or {}
    if ai.get("available"):
        lines.append(f"- **AI-voice likelihood:** {ai['ai_probability']:.0%} — {ai['label']} "
                     f"_(detector: {ai.get('model','').split('/')[-1]})_")
    elif ai.get("skipped"):
        pass
    else:
        lines.append("- **AI-voice likelihood:** detector not installed "
                     "(`pip install transformers torchaudio soundfile`).")
    w = vc.get("warmth") or {}
    if w.get("available"):
        lines.append(f"- **Warmth / tone:** {w['warmth_label']} "
                     f"(spectral centroid {w['centroid_hz']} Hz, flatness {w['flatness']})")
    a = vc.get("acoustics") or {}
    if a.get("available"):
        bits = []
        if a.get("f0_mean_hz") is not None:
            bits.append(f"pitch {a['f0_mean_hz']} Hz")
        if a.get("hnr_db") is not None:
            bits.append(f"HNR {a['hnr_db']} dB")
        if a.get("jitter_pct") is not None:
            bits.append(f"jitter {a['jitter_pct']}%")
        if a.get("shimmer_pct") is not None:
            bits.append(f"shimmer {a['shimmer_pct']}%")
        smooth = f" — {a['smoothness_label']}" if a.get("smoothness_label") else ""
        lines.append(f"- **Voice acoustics:** {', '.join(bits)}{smooth}")
    elif not a.get("available"):
        lines.append("- **Voice acoustics:** Praat not installed (`pip install praat-parselmouth`).")
    lines.append("- _Note: these are acoustic measurements, not a human judgment of how the voice "
                 "feels; use them to flag clips worth a listen._")
    return "\n".join(lines) if lines else "_Voice check produced no metrics._"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: voicecheck.py <audio-path> [--no-ai]", file=sys.stderr)
        raise SystemExit(2)
    run_ai = "--no-ai" not in sys.argv
    vc = voice_check(sys.argv[1], run_ai=run_ai)
    # drop bulky raw model output from CLI json
    if vc.get("ai"):
        vc["ai"].pop("raw", None)
    print(json.dumps(vc, indent=2))
