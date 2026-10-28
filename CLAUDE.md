# Wabi Sanji stream kit

Tools for a Japanese-learning-through-games stream: a screenshot parser that feeds Anki, an
on-stream voice assistant (ARIA) with a meme bot, and a post-stream pipeline that turns
recordings into YouTube episodes, Shorts and social posts.

## Principles

- **Anki owns the SRS.** Don't build flashcard scheduling or review here. The parser's SQLite
  DB is a capture log only (what was seen, what's synced). Review happens in Anki via
  AnkiConnect.
- **Keep the parser dependency-light and local.** No paid APIs in the parser; model work goes
  through a local Ollama server. manga-ocr is an optional extra (`[ocr]`) because it pulls in
  PyTorch.
- **Nothing personal in code.** Paths, channel names and the streamer's name come from `.env`
  (see `.env.example`) or `apps/screenshot-parser/config.yaml`.

## Key locations

- `apps/screenshot-parser/src/wabisanji/`: analyzer (manga-ocr + Ollama), SQLite capture log,
  AnkiConnect client, watcher, overlay server (`stream/`), ARIA and memes (`live/`), CLI.
- `tools/stream-pipeline/src/wabisanji_post/`: the post-stream pipeline. Run it with
  `uv run --directory tools/stream-pipeline python pipeline.py stream-<cmd>`.
- `content/stream/series.yaml` (series and playlists), `content/stream/memes.yaml` (meme library).
- `docs/`: `stream-mode-spec.md` (live tools), `post-stream-spec.md` (pipeline design),
  `content-rules.md` (what keeps episodes publishable), `stream-setup.md` (OBS),
  `aria-setup.md` (ARIA).
- `.claude/skills/stream-*`: the post-stream workflow (`/stream-post` is the entry point).

## Conventions

- Python ≥3.11, `uv` for env/deps, hatchling builds. Windows is the primary platform.
- Parser CLI: `wabisanji analyze|watch|pending|ingest|section|anki-sync|stats|serve|wotd|listen|live`.
- **Streaming:** keep Ollama off while live (it competes with OBS for the GPU); use
  `/translate` so Claude reads new screenshots instead.
- **Post-stream:** two human gates (`session.yaml`, `clips.yaml`). Never publish public, never
  delete raw recordings. Ask before GPU work if OBS may be live.
- **Instagram posts are public immediately.** Only entries the streamer set to
  `approved: true` in a session's `social.yaml` get posted, and only with `stream-social --go`.
  Claude drafts captions but never approves them.
