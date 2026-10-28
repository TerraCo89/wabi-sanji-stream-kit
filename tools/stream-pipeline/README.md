# wabisanji-post: the post-stream pipeline

Turns a finished stream recording into publishable pieces: segments (one per game or
activity), transcripts, clip suggestions, captioned 9:16 Shorts, YouTube episodes with
chapters, descriptions and thumbnails, Instagram captions, and a metrics report.

Two human gates keep it honest: `session.yaml` (the segments) and `clips.yaml` (the
clips) must be approved before anything renders or publishes. Nothing is ever published
public automatically. The design is in `docs/post-stream-spec.md`, and the Claude Code
skills in `.claude/skills/stream-*` drive it (`/stream-post` is the entry point).

## Install and run

```bash
uv sync                          # add --extra gpu (CUDA Whisper) and/or --extra cutout (rembg)
uv run python pipeline.py --help
uv run python pipeline.py stream-status
```

From the repo root: `uv run --directory tools/stream-pipeline python pipeline.py stream-<cmd>`.
Needs ffmpeg and ffprobe on PATH.

## Settings

Everything personal comes from environment variables or the repo-root `.env`
(`src/wabisanji_post/settings.py`):

| Setting | Default |
|---|---|
| `WS_RECORDINGS_ROOT` | `~/Videos` |
| `WS_SESSIONS_ROOT` | `<recordings>/WabiSanji/sessions` |
| `WS_CONTENT_ROOT` | `<repo>/content` (series.yaml, memes.yaml) |
| `WS_PARSER_DIR` | `<repo>/apps/screenshot-parser` |
| `WABISANJI_MEMES` | the parser config's `stream.live.memes_dir` |
| `WS_TIMEZONE` | the system time zone |
| `WS_FAMILY_PATTERN` | `\b(kiddo\|kids)\b` |
| `WS_STREAMER_NAME` | `the streamer` |
| `WS_TWITCH_LOGIN`, `WS_CHANNEL_NAME`, `WS_DISCORD_URL` | the Wabi Sanji channel |
| `WS_LAYOUTS` | the packaged `layouts.yaml` |
| `WS_WHISPER_DEVICE` | `auto` (CUDA, else CPU) |
| `RECORDER_URL`, `RECORDER_TOKEN` | unset (the chat recorder is optional) |

API credentials for Twitch, YouTube, Instagram and Discord also go in `.env`
(see `.env.example`).

## Tests

```bash
uv run pytest
```

Tests that render tiny videos skip themselves when ffmpeg isn't on PATH.
