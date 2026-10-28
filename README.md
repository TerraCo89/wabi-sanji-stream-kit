# Wabi Sanji stream kit

The tools behind the [Wabi Sanji](https://www.twitch.tv/wabisanji) stream, shared so you can
borrow what's useful for your own. It's a working setup, not a polished product: expect to
read some code and adapt it. Questions are welcome in the
[Discord](https://discord.gg/7E9mJQ795K), but there's no promise of support.

Built and run with [Claude Code](https://claude.com/claude-code), which drives most of the
workflows below through the skills in `.claude/`.

## What's in here

| Part | Where | What it does |
|---|---|---|
| Stream overlay and Japanese capture | `apps/screenshot-parser` (`wabisanji`) | Screenshots of a game in Japanese become vocabulary cards in Anki (AnkiConnect), shown live in an OBS overlay. OCR with manga-ocr or a local Ollama vision model; `/translate` lets Claude read the screenshots instead while streaming. |
| ARIA, the on-stream assistant | `apps/screenshot-parser` (`wabisanji listen`, `wabisanji live`) | Transcribes your mic on the CPU with Whisper. "Hey ARIA, how do I…" questions go to Claude, which searches a guide and answers on screen and out loud (Speaker.bot). Saying a meme's catchphrase pops the meme instantly; Claude picks looser fits from the transcript. |
| Meme library | `content/stream/memes.yaml` | About 90 memes with their trigger phrases, when they fit, and where each clip came from. No media is included; rebuild your own with `wabisanji live fetch`. |
| Post-stream pipeline | `tools/stream-pipeline` (`wabisanji_post`) | A finished stream recording becomes segments, transcripts, clip suggestions, captioned 9:16 Shorts, YouTube episodes with chapters and thumbnails, Instagram captions, and metrics. Two human approval gates; nothing is ever published public without you. |
| Claude Code workflows | `.claude/commands`, `.claude/skills` | `/aria watch`, `/translate`, `/stream-post`, `/stream-moments`, `/stream-shorts`, `/stream-vods`, `/stream-memes`, `/stream-social`, `/stream-status` and friends. |
| Docs | `docs/` | How the pieces fit: OBS setup, ARIA setup, the post-stream design, and the content rules the episodes follow. |

## Requirements

- Windows first (that's where it runs). Most of the Python works elsewhere, but the helper
  scripts and some paths assume Windows.
- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- [ffmpeg](https://ffmpeg.org/) on PATH
- OBS Studio with obs-websocket
- Optional, per feature: Anki with AnkiConnect, Ollama, Speaker.bot (voice), an NVIDIA GPU
  (faster Whisper), yt-dlp (meme fetching, installed with the pipeline)

## Quick start

```bash
cp .env.example .env                                   # paths, channel names, API keys
cp apps/screenshot-parser/config.example.yaml apps/screenshot-parser/config.yaml

# Live tools: overlay + ARIA
uv sync --directory apps/screenshot-parser --extra stream --extra listen
uv run --directory apps/screenshot-parser wabisanji serve    # overlay on http://127.0.0.1:8770

# Post-stream pipeline
uv sync --directory tools/stream-pipeline                     # add --extra gpu / --extra cutout as needed
uv run --directory tools/stream-pipeline python pipeline.py stream-status
```

Then open the repo in Claude Code and try `/stream-status` or `/aria watch`.

## Making it yours

Settings come from environment variables or `.env` (see `.env.example`); nothing personal is
hard-coded. The main ones:

| Setting | What it's for |
|---|---|
| `WS_RECORDINGS_ROOT`, `WS_SESSIONS_ROOT` | Where OBS saves recordings, and where the pipeline keeps its working folders |
| `WABISANJI_MEMES` | Folder holding your meme clips |
| `WS_TIMEZONE` | Your time zone (IANA name), for stream times |
| `WS_STREAMER_NAME` | How the tools refer to you in prompts |
| `WS_TWITCH_LOGIN`, `WS_CHANNEL_NAME`, `WS_DISCORD_URL` | Your channel, used in descriptions, tags and badges |
| `WS_FAMILY_PATTERN` | Words that mean family is on stream, so innuendo memes stay off |
| `WS_LAYOUTS` | Crop layouts for your OBS scenes (Shorts and thumbnails) |
| `RECORDER_URL` | Optional chat recorder (if you run one) |

Also worth changing: `content/stream/series.yaml` (your series and playlists), the caption
colours in `tools/stream-pipeline/src/wabisanji_post/render.py`, and the badge in `badge.py`.

## Memes

`memes.yaml` lists each meme's source (a YouTube link, myinstants page or GIPHY clip) next to
its file name. The clips themselves belong to their owners and aren't included. To add one:

```bash
uv run --directory apps/screenshot-parser wabisanji live fetch "ytsearch:oh no anyway" --expect "oh no anyway" --id oh-no-anyway
```

`--expect` has Whisper find the line and trims tightly around it. Check the `risk` field before
putting memes into published videos: TV and film audio can trigger Content ID.

## Licence

MIT, see `LICENSE`. Bundled fonts keep their own licences (DM Sans: SIL Open Font Licence,
`tools/stream-pipeline/src/wabisanji_post/fonts/OFL.txt`).

Follow along on [Twitch](https://www.twitch.tv/wabisanji) and
[YouTube](https://www.youtube.com/channel/UCIGEn5URYh8mS-A-vJUe-7g).
