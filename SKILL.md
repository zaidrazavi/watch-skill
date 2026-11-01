---
name: watch
version: "0.3.0"
description: Watch a video (URL or local path). Downloads with yt-dlp, extracts auto-scaled frames with ffmpeg, pulls the transcript from captions (or fully local faster-whisper transcription — no API key, no cost), labels the opening 3-second ad hook, and hands the result to Claude so it can answer questions about what's in the video.
argument-hint: "<video-url-or-path> [question]"
allowed-tools: Bash, Read, AskUserQuestion
homepage: https://github.com/zaidrazavi/watch-skill
repository: https://github.com/zaidrazavi/watch-skill
author: zaidrazavi
license: MIT
user-invocable: true
---

# /watch

You don't have a video input; this skill gives you one. A Python script gets captions first, optionally downloads the video, extracts frames as JPEGs (scene-aware, or fast keyframes at `efficient` detail), gets a timestamped transcript (native captions first, then **fully local** faster-whisper transcription as fallback — no API key, no cost), calls out the opening 3-second **ad hook** with word-level timing, and prints frame paths. You then `Read` each frame path to see the images and combine them with the transcript to answer the user.

## Resolve `SKILL_DIR` (do this before any command)

Every `python3 ...` command below runs a bundled script under `SKILL_DIR/scripts/`. Set `SKILL_DIR` to the **absolute path of the directory containing THIS SKILL.md you just Read** — your harness told you that path in the Read result. The scripts are always a direct sibling of this file (`SKILL_DIR/scripts/watch.py`), in every install layout:

```
Read ~/.claude/skills/watch/SKILL.md                                          → SKILL_DIR=~/.claude/skills/watch
Read ~/.codex/skills/watch/SKILL.md                                          → SKILL_DIR=~/.codex/skills/watch
Read ~/.agents/skills/watch/SKILL.md                                         → SKILL_DIR=~/.agents/skills/watch
```

Substitute that literal path for `${SKILL_DIR}` in every command. This works on every harness (Claude Code, Codex, Cursor, Gemini CLI, …) without relying on any harness-specific environment variable. Guard once at the start of a run:

```bash
SKILL_DIR="<absolute path of the directory containing the SKILL.md you Read>"
if [ ! -f "$SKILL_DIR/scripts/watch.py" ]; then
  echo "ERROR: scripts/watch.py not found under SKILL_DIR=$SKILL_DIR" >&2
  echo "Re-check the directory of the SKILL.md you Read and substitute it as SKILL_DIR." >&2
  exit 1
fi
```

## Step 0 — Setup preflight (runs every `/watch` invocation, silent on success)

**Python interpreter:** every `python3 ...` command in this skill is for macOS/Linux. On **Windows**, substitute `python` — the `python3` command on Windows is the Microsoft Store stub and will not run the script.

On the first `/watch` invocation in a session, use structured preflight so you can detect first-run setup:

```bash
python3 "${SKILL_DIR}/scripts/setup.py" --json
```

Branch on two fields:

- **`can_proceed: true` and `first_run: false`** → setup is already done. Proceed to Step 1 without comment.
- **`first_run: true`** → genuine first-time setup. Do these in order:
  1. If `missing_binaries` is non-empty **or** `has_faster_whisper` is false, run the installer first (it auto-installs ffmpeg/yt-dlp on macOS and always pip-installs faster-whisper — see below) and confirm everything lands. **Do not skip this and jump to preferences.**
  2. Run the installer once more if needed so it scaffolds `~/.config/watch/.env` (it only writes the template when the file is absent, so let it create the file *before* you write any values into it).
  3. Ask the watch-preference question below, then write the selected value into `~/.config/watch/.env` and set `SETUP_COMPLETE=true`.
- **`can_proceed: false` and `first_run: false`** → setup was finished before but the environment regressed (e.g. `missing_binaries` after an OS change, or the Python env was reset). Run the installer to remediate, then proceed. Don't re-ask preferences.

Transcription is **100% local and free** (faster-whisper) — there is no API key to configure and nothing to encourage. Setup only needs the binaries and the faster-whisper package, both of which the installer handles automatically.

On follow-up `/watch` calls in the same session, use the silent check:

```bash
python3 "${SKILL_DIR}/scripts/setup.py" --check
```

This is a <100ms lookup. Exit 0 means /watch can run. On exit 0 the script emits **nothing** — proceed to Step 1 without comment. **Do NOT announce "setup is complete" to the user** — they don't need a status message on every turn. The only acceptable user-visible output from Step 0 is when remediation is required.

On non-zero exit, follow the table:

| Exit | Meaning | Action |
|------|---------|--------|
| `2` | Missing dependency — a binary (`ffmpeg` / `ffprobe` / `yt-dlp`) and/or the `faster-whisper` package | Run installer |

The installer is idempotent — safe to re-run:

```bash
python3 "${SKILL_DIR}/scripts/setup.py"
```

On macOS with Homebrew, it auto-installs `ffmpeg` and `yt-dlp`; on Linux/Windows it prints the exact commands for those two. It **always** pip-installs `faster-whisper` (the local transcription engine) into the current Python on every platform — no user action needed. It scaffolds `~/.config/watch/.env` with commented defaults at `0600` perms.

**First-run watch preference:** after the installer has scaffolded `~/.config/watch/.env`, use `AskUserQuestion` to ask one question:

- Default detail (one dial). Present these as `AskUserQuestion` options in this exact order — lightest to heaviest — and keep `(recommended)` on `balanced` even though it is not first (do **not** reorder to put the recommended option first):
  - `transcript` — no frames at all, transcript only (skips video download when captions exist).
  - `efficient` — fast keyframe pass (cap 50).
  - `balanced` (recommended) — scene-aware frames (cap 100, default).
  - `token-burner` — scene-aware, uncapped (maximum fidelity; high token cost).

Write the answer directly into `~/.config/watch/.env` by setting the bare key on its own line — **no trailing inline comment** (a `# note` after the value can break parsing):

```bash
WATCH_DETAIL=balanced
```

Use the user's selected value. If they skip the question, keep the recommended default. Once dependencies, the API-key choice, and this preference are handled, write or update `SETUP_COMPLETE=true` in the same file. Do not ask this preference question again when `SETUP_COMPLETE=true`.

**Structured mode (optional):** `python3 "${SKILL_DIR}/scripts/setup.py" --json` emits `{status, can_proceed, first_run, setup_complete, missing_binaries, missing_packages, has_faster_whisper, config_file, watch_detail, platform}` where `status` is one of `ready | needs_install`. `can_proceed` is the operational gate (all binaries present AND faster-whisper importable). Branch on `can_proceed`/`first_run` to decide whether to run and whether to walk first-run setup.

**Whisper model quality (optional):** the local model size is set by `WHISPER_MODEL` in `~/.config/watch/.env` (`tiny | base | small | medium | large-v3`). It defaults to `medium` on CPU / `large-v3` on GPU, which is a good balance for ad research — you do **not** need to ask the user about it during setup. If a teammate wants faster (lower accuracy) or maximum accuracy, they can set `WHISPER_MODEL=` themselves, or you can pass `--whisper-model` for a single run.

Within a single session, you can skip Step 0 on follow-up `/watch` calls — once `--check` returned 0, nothing about the environment changes between turns.

## When to use

- User pastes a video URL (YouTube, Vimeo, X, TikTok, Twitch clip, most yt-dlp-supported sites) and asks about it.
- User points at a local video file (`.mp4`, `.mov`, `.mkv`, `.webm`, etc.) and asks about it.
- User types `/watch <url-or-path> [question]`.

## Recommended limits

- **Best accuracy: videos under 10 minutes.** Frame coverage scales inversely with duration.
- **Universal rate cap: 2 fps.** The script never samples faster than 2 fps, even when a budget or `--fps` would imply more.
- **The frame ceiling is set by the detail mode** (`WATCH_DETAIL` in `~/.config/watch/.env`, or `--detail`), not a single global cap:
  - `transcript` → no frames
  - `efficient` → up to **50** (keyframes)
  - `balanced` (default) → up to **100** (scene-aware)
  - `token-burner` → **uncapped** (scene-aware; a soft warning prints past 250 frames)
  - `--max-frames N` overrides whichever cap the mode would otherwise use.
- **Full-video frame budget by duration.** Token cost grows with frame count, so the script targets a budget by duration. This budget sets the fps and the uniform-sampling fallback; scene-aware selection can fill up to the detail cap above, whichever is lower:
  - ≤30s → ~12-30 frames
  - 30s-1min → ~40 frames
  - 1-3min → ~60 frames
  - 3-10min → ~80 frames
  - \>10min → up to the detail cap, sparsely spaced (warning printed)
- If the user hands you a long video, consider asking whether they want a specific section before burning tokens on a sparse scan.

## How to invoke

**Step 1 — parse the user input.** Separate the video source (URL or path) from any question the user asked. Example: `/watch https://youtu.be/abc what language is this in?` → source = `https://youtu.be/abc`, question = `what language is this in?`.

**Step 2 — run the watch script.** Pass the source verbatim. Do not shell-escape it yourself beyond normal quoting:

```bash
python3 "${SKILL_DIR}/scripts/watch.py" "<source>"
```

Optional flags:
- `--detail transcript|efficient|balanced|token-burner` — fidelity/speed dial. `transcript` = no frames (transcript only, skips video download when captions exist); `efficient` = fast keyframes (cap 50); `balanced` = scene-aware frames (cap 100); `token-burner` = scene-aware, uncapped.
- `--start T` / `--end T` — focus on a section. Accepts `SS`, `MM:SS`, or `HH:MM:SS`. When either is set, fps auto-scales denser (see "Focusing on a section" below).
- `--timestamps T1,T2,…` — grab a frame at each of these absolute timestamps (`SS`, `MM:SS`, or `HH:MM:SS`). Use this after reading the transcript to capture deictic moments the presenter flags ("look here", "as you can see", "notice this") that visual selection alone may miss. See "Transcript-cue frames" below.
- `--max-frames N` — override the preset cap for tighter token budget (e.g. `--max-frames 40`)
- `--resolution W` — change frame width in px (default 512; bump to 1024 only if the user needs to read on-screen text)
- `--fps F` — override auto-fps (clamped to 2 fps max)
- `--out-dir DIR` — keep working files somewhere specific (default: an auto-generated tmp dir)
- `--whisper-model tiny|base|small|medium|large-v3` — override the local model size for this run (default: `WHISPER_MODEL` in `.env`, else `medium` on CPU / `large-v3` on GPU)
- `--no-whisper` — disable local transcription entirely (frames-only if no captions)
- `--no-dedup` — keep near-duplicate frames. By default a frame-delta pass drops frames that are visually near-identical to the previous kept one (held slides, static screen recordings, paused video) so the frame budget goes to distinct content; the report's **Frames** line notes how many were dropped. Pass this only if the user needs every sampled frame (e.g. judging subtle frame-to-frame motion).

### Focusing on a section (higher frame rate)

When the user asks about a specific moment — "what happens at the 2 minute mark?", "zoom into 0:45 to 1:00", "the first 10 seconds" — pass `--start` and/or `--end`. The script switches to focused-mode budgets, which are denser than full-video budgets (still capped at 2 fps, and still bounded by the detail-mode cap — the counts below assume the default `balanced` cap of 100; `efficient` tops out at 50):

- ≤5s → 2 fps (up to 10 frames)
- 5-15s → 2 fps (up to 30 frames)
- 15-30s → ~2 fps (up to 60 frames)
- 30-60s → ~1.3 fps (up to 80 frames)
- 60-180s → ~0.6 fps (100 frames, capped)

Focused mode is the right call for:
- Any moment/range the user names explicitly ("around 2:30", "the intro", "the last 30 seconds").
- Any video longer than ~10 minutes where the user's question is about a specific part — running focused on the relevant section is far more useful than a sparse scan of the whole thing.
- Re-runs after a full scan didn't have enough detail in some region.

Transcript is auto-filtered to the same range. Frame timestamps are absolute (real video timeline, not offset-from-start).

Examples:
```bash
# Last 10 seconds of a 1 minute video
python3 "${SKILL_DIR}/scripts/watch.py" video.mp4 --start 50 --end 60

# Zoom into 2:15 → 2:45 at 2 fps (60 frames)
python3 "${SKILL_DIR}/scripts/watch.py" "$URL" --start 2:15 --end 2:45 --fps 2

# From 1h12m to the end of the video
python3 "${SKILL_DIR}/scripts/watch.py" "$URL" --start 1:12:00
```

**Step 3 — Read every frame path the script lists.** The Read tool renders JPEGs directly as images for you. Read all frames in a single message (parallel tool calls) so you see them together. The frames are in chronological order with a `t=MM:SS` timestamp so you can align them to the transcript.

**Step 4 — answer the user.** You now have two streams of evidence:
- **Frames** — what's on screen at each timestamp
- **Transcript** — what's said at each timestamp. The report's header shows the source (`captions` = yt-dlp pulled native subs; `faster-whisper <model> (cpu|cuda)` = transcribed locally).
- **Hook** — the report calls out the first 3 seconds in a dedicated `## HOOK` section with word-level timing and the frame(s) in that window. For ad research, lead your answer with the hook: what's said and shown in the opening scroll-stopping moment.

If the user asked a specific question, answer it directly citing timestamps. If they didn't ask anything, summarize what happens in the video — structure, key moments, notable visuals, spoken content.

This holds for `transcript` detail too: even with no frames, produce a **summary** like the other modes — do not paste the full transcript into chat. Synthesize structure, key moments, and spoken content with timestamps; quote only the lines that matter. Offer the raw transcript only if the user explicitly asks for it.

**Step 5 — clean up.** The script prints a working directory at the end. If the user isn't going to ask follow-ups about this video, delete it with `rm -rf <dir>`. If they might, leave it in place.

## Detail and frames

Default behavior comes from `~/.config/watch/.env`:

- `WATCH_DETAIL=transcript|efficient|balanced|token-burner` (default: `balanced`)

At `transcript` detail, captions are enough to return a report without downloading video. If captions are missing, the script downloads audio only and transcribes it locally with faster-whisper. If no transcript can be produced, it reports the limitation clearly; re-run with `--detail balanced` for frames.

At `efficient` detail, the script downloads the video and extracts **keyframes only** (`ffmpeg -skip_frame nokey`) — a near-instant pass that lands frames on scene cuts. If a clip has fewer than 4 keyframes it falls back to uniform sampling.

At `balanced` / `token-burner` detail, the script extracts **scene-aware** frames: ffmpeg scene-change selection first, falling back to uniform sampling only when the video is effectively static. `balanced` caps at 100 frames; `token-burner` is uncapped. Frame report lines include both timestamp and selection reason. Extracted images are clamped to a maximum 1998px height for Claude Read compatibility.

## Transcript-cue frames

Visual frame selection (scene/keyframe) can miss the moments a presenter explicitly flags — "look here", "as you can see", "notice this", "watch what happens" — because pointing at a slide is often a *low* visual change. `--timestamps` lets you force a frame at those exact moments. **You** decide which moments matter, by reading the transcript:

1. Run once at `--detail transcript` (or any detail) to get the timestamped transcript.
2. Scan it for deictic cues — phrases where the speaker directs attention to something on screen. This is a judgment call (ignore rhetorical "look, the point is…"); that's why it's done by you, not a regex.
3. Re-run with `--timestamps 4:32,7:10,9:55` (absolute source times). For a URL, point the second run at the **downloaded local file** in the work dir so it doesn't re-download.

Behavior:
- **Additive by default.** Cue frames (`reason=transcript-cue`) are merged into whatever `--detail` already selected, in chronological order.
- **Pinned and counted first.** Cue frames are reserved against the frame cap before the detail engine runs, so they're never evicted by even-sampling.
- **Honors focus mode.** With `--start/--end`, any cue timestamp outside the window is dropped (reported in the summary). Coordinates are always absolute source time.
- **Cue-only frames.** `--detail transcript --timestamps …` skips scene/keyframe sampling and returns *only* the cue frames (it will download the video to do so, since frames need pixels).

## Local Transcription

The script gets a timestamped transcript in one of two ways:

1. **Native captions (free, preferred).** yt-dlp pulls manual or auto-generated subtitles from the source platform if available.
2. **Local transcription (free, no API key).** If no captions came back (or the source is a local file with no subtitles), the script extracts audio (`ffmpeg -vn -ac 1 -ar 16000 -b:a 64k`, ~0.5 MB/min) and transcribes it **fully offline** with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (a CTranslate2 Whisper implementation, MIT-licensed):
   - **Zero cost, zero API key, nothing leaves the machine.** The audio is never uploaded anywhere.
   - **Auto hardware detection.** Uses a CUDA GPU (`float16`) when present, otherwise quantized CPU inference (`int8`) which keeps it fast.
   - **One-time model download.** On first use the model auto-downloads to `~/.cache/huggingface` (~1.5 GB for the default `medium`) and is cached forever — every run after that is fully offline. A one-line "[watch] downloading … one-time only" notice prints to stderr so the delay isn't confusing.
   - **Model size is configurable** via `WHISPER_MODEL` in `~/.config/watch/.env` (`tiny | base | small | medium | large-v3`), defaulting to `medium` on CPU and `large-v3` on GPU. Override per-run with `--whisper-model`.
   - **VAD-filtered + word-level timestamps.** Silence is skipped (faster, and avoids hallucinated text in silent gaps), and word-level timing is captured — which powers the `## HOOK` section (see below). Language is auto-detected.

Use `--no-whisper` to skip local transcription entirely (frames-only if no captions).

### Ad hook analysis (first 3 seconds)

Because /watch is built for ad research, the report includes a dedicated **`## HOOK (0:00–0:03)`** section above the normal frames/transcript. It combines the exact **word-level** transcript of the opening 3 seconds (each word with its start/end in seconds) with the extracted frame(s) that fall in that window — the single most important moment in any ad. Word-level timing is available whenever the transcript came from local transcription (native captions are typically only line-level; the hook then falls back to segment text). To force a frame exactly on the hook, re-run with `--timestamps 0,1,2` or `--start 0 --end 3`.

## Failure modes and handling

- **Setup preflight failed** → run `python3 "${SKILL_DIR}/scripts/setup.py"` (auto-installs ffmpeg/yt-dlp via brew on macOS, pip-installs faster-whisper on every platform, scaffolds the `.env`).
- **faster-whisper not installed** → local transcription raises a "faster-whisper is not installed" error to stderr and the report falls back to frames-only. Run the installer (`python3 "${SKILL_DIR}/scripts/setup.py"`) to pip-install it, then re-run.
- **No transcript available** → captions missing AND (`--no-whisper` was passed, OR the video has no audio track, OR faster-whisper isn't installed). Script prints a hint pointing to setup. Proceed frames-only and tell the user.
- **First-run model download is slow** → the first transcription downloads the model (~1.5 GB for `medium`); a "[watch] downloading … one-time only" line prints to stderr. This happens once, then it's cached. If bandwidth is a concern, set `WHISPER_MODEL=small` (or `base`/`tiny`) in `~/.config/watch/.env`.
- **Transcription is slow on CPU for long videos** → a large model on CPU is thorough but not instant. Set a smaller `WHISPER_MODEL`, or focus on a section with `--start`/`--end`. A CUDA GPU is detected and used automatically when present.
- **Long video warning printed** → acknowledge it in your answer. Offer to re-run focused on a specific section via `--start`/`--end` rather than a sparse full-video scan.
- **Download fails** → yt-dlp's error goes to stderr. If it's a login-required or region-locked video, tell the user plainly; do not keep retrying.

## Token efficiency

This skill burns tokens primarily on frames. Order of magnitude:
- 80 frames at 512px wide is roughly 50-80k image tokens depending on aspect ratio.
- The transcript is cheap (a few thousand tokens at most for a 10-minute video).
- Bumping `--resolution` to 1024 roughly quadruples the image tokens per frame. Only do it when necessary.

If you already watched a video this session and the user asks a follow-up, do **not** re-run the script — you already have the frames and transcript in context. Just answer from what you have.

## Security & Permissions

**What this skill does:**
- Runs `yt-dlp` locally to download the video and pull native captions when the source supports them (public data; the request goes directly to whatever host the URL points at)
- Runs `ffmpeg` / `ffprobe` locally to extract frames as JPEGs and, when transcription is needed, a mono 16 kHz audio clip
- Transcribes that audio clip **fully locally** with faster-whisper — the audio never leaves the machine and no API key is involved
- On the first transcription only, downloads the Whisper model weights from Hugging Face (`huggingface.co`) into `~/.cache/huggingface`; cached and fully offline thereafter
- Writes the downloaded video, frames, audio, and an intermediate transcript to a working directory under the system temp dir (or `--out-dir` if specified) so Claude can `Read` them
- Reads / creates `~/.config/watch/.env` (mode `0600`) to store watch preferences (`WATCH_DETAIL`, optional `WHISPER_MODEL`) and a `SETUP_COMPLETE` marker. As a fallback, also reads `.env` in the current working directory

**What this skill does NOT do:**
- Does not upload the video or the audio to any transcription API — all transcription runs locally on this machine
- Does not require, store, or transmit any API key — there is none
- Does not access any platform account (no login, no session cookies, no posting) — yt-dlp only ever requests public data
- Does not persist anything outside the working directory, `~/.config/watch/.env`, and the one-time Hugging Face model cache — clean up the working directory when you're done (Step 5)

**Bundled scripts:** `scripts/watch.py` (entry point), `scripts/download.py` (yt-dlp wrapper), `scripts/frames.py` (ffmpeg frame extraction), `scripts/transcribe.py` (caption parsing + transcript formatting), `scripts/local_whisper.py` (fully local faster-whisper transcription), `scripts/setup.py` (preflight + installer)

Review scripts before first use to verify behavior.
