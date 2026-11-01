# Watch — AI Video-Understanding Skill for Claude

A Claude Code skill that lets Claude **watch** a video. Give it a URL or a local file. It pulls frames and a timestamped transcript, and Claude answers questions about what's in the video.

Built for ad research: it labels each ad's **opening 3-second hook** with word-level timing, so you can break down competitor ads in seconds.

## What it does

- **Frames:** extracts frames with FFmpeg (scene-aware, or fast keyframes), auto-scaled for Claude.
- **Transcript:** uses the video's own captions when they exist. When they don't, it transcribes **fully locally** with faster-whisper.
- **Ad hook:** labels the first 3 seconds with word-level timestamps.
- **Detail levels:** `transcript`, `efficient`, `balanced` and `token-burner` trade speed against detail.

## What I changed from the original

This is a reworked version of an open-source skill (credited below). My changes:

- **Removed the paid cloud transcription** (Groq / OpenAI Whisper APIs) and replaced it with local, open-source **faster-whisper**. It needs no API key and has no usage cost, and the audio never leaves your machine.
- **Improved timestamp precision** for ad-hook analysis, with word-level timing on the opening hook.
- **Removed the API-key setup prompts.** First run now auto-downloads and caches the Whisper model, and it works offline after that.

## Tech

Python · FFmpeg · yt-dlp · faster-whisper

## Setup

Requires Python 3, FFmpeg and yt-dlp. Put the folder at `~/.claude/skills/watch/`. On first use, `scripts/setup.py` checks your setup and installs what's missing.

Usage in Claude Code:

```
/watch <video-url-or-path> [question]
```

## Credits

Based on [claude-video](https://github.com/bradautomates/claude-video) by Bradley Bonanno, released under the MIT License. The original copyright notice is kept in [LICENSE](LICENSE).
