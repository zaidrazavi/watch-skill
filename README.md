# Watch — AI Video-Understanding Skill for Claude

A Claude Code skill that lets Claude **watch** a video. Give it a URL or a local file. It pulls frames, a timestamped transcript, the on-screen text, an audio profile, and cut pacing, and Claude answers questions about what's in the video.

Built for ad research: it labels each ad's **opening 3-second hook** with word-level timing, reads the words burned into the pixels, and profiles the hook's audio and cut rate — so you can break down competitor ads in seconds.

## What it does

- **Frames:** extracts frames with FFmpeg (scene-aware, or fast keyframes), auto-scaled for Claude.
- **Transcript:** uses the video's own captions when they exist. When they don't, it transcribes **fully locally** with faster-whisper.
- **On-screen text (OCR):** reads the burned-in captions, CTAs, and prices off each frame with local OCR (RapidOCR) — the words a 512px frame is too small to read directly.
- **Audio intelligence:** integrated loudness (LUFS), silence gaps, a speech / music / silence content mix, and whether the hook is voice-led or music/sfx and how much louder it is than the rest.
- **Pacing / cut metrics:** total cuts, cuts per second with a pace label, average shot length, and the number of cuts inside the hook window.
- **Ad hook:** labels the first 3 seconds with word-level timestamps, hook on-screen text, hook audio, and hook cut count.
- **Multi-video compare:** pass several URLs/paths at once for a side-by-side hook comparison table.
- **Structured JSON:** `--json` / `--report-json` emit the whole analysis as JSON for another tool or skill to consume.
- **Detail levels:** `transcript`, `efficient`, `balanced` and `token-burner` trade speed against detail.

Everything runs **locally and free** — no API key, and nothing (video, audio, or frames) ever leaves your machine.

## Local transcription with model fallback

Transcription starts at the **best** Whisper model (`large-v3`) and automatically **cascades down** — `large-v3 → medium → small → base → tiny` — if a model can't load or run (out-of-memory on a smaller GPU, a failed weight download, a missing CUDA runtime). For each model it tries the GPU first, then CPU. A strong machine gets maximum accuracy; a weaker one still succeeds on a smaller fallback instead of returning nothing. Pin a starting model with `WHISPER_MODEL=` in `~/.config/watch/.env` or `--whisper-model` per run.

## What I changed from the original

This is a reworked version of an open-source skill (credited below). My changes:

- **Removed the paid cloud transcription** (Groq / OpenAI Whisper APIs) and replaced it with local, open-source **faster-whisper** — no API key, no usage cost, audio never leaves your machine.
- **Added a largest-to-smallest model fallback cascade** so transcription always uses the best model the hardware can run.
- **Added on-screen text OCR, audio intelligence, and cut/pacing metrics** — the three evidence streams ad research needs beyond frames and speech.
- **Added multi-video compare and structured JSON output.**
- **Improved timestamp precision** for ad-hook analysis, with word-level timing on the opening hook.
- **Removed the API-key setup prompts.** First run auto-downloads and caches the models, then works offline.

## Tech

Python · FFmpeg · yt-dlp · faster-whisper · RapidOCR

## Setup

Requires Python 3, FFmpeg and yt-dlp. Put the folder at `~/.claude/skills/watch/`. On first use, `scripts/setup.py` checks your setup and installs what's missing (including optional OCR support — best-effort, never required).

Usage in Claude Code:

```
/watch <video-url-or-path> [more-urls…] [question]
```

## Credits

Based on [claude-video](https://github.com/bradautomates/claude-video) by Bradley Bonanno, released under the MIT License. The original copyright notice is kept in [LICENSE](LICENSE).
