#!/usr/bin/env python3
"""/watch entry point: download video, extract frames, parse transcript.

Prints a markdown report to stdout listing frame paths + transcript, plus — new
in 0.4 — on-screen text (OCR), audio intelligence, and cut/pacing metrics, and
can compare several videos at once or emit a structured JSON report. Claude then
Reads each frame path to see the video.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


# Force UTF-8 I/O so OCR text, transcripts, and the markdown punctuation we emit
# survive on platforms whose default console encoding is not UTF-8 (e.g. Windows
# cp1252, where an emoji or accented glyph in on-screen text would otherwise
# raise UnicodeEncodeError and abort the whole report).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from config import frame_cap, get_config  # noqa: E402
from download import download, fetch_captions, is_url  # noqa: E402
from frames import MAX_FPS, auto_fps, auto_fps_focus, extract_at_timestamps, extract_keyframes, extract_scene_or_uniform, format_time, get_metadata, merge_frames, parse_time, parse_timestamps  # noqa: E402
from transcribe import filter_range, format_transcript, parse_vtt  # noqa: E402
from local_whisper import extract_audio, transcribe_video  # noqa: E402
from metrics import compute_pacing, format_pacing_report  # noqa: E402
from audio import analyze_audio, format_audio_report  # noqa: E402
from timeline import build_timeline, format_timeline  # noqa: E402
from identify import identify_track, format_identify_report  # noqa: E402
import ocr as ocr_mod  # noqa: E402
import stems as stems_mod  # noqa: E402
import scenes as scenes_mod  # noqa: E402
import voicecheck as voicecheck_mod  # noqa: E402


HOOK_SECONDS = 3.0


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="watch",
        description="Download a video, extract auto-scaled frames, and surface the transcript, "
                    "on-screen text, audio profile, and pacing.",
    )
    ap.add_argument("source", nargs="+", help="Video URL(s) or local file path(s). Pass more than one to compare.")
    ap.add_argument("--max-frames", type=int, default=None, help="Override frame cap")
    ap.add_argument("--resolution", type=int, default=512, help="Frame width in pixels (default 512)")
    ap.add_argument("--fps", type=float, default=None, help="Override auto-fps")
    ap.add_argument(
        "--detail",
        choices=["transcript", "efficient", "balanced", "token-burner"],
        default=None,
        help="Fidelity/speed dial: transcript (no frames), efficient (fast keyframes, cap 50), "
             "balanced (scene, cap 100), token-burner (scene, uncapped).",
    )
    ap.add_argument(
        "--timestamps",
        type=str,
        default=None,
        help="Comma-separated absolute timestamps (SS, MM:SS, HH:MM:SS) to grab a frame at.",
    )
    ap.add_argument("--start", type=str, default=None, help="Range start (SS, MM:SS, or HH:MM:SS)")
    ap.add_argument("--end", type=str, default=None, help="Range end (SS, MM:SS, or HH:MM:SS)")
    ap.add_argument("--out-dir", type=str, default=None, help="Working directory (default: tmp)")
    ap.add_argument(
        "--no-whisper",
        action="store_true",
        help="Disable local transcription. Report frames-only if no captions available.",
    )
    ap.add_argument(
        "--whisper-model",
        choices=["tiny", "base", "small", "medium", "large-v3"],
        default=None,
        help="Override the faster-whisper model size for this run.",
    )
    ap.add_argument(
        "--no-dedup",
        action="store_true",
        help="Disable near-duplicate frame removal.",
    )
    # --- New in 0.4 ---
    ocr_grp = ap.add_mutually_exclusive_group()
    ocr_grp.add_argument("--ocr", dest="ocr", action="store_true", default=None,
                         help="Force the on-screen text OCR pass over frames.")
    ocr_grp.add_argument("--no-ocr", dest="ocr", action="store_false",
                         help="Skip the on-screen text OCR pass.")
    ap.add_argument("--ocr-min-conf", type=float, default=0.5,
                    help="Minimum OCR confidence to keep a line (default 0.5).")
    ap.add_argument("--ocr-max-frames", type=int, default=60,
                    help="Cap how many frames get OCR'd (default 60; hook frames always included).")
    audio_grp = ap.add_mutually_exclusive_group()
    audio_grp.add_argument("--audio", dest="audio", action="store_true", default=None,
                           help="Force audio intelligence analysis.")
    audio_grp.add_argument("--no-audio", dest="audio", action="store_false",
                           help="Skip audio intelligence analysis.")
    stems_grp = ap.add_mutually_exclusive_group()
    stems_grp.add_argument("--stems", dest="stems", action="store_true", default=None,
                           help="Separate audio into vocals/music stems (Demucs, local). "
                                "Heavy: needs torch + a model download. Sharpens the timeline.")
    stems_grp.add_argument("--no-stems", dest="stems", action="store_false",
                           help="Skip stem separation.")
    ap.add_argument("--lyrics", dest="lyrics", action="store_true",
                    help="Transcribe the isolated vocal stem for sung lyrics (implies --stems).")
    ap.add_argument("--no-timeline", dest="timeline", action="store_false", default=True,
                    help="Skip the speech/music/singing timeline.")
    ap.add_argument("--identify", dest="identify", action="store_true",
                    help="Identify the music/track (Shazam-style, via shazamio). OPT-IN: "
                         "this sends a short audio fingerprint to an external service, so it "
                         "runs ONLY with this flag — use it when the question is about the "
                         "music or sound. Uses the isolated music stem when stems are available.")
    ap.add_argument("--voice-check", dest="voice_check", action="store_true",
                    help="Analyze the VOICE: AI-generated likelihood (wav2vec2 detector), "
                         "acoustics (pitch/jitter/shimmer/HNR via Praat), and a warmth/tone proxy. "
                         "Local. Runs on the isolated vocal stem when --stems is set (much cleaner). "
                         "Use when the question is about voice quality / is-it-AI / does-it-sound-right.")
    ap.add_argument("--no-voice-ai", dest="voice_ai", action="store_false", default=True,
                    help="With --voice-check, skip the heavy AI-voice detector (keep warmth + acoustics only).")
    ap.add_argument("--report-json", type=str, default=None,
                    help="Write the structured report as JSON to this path (markdown still prints).")
    ap.add_argument("--json", dest="json_stdout", action="store_true",
                    help="Emit the structured JSON report to stdout instead of markdown.")
    return ap


def build_report(source: str, args, config: dict, index: int = 0) -> dict:
    """Run the full pipeline for ONE source and return a structured report dict.

    No printing here — rendering (markdown / JSON / compare) is done by the
    caller from this dict, so single-video and multi-video paths share one code
    path for the actual analysis.
    """
    detail = args.detail or str(config["detail"])
    configured_cap = frame_cap(detail)
    max_frames = args.max_frames if args.max_frames is not None else configured_cap
    if max_frames is not None and max_frames < 1:
        raise SystemExit("--max-frames must be greater than zero")
    budget_cap = max_frames if max_frames is not None else 100
    cue_timestamps = parse_timestamps(args.timestamps)

    if args.out_dir:
        base = Path(args.out_dir).expanduser().resolve()
        work = base if len(args.source) == 1 else base / f"video{index + 1}"
    else:
        work = Path(tempfile.mkdtemp(prefix="watch-"))
    work.mkdir(parents=True, exist_ok=True)
    print(f"[watch] working dir: {work}", file=sys.stderr)

    url_source = is_url(source)
    dl: dict = {"subtitle_path": None, "info": {}, "downloaded": False}
    transcript_segments: list[dict] = []
    transcript_text: str | None = None
    transcript_source: str | None = None
    video_path: str | None = None

    if url_source:
        print("[watch] checking metadata/captions via yt-dlp…", file=sys.stderr)
        dl = fetch_captions(source, work / "download", config=config)
        if dl.get("subtitle_path"):
            try:
                transcript_segments = parse_vtt(dl["subtitle_path"])
                transcript_text = format_transcript(transcript_segments)
                transcript_source = "captions"
            except Exception as exc:
                print(f"[watch] subtitle parse failed: {exc}", file=sys.stderr)
                transcript_segments = []

    audio_only = detail == "transcript" and not cue_timestamps
    if detail == "transcript" and transcript_segments and not cue_timestamps:
        video_path = None
    else:
        if url_source:
            print(
                "[watch] downloading audio via yt-dlp…" if audio_only
                else "[watch] downloading video via yt-dlp…",
                file=sys.stderr,
            )
            dl = download(source, work / "download", audio_only=audio_only, config=config)
        else:
            print("[watch] using local file…", file=sys.stderr)
            dl = download(source, work / "download", config=config)
        video_path = dl["video_path"]

    meta = get_metadata(video_path) if video_path else {
        "duration_seconds": float((dl.get("info") or {}).get("duration") or 0),
        "width": None, "height": None, "codec": None, "has_audio": False,
    }
    full_duration = meta["duration_seconds"]

    start_sec = parse_time(args.start)
    end_sec = parse_time(args.end)
    if start_sec is not None and start_sec < 0:
        raise SystemExit("--start must be non-negative")
    if end_sec is not None and start_sec is not None and end_sec <= start_sec:
        raise SystemExit("--end must be greater than --start")
    if full_duration > 0 and start_sec is not None and start_sec >= full_duration:
        raise SystemExit(f"--start {start_sec:.1f}s is past end of video ({full_duration:.1f}s)")

    effective_start = start_sec if start_sec is not None else 0.0
    effective_end = end_sec if end_sec is not None else full_duration
    effective_duration = max(0.0, effective_end - effective_start)
    focused = start_sec is not None or end_sec is not None

    if focused:
        fps, target = auto_fps_focus(effective_duration, max_frames=budget_cap)
    else:
        fps, target = auto_fps(effective_duration, max_frames=budget_cap)
    if args.fps is not None:
        fps = min(args.fps, MAX_FPS)
        target = max(1, int(round(fps * effective_duration)))

    if transcript_segments and focused:
        transcript_segments = filter_range(transcript_segments, start_sec, end_sec)
        transcript_text = format_transcript(transcript_segments)

    frames: list[dict] = []
    frame_meta: dict = {"engine": "none", "candidate_count": 0, "selected_count": 0, "fallback": False}
    cue_frames: list[dict] = []
    cue_meta: dict = {}

    if cue_timestamps and video_path:
        cue_frames, cue_meta = extract_at_timestamps(
            video_path, work / "frames", cue_timestamps,
            resolution=args.resolution, max_frames=max_frames,
            start_seconds=start_sec, end_seconds=end_sec,
        )

    detail_budget = max_frames if max_frames is None else max(0, max_frames - len(cue_frames))
    if detail != "transcript" and video_path and detail_budget != 0:
        if detail == "efficient":
            frames, frame_meta = extract_keyframes(
                video_path, work / "frames", resolution=args.resolution,
                max_frames=detail_budget, start_seconds=start_sec, end_seconds=end_sec,
                dedup=not args.no_dedup,
            )
        else:
            frames, frame_meta = extract_scene_or_uniform(
                video_path, work / "frames", fps=fps, target_frames=target,
                resolution=args.resolution, max_frames=detail_budget,
                start_seconds=start_sec, end_seconds=end_sec, dedup=not args.no_dedup,
            )

    if cue_frames:
        frames = merge_frames(frames, cue_frames)

    if not transcript_segments and dl.get("subtitle_path"):
        try:
            all_segments = parse_vtt(dl["subtitle_path"])
            transcript_segments = filter_range(all_segments, start_sec, end_sec) if focused else all_segments
            transcript_text = format_transcript(transcript_segments)
            transcript_source = "captions"
        except Exception as exc:
            print(f"[watch] subtitle parse failed: {exc}", file=sys.stderr)

    if not transcript_segments and not args.no_whisper and video_path and meta.get("has_audio"):
        try:
            all_segments, used_backend = transcribe_video(
                video_path, work / "audio.mp3", model_size=args.whisper_model,
            )
            transcript_segments = filter_range(all_segments, start_sec, end_sec) if focused else all_segments
            transcript_text = format_transcript(transcript_segments)
            transcript_source = used_backend
        except SystemExit as exc:
            setup_py = SCRIPT_DIR / "setup.py"
            print(f"[watch] local transcription failed: {exc} — run `python3 {setup_py}`", file=sys.stderr)
    elif not transcript_segments and video_path and not meta.get("has_audio"):
        print("[watch] no audio stream found — proceeding without transcription", file=sys.stderr)

    # --- On-screen text OCR ---------------------------------------------------
    ocr_wanted = args.ocr
    if ocr_wanted is None:  # no CLI flag → fall back to config toggle (default auto)
        ocr_pref = str(config.get("ocr", "auto"))
        if ocr_pref == "off":
            ocr_wanted = False
        elif ocr_pref == "on":
            ocr_wanted = True
        else:  # auto: on when an engine is available and we have frames
            ocr_wanted = bool(frames) and ocr_mod.engine_available()
    ocr_summary = {"available": False, "engine": "none", "frames_with_text": 0, "total_lines": 0, "processed": 0}
    if ocr_wanted and frames:
        if not ocr_mod.engine_available():
            print("[watch] OCR requested but no engine available — "
                  "`pip install rapidocr-onnxruntime` to enable on-screen text.", file=sys.stderr)
        else:
            # Prioritize hook-window frames so they're never skipped by the cap.
            hook_end = effective_start + HOOK_SECONDS
            ordered = sorted(
                frames,
                key=lambda f: 0 if effective_start - 1e-6 <= f["timestamp_seconds"] <= hook_end + 1e-6 else 1,
            )
            print(f"[watch] running OCR ({ocr_mod.engine_name()}) over up to "
                  f"{min(len(frames), args.ocr_max_frames)} frame(s)…", file=sys.stderr)
            ocr_summary = ocr_mod.ocr_frames(ordered, min_conf=args.ocr_min_conf, max_frames=args.ocr_max_frames)
    else:
        for fr in frames:
            fr.setdefault("ocr_text", "")
            fr.setdefault("ocr_lines", [])

    # --- Audio: analysis, stem separation, lyrics, timeline -------------------
    have_media_audio = bool(video_path) and meta.get("has_audio", False)
    audio_wanted = args.audio
    if audio_wanted is None:
        audio_pref = str(config.get("audio", "auto"))
        audio_wanted = False if audio_pref == "off" else have_media_audio

    # Stems are opt-in (heavy): --stems, --lyrics, or WATCH_STEMS=on.
    stems_wanted = args.stems
    if stems_wanted is None:
        stems_wanted = str(config.get("stems", "off")) == "on"
    if args.lyrics:
        stems_wanted = True

    audio_dur = effective_duration if focused else full_duration
    audio_result = {"available": False}
    stem_result = None
    lyrics_segments: list[dict] = []
    lyrics_source: str | None = None
    timeline_result: dict = {"available": False}

    # Ensure a single shared audio clip for every audio consumer.
    audio_src = None
    if have_media_audio and (audio_wanted or stems_wanted or args.timeline):
        candidate = work / "audio.mp3"
        if not candidate.exists():
            try:
                extract_audio(video_path, candidate)
            except SystemExit as exc:
                print(f"[watch] audio extraction failed: {exc}", file=sys.stderr)
                candidate = None
        audio_src = candidate if candidate and Path(candidate).exists() else None

    if audio_wanted and audio_src:
        print("[watch] analyzing audio (loudness / silence / content mix)…", file=sys.stderr)
        audio_result = analyze_audio(
            str(audio_src), audio_dur,
            transcript_segments=transcript_segments, hook_start=effective_start,
        )

    if stems_wanted and audio_src:
        if not stems_mod.stems_available():
            print("[watch] stem separation requested but Demucs is not installed — "
                  "`pip install demucs` to enable (vocals/music split + lyrics).", file=sys.stderr)
        else:
            stem_result = stems_mod.separate(str(audio_src), work / "stems")

    if args.lyrics and stem_result and stem_result.get("vocals"):
        try:
            print("[watch] transcribing isolated vocal stem for lyrics…", file=sys.stderr)
            lyrics_segments, lyrics_backend = transcribe_video(
                stem_result["vocals"], work / "vocals_16k.mp3",
                model_size=args.whisper_model, vad_filter=False,
            )
            if focused:
                lyrics_segments = filter_range(lyrics_segments, start_sec, end_sec)
            lyrics_source = f"{lyrics_backend} (vocals stem)"
        except SystemExit as exc:
            print(f"[watch] lyric transcription failed: {exc}", file=sys.stderr)

    if args.timeline and audio_src:
        print("[watch] building speech/music/singing timeline…", file=sys.stderr)
        timeline_result = build_timeline(
            str(audio_src), audio_dur, transcript_segments=transcript_segments,
            vocals_path=stem_result.get("vocals") if stem_result else None,
            music_path=stem_result.get("music") if stem_result else None,
        )

    # --- Music/track identification (OPT-IN, external) ------------------------
    identify_result: dict | None = None
    if args.identify and audio_src:
        # Prefer the isolated music stem (voiceover removed) when available.
        id_src = stem_result.get("music") if stem_result and stem_result.get("music") else str(audio_src)
        print("[watch] identifying track via Shazam (sends an audio fingerprint "
              "to an external service)…", file=sys.stderr)
        identify_result = identify_track(id_src)

    # --- Voice check: AI-likelihood + acoustics + warmth (OPT-IN) ------------
    voicecheck_result: dict | None = None
    voice_check_wanted = args.voice_check or str(config.get("voice_check", "off")) == "on"
    if voice_check_wanted and audio_src:
        # The isolated vocal stem gives much cleaner acoustics/AI signal (no music).
        vc_src = stem_result.get("vocals") if stem_result and stem_result.get("vocals") else str(audio_src)
        run_ai = getattr(args, "voice_ai", True)
        print(f"[watch] voice check (warmth / acoustics{' / AI-likelihood' if run_ai else ''}) "
              f"on {'vocal stem' if stem_result else 'mixed audio'}…", file=sys.stderr)
        voicecheck_result = voicecheck_mod.voice_check(vc_src, run_ai=run_ai)
        voicecheck_result["source"] = "vocal stem" if stem_result else "mixed audio"

    # --- Pacing / cut metrics -------------------------------------------------
    # Prefer PySceneDetect for EXACT cuts + gradual transitions; fall back to the
    # frame engine's cut data when it isn't installed.
    pacing_dur = effective_duration if focused else full_duration
    scene_data = None
    if video_path and scenes_mod.scenes_available():
        print("[watch] detecting exact cuts/transitions (PySceneDetect)…", file=sys.stderr)
        scene_data = scenes_mod.detect_scenes(video_path, start_seconds=start_sec, end_seconds=end_sec)
    if scene_data and scene_data.get("available"):
        pacing = compute_pacing(
            scene_data["shot_start_times"], pacing_dur,
            engine=scene_data["engine"], hook_start=effective_start,
        )
    else:
        pacing = compute_pacing(
            frame_meta.get("candidate_timestamps"), pacing_dur,
            engine=frame_meta.get("engine", "none"), hook_start=effective_start,
        )

    # --- Hook assembly --------------------------------------------------------
    hook_start = effective_start
    hook_end = effective_start + HOOK_SECONDS
    hook_words: list[dict] = []
    for seg in transcript_segments:
        for w in seg.get("words", []) or []:
            if w.get("end", 0) > hook_start and w.get("start", 0) < hook_end:
                hook_words.append(w)
    hook_seg_texts = [
        seg["text"] for seg in transcript_segments
        if seg["end"] > hook_start and seg["start"] < hook_end
    ]
    hook_frames = [f for f in frames if hook_start - 1e-6 <= f["timestamp_seconds"] <= hook_end + 1e-6]
    hook_line = " ".join(w["word"].strip() for w in hook_words).strip() or " ".join(
        t.strip() for t in hook_seg_texts).strip()
    hook_ocr = []
    for f in hook_frames:
        for ln in f.get("ocr_lines", []) or []:
            hook_ocr.append(ln["text"])

    info = dl.get("info") or {}
    return {
        "source": source,
        "info": info,
        "detail": detail,
        "full_duration": full_duration,
        "focused": focused,
        "effective_start": effective_start,
        "effective_end": effective_end,
        "effective_duration": effective_duration,
        "meta": meta,
        "frames": frames,
        "frame_meta": frame_meta,
        "detail_budget": detail_budget,
        "target": target,
        "cue_frames": cue_frames,
        "cue_meta": cue_meta,
        "transcript_segments": transcript_segments,
        "transcript_text": transcript_text,
        "transcript_source": transcript_source,
        "resolution": args.resolution,
        "hook": {
            "start": hook_start, "end": hook_end,
            "words": hook_words, "seg_texts": hook_seg_texts,
            "frames": hook_frames, "line": hook_line, "ocr": hook_ocr,
        },
        "pacing": pacing,
        "scenes": scene_data,
        "audio": audio_result,
        "stems": stem_result,
        "lyrics_segments": lyrics_segments,
        "lyrics_source": lyrics_source,
        "lyrics_text": format_transcript(lyrics_segments) if lyrics_segments else None,
        "timeline": timeline_result,
        "identify": identify_result,
        "voicecheck": voicecheck_result,
        "ocr_summary": ocr_summary,
        "subtitle_path": dl.get("subtitle_path"),
        "work": str(work),
    }


def _frame_line(frame: dict) -> str:
    base = (f"- `{frame['path']}` "
            f"(t={format_time(frame['timestamp_seconds'])}, reason={frame.get('reason', 'selected')})")
    text = (frame.get("ocr_text") or "").replace("\n", " · ").strip()
    if text:
        base += f' — on-screen: "{text}"'
    return base


def print_markdown_report(r: dict, header_level: int = 1) -> None:
    h = "#" * header_level
    info = r["info"]
    detail = r["detail"]
    focused = r["focused"]
    frames = r["frames"]
    frame_meta = r["frame_meta"]
    meta = r["meta"]

    print()
    print(f"{h} watch: video report")
    print()
    print(f"- **Source:** {r['source']}")
    if info.get("title"):
        print(f"- **Title:** {info['title']}")
    if info.get("uploader"):
        print(f"- **Uploader:** {info['uploader']}")
    if info.get("track") or info.get("artist"):
        pa = " — ".join(x for x in (info.get("track"), info.get("artist")) if x)
        print(f"- **Platform audio:** {pa} _(named by the post itself — free, no fingerprinting)_")
    print(f"- **Duration:** {format_time(r['full_duration'])} ({r['full_duration']:.1f}s)")
    # Engagement signals (whatever the platform exposed) — real ad-research data.
    eng = []
    for label, key in (("views", "view_count"), ("likes", "like_count"),
                       ("comments", "comment_count"), ("reposts", "repost_count")):
        v = info.get(key)
        if v is not None:
            eng.append(f"{int(v):,} {label}")
    if info.get("upload_date"):
        d = str(info["upload_date"])
        if len(d) == 8:
            eng.append(f"posted {d[:4]}-{d[4:6]}-{d[6:]}")
    if eng:
        print(f"- **Engagement:** {' · '.join(eng)}")
    if focused:
        print(f"- **Focus range:** {format_time(r['effective_start'])} → {format_time(r['effective_end'])} "
              f"({r['effective_duration']:.1f}s)")
    if meta.get("width") and meta.get("height"):
        print(f"- **Resolution:** {meta['width']}x{meta['height']} ({meta.get('codec') or 'unknown codec'})")
    range_mode = "focused" if focused else "full"
    print(f"- **Detail:** {detail}")
    detail_count = frame_meta.get("selected_count", 0)
    detail_budget = r["detail_budget"]
    if detail != "transcript":
        cap_label = "unlimited" if detail_budget is None else str(detail_budget)
        engine = frame_meta.get("engine", "scene")
        fallback = " with uniform fallback" if frame_meta.get("fallback") else ""
        deduped = frame_meta.get("deduped_count", 0)
        dedup_note = f", {deduped} near-duplicate{'s' if deduped != 1 else ''} dropped" if deduped else ""
        print(f"- **Frames:** {detail_count} selected from {frame_meta.get('candidate_count', detail_count)} "
              f"candidates ({engine}{fallback}{dedup_note}, {range_mode} range, budget {r['target']}, cap {cap_label})")
    elif not r["cue_frames"]:
        print("- **Frames:** skipped (transcript detail)")
    if r["cue_frames"]:
        dropped = r["cue_meta"].get("dropped_out_of_window", 0)
        drop_note = f", {dropped} dropped outside range" if dropped else ""
        print(f"- **Cue frames:** {len(r['cue_frames'])} at transcript-flagged timestamps (transcript-cue{drop_note})")
    if frames:
        print(f"- **Frame size:** max {r['resolution']}px wide, max 1998px tall")
    if r["ocr_summary"].get("available"):
        s = r["ocr_summary"]
        print(f"- **On-screen text:** {s['total_lines']} line(s) across {s['frames_with_text']} frame(s) "
              f"(OCR via {s['engine']})")
    if r["transcript_segments"]:
        in_range = " in range" if focused else ""
        print(f"- **Transcript:** {len(r['transcript_segments'])} segments{in_range} "
              f"(via {r['transcript_source'] or 'captions'})")
    else:
        print("- **Transcript:** none available")

    if detail == "token-burner" and len(frames) > 250:
        print()
        print(f"> **Warning:** token-burner detail selected {len(frames)} frames. "
              "This may use a large number of image tokens.")
    if not focused and r["full_duration"] > 600 and detail not in ("transcript", "token-burner"):
        mins = int(r["full_duration"] // 60)
        print()
        print(f"> **Warning:** This is a {mins}-minute video. Frame coverage is sparse at this length "
              f"under `{detail}` detail. Re-run with `--start/--end` to zoom in, or `--detail token-burner`.")

    # Post caption & hashtags (ad-research context from the post itself)
    caption = (info.get("caption") or "").strip()
    hashtags = info.get("hashtags") or []
    if caption or hashtags:
        print()
        print("## Post caption & hashtags")
        print()
        if caption:
            print("> " + caption.replace("\n", "\n> "))
        if hashtags:
            print()
            print("**Hashtags:** " + " ".join(hashtags))

    # HOOK
    hook = r["hook"]
    print()
    print(f"## HOOK ({format_time(hook['start'])}–{format_time(hook['end'])})")
    print()
    print("_The opening 3 seconds — the single most important window in ad analysis "
          "(scroll-stopping hook)._")
    print()
    if hook["words"]:
        print("**Spoken (word-level, absolute source time):**")
        print()
        print("```")
        for w in hook["words"]:
            print(f"{w['start']:6.2f}s – {w['end']:6.2f}s  {w['word']}")
        print("```")
        if hook["line"]:
            print()
            print(f"> Hook line: \"{hook['line']}\"")
    elif hook["seg_texts"]:
        print("**Spoken (segment-level — word timing unavailable from captions):**")
        print()
        print("```")
        print(" ".join(t.strip() for t in hook["seg_texts"]).strip())
        print("```")
    else:
        print("_No spoken words in the first 3 seconds (silent/musical open)._")
    if hook["ocr"]:
        print()
        print("**On-screen text in hook:** " + " · ".join(dict.fromkeys(hook["ocr"])))
    if r["audio"].get("available") and r["audio"].get("hook"):
        print()
        print(f"**Hook audio:** {r['audio']['hook'].get('content', 'unknown')}")
    if r["pacing"].get("available"):
        print(f"**Hook cuts:** {r['pacing'].get('hook_cuts')}")
    print()
    if hook["frames"]:
        print("**Frame(s) in the hook window** (Read these first):")
        print()
        for frame in hook["frames"]:
            print(_frame_line(frame))
    else:
        print("_No extracted frame falls in the hook window. "
              "Re-run with `--timestamps 0,1,2` to force hook frames._")

    # Pacing
    print()
    print("## Pacing")
    print()
    print(format_pacing_report(r["pacing"]))

    # Audio
    print()
    print("## Audio")
    print()
    print(format_audio_report(r["audio"]))
    if r.get("stems"):
        s = r["stems"]
        print()
        print(f"- **Stems separated** (Demucs {s.get('model')}, {s.get('device')}): "
              f"isolated vocals + music tracks")

    # Track identification (only present when --identify was used)
    if r.get("identify") is not None:
        print()
        print("## Music / track ID")
        print()
        print("_Identified via Shazam (external lookup — this is the one step that "
              "sends audio off-machine, run on request)._")
        print()
        print(format_identify_report(r["identify"]))

    # Voice check (only present when --voice-check was used)
    if r.get("voicecheck") is not None:
        print()
        print("## Voice check")
        print()
        print(f"_Measured on the {r['voicecheck'].get('source', 'audio')} "
              "(wav2vec2 AI detector + Praat acoustics + spectral warmth)._")
        print()
        print(voicecheck_mod.format_voicecheck_report(r["voicecheck"]))

    # Timeline
    if r.get("timeline", {}).get("available"):
        print()
        print("## Timeline (speech / music / singing / silence)")
        print()
        print(format_timeline(r["timeline"]))

    # Lyrics (isolated vocal stem)
    if r.get("lyrics_text"):
        print()
        print("## Lyrics / vocals")
        print()
        print(f"_Transcribed from the isolated vocal stem ({r['lyrics_source']}). "
              "Sung lyrics are approximate._")
        print()
        print("```")
        print(r["lyrics_text"])
        print("```")

    # Frames
    print()
    print("## Frames")
    print()
    if frames:
        print(f"Frames live at: `{Path(r['work']) / 'frames'}`")
        print()
        print("**Read each frame path below with the Read tool to view the image.** "
              "`on-screen:` is OCR'd text burned into that frame; `t=MM:SS` is the absolute timestamp.")
        print()
        for frame in frames:
            print(_frame_line(frame))
    else:
        print("_No frames extracted._")

    # Transcript
    print()
    print("## Transcript")
    print()
    if r["transcript_text"]:
        label = r["transcript_source"] or "captions"
        if focused:
            print(f"_Source: {label}. Filtered to {format_time(r['effective_start'])} → "
                  f"{format_time(r['effective_end'])}:_")
        else:
            print(f"_Source: {label}._")
        print()
        print("```")
        print(r["transcript_text"])
        print("```")
    elif detail == "transcript":
        print("_No transcript available at transcript detail. Re-run with `--detail balanced` for frames._")
    elif focused and r["subtitle_path"]:
        print(f"_No transcript lines fell inside {format_time(r['effective_start'])} → "
              f"{format_time(r['effective_end'])}._")
    else:
        setup_py = SCRIPT_DIR / "setup.py"
        print("_No transcript available — proceed with frames only. "
              f"Run `python3 {setup_py}` to install faster-whisper, then re-run._")

    print()
    print("---")
    print(f"_Work dir: `{r['work']}` — delete when done._")


def _voicecheck_json(vc: dict | None) -> dict | None:
    """Trim the voice-check dict for JSON (drop the bulky raw model output)."""
    if not vc:
        return None
    out = {k: v for k, v in vc.items()}
    if isinstance(out.get("ai"), dict):
        out["ai"] = {k: v for k, v in out["ai"].items() if k != "raw"}
    return out


def _json_safe_report(r: dict) -> dict:
    """Trim a report dict to a clean, serializable shape for JSON output."""
    def frame_json(f: dict) -> dict:
        return {
            "index": f.get("index"),
            "timestamp_seconds": f.get("timestamp_seconds"),
            "time": format_time(f.get("timestamp_seconds", 0)),
            "path": f.get("path"),
            "reason": f.get("reason"),
            "ocr_text": f.get("ocr_text", ""),
            "ocr_lines": f.get("ocr_lines", []),
        }
    hook = r["hook"]
    return {
        "source": r["source"],
        "title": r["info"].get("title"),
        "uploader": r["info"].get("uploader"),
        "platform_audio": {"track": r["info"].get("track"), "artist": r["info"].get("artist"),
                           "album": r["info"].get("album")},
        "engagement": {k: r["info"].get(k) for k in
                       ("view_count", "like_count", "comment_count", "repost_count",
                        "upload_date", "channel_follower_count")},
        "caption": r["info"].get("caption"),
        "hashtags": r["info"].get("hashtags") or [],
        "detail": r["detail"],
        "duration_seconds": round(r["full_duration"], 2),
        "focused": r["focused"],
        "focus_range": [round(r["effective_start"], 2), round(r["effective_end"], 2)] if r["focused"] else None,
        "resolution": {"width": r["meta"].get("width"), "height": r["meta"].get("height"),
                       "codec": r["meta"].get("codec")},
        "hook": {
            "window": [round(hook["start"], 2), round(hook["end"], 2)],
            "spoken_line": hook["line"],
            "words": hook["words"],
            "on_screen_text": list(dict.fromkeys(hook["ocr"])),
            "frames": [frame_json(f) for f in hook["frames"]],
            "audio": r["audio"].get("hook", {}),
            "cuts": r["pacing"].get("hook_cuts"),
        },
        "pacing": r["pacing"],
        "scenes": r.get("scenes"),
        "audio": {k: v for k, v in r["audio"].items() if k != "loudness_curve"},
        "audio_loudness_curve": r["audio"].get("loudness_curve", []),
        "stems": r.get("stems"),
        "timeline": r.get("timeline"),
        "identify": r.get("identify"),
        "voicecheck": _voicecheck_json(r.get("voicecheck")),
        "lyrics_source": r.get("lyrics_source"),
        "lyrics_segments": r.get("lyrics_segments", []),
        "ocr_summary": r["ocr_summary"],
        "transcript_source": r["transcript_source"],
        "transcript_segments": r["transcript_segments"],
        "frames": [frame_json(f) for f in r["frames"]],
        "work_dir": r["work"],
    }


def print_compare_summary(reports: list[dict]) -> None:
    print()
    print("# watch: comparison")
    print()
    print(f"Comparing **{len(reports)} videos**. Hook-by-hook summary first, then each full report below.")
    print()
    print("| # | Duration | Hook cuts | Cuts/sec | Hook audio | Hook on-screen text | Hook spoken line |")
    print("|---|----------|-----------|----------|------------|---------------------|------------------|")
    for i, r in enumerate(reports, 1):
        hook = r["hook"]
        pac = r["pacing"]
        aud = r["audio"].get("hook", {}) if r["audio"].get("available") else {}
        ocr_txt = " · ".join(dict.fromkeys(hook["ocr"]))[:60] or "—"
        spoken = (hook["line"] or "—")[:60]
        dur = f"{format_time(r['full_duration'])}"
        hook_cuts = pac.get("hook_cuts") if pac.get("available") else "—"
        cps = pac.get("cuts_per_second") if pac.get("available") else "—"
        content = aud.get("content", "—")
        print(f"| {i} | {dur} | {hook_cuts} | {cps} | {content} | {ocr_txt} | {spoken} |")


def main() -> int:
    ap = build_arg_parser()
    args = ap.parse_args()
    config = get_config()

    reports: list[dict] = []
    for i, src in enumerate(args.source):
        if len(args.source) > 1:
            print(f"[watch] === video {i + 1}/{len(args.source)}: {src} ===", file=sys.stderr)
        reports.append(build_report(src, args, config, index=i))

    if args.report_json:
        payload = [_json_safe_report(r) for r in reports]
        out = payload if len(reports) > 1 else payload[0]
        Path(args.report_json).expanduser().write_text(
            json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[watch] wrote JSON report to {args.report_json}", file=sys.stderr)

    if args.json_stdout:
        payload = [_json_safe_report(r) for r in reports]
        out = payload if len(reports) > 1 else payload[0]
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if len(reports) > 1:
        print_compare_summary(reports)
        for i, r in enumerate(reports, 1):
            print()
            print(f"# Video {i}")
            print_markdown_report(r, header_level=2)
    else:
        print_markdown_report(reports[0], header_level=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
