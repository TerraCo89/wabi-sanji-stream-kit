# Stream Mode spec: OBS auto-capture, vocab overlay, reader page

Status: ready to build. Written 2026-10-04 for Claude Code.

## Goal

One command runs everything needed on stream:

```powershell
wabisanji stream --source "Pokemon LeafGreen"
```

That single process:

1. **Serves** the OBS vocab overlay, a reader page and a small JSON API on `http://localhost:8770`.
2. **Auto-captures** dialogue from the OBS game source over the OBS WebSocket. When a text box changes and settles, it saves a screenshot into `screenshot_dir`, so `/translate`, `pending` and `watch` keep working unchanged. No more F8.
3. **Live-OCRs** the dialogue box with manga-ocr on the CPU so the reader page shows the line within a second or two. Chrome + Yomitan on that page gives hover lookups.
4. **Listens for hotkeys**: force a capture, and hide/show the overlay.

The feature set is borrowed from Kamui (kamui.gg): auto-OCR on text change, scan hotkey, dictionary lookups through Yomitan. None of Kamui's code, model or binaries are used.

## Constraints (from CLAUDE.md, still binding)

- **No paid APIs, no Ollama during streams.** Ollama starves OBS of GPU. Live OCR is manga-ocr forced onto the CPU. Full analysis (readings, translation, vocab) stays with `/translate` in Claude Code + `wabisanji ingest`.
- **Anki owns the SRS.** Nothing here schedules reviews. Yomitan is lookup-only (see Phase 3) so `WabiSanji Vocab` stays the single source of cards.
- **Dependency-light.** New deps go in an optional `[stream]` extra. The base install must not change.
- **Existing commands keep their behaviour.** Refactor `analyzer.py` only by adding helpers, never by changing what `analyze`, `watch` or `ingest` do.

## Port conflict to fix first

AnkiConnect listens on **8765**. The OBS `Vocab Overlay` browser source and the old coding-segment plan both point at `http://localhost:8765/overlay`, which would collide. Stream mode uses **8770**.

- Change the OBS `Vocab Overlay` URL to `http://localhost:8770/overlay`.
- Update `Coding-Stream-Segment.md` and `WabiSanji-scenes.json` in the streaming folder (`Documents\WabiSanji`), which still say 8765.

## Architecture

```
OBS "Game Capture" ──WebSocket :4455──> capture loop ──> change detector
                                                          │
                     PNG obs-YYYYMMDD-HHMMSS-fff.png <────┤ (settled + new)
                     in screenshot_dir                    │
                            │                             └──> manga-ocr (CPU, crop only)
                            │                                        │
      Claude Code /translate ──> wabisanji ingest ──> SQLite         └──> LiveLines ring buffer
                                                       │                       │
                                   GET /api/words <────┘      GET /api/live <──┘
                                        │                            │
                         OBS overlay (340×1080)        /reader in Chrome + Yomitan
                                                       /reader?obs=1 as subtitle bar
```

Cross-process data flows through the SQLite DB (WAL is already on). In-process live data (OCR lines, overlay hidden flag, OBS status) lives in memory.

## File layout (new unless noted)

```
apps/screenshot-parser/
  pyproject.toml              # edit: add [stream] extra and a dev group with pytest
  config.yaml                 # edit: add stream: section
  src/wabisanji/
    config.py                 # edit: StreamConfig dataclasses + loader
    cli.py                    # edit: add `serve` and `stream` subcommands
    analyzer.py               # edit: add preprocess_pil() helper only
    db.py                     # edit: add read-only query helpers
    stream/
      __init__.py
      server.py               # FastAPI app, routes, lifespan starts workers
      state.py                # thread-safe shared state (live lines, flags, status)
      obs_client.py           # OBS WebSocket wrapper with reconnect
      detect.py               # pure change-detection logic (no I/O, unit-tested)
      capture.py              # capture loop: OBS frame → detect → save → enqueue OCR
      liveocr.py              # manga-ocr worker thread (CPU)
      hotkeys.py              # pynput global hotkeys
      static/
        overlay.html
        reader.html
  tests/
    test_detect.py
    test_api.py
```

## Config additions (`config.yaml`)

```yaml
stream:
  port: 8770
  obs:
    host: "localhost"
    port: 4455
    password_env: "OBS_WS_PASSWORD"   # read from env; never put the password in yaml
    source: "Game Capture"            # OBS source to screenshot
    only_scenes: ["Gameplay"]         # skip capture on BRB/Starting/Ending; [] = always
  capture:
    enabled: true
    interval_ms: 500                  # how often to grab a frame
    settle_ms: 350                    # box must be still this long before saving
    change_threshold: 6.0             # mean abs diff (0-255) on the downscaled crop
    dup_hash_distance: 6              # dHash Hamming distance; below = same box, skip
    min_ink: 0.005                    # fraction of dark pixels in crop; 0 disables check
    max_ink: 0.25
    max_per_minute: 20                # flood guard for cutscenes
    prefix: "obs-"
    region: [0.0, 0.70, 1.0, 0.30]    # default dialogue box: x, y, w, h as frame fractions
    regions:                          # per-game overrides, keyed by source name
      "Pokemon LeafGreen": [0.0, 0.70, 1.0, 0.30]
  live_ocr: "manga-ocr"               # manga-ocr | none
  hotkeys:
    scan: "<ctrl>+<alt>+s"
    toggle_overlay: "<ctrl>+<alt>+o"
```

Hotkey note: pynput does not swallow keys, so the focused app also receives them. Avoid RetroArch's F-keys (F8 is screenshot, others are bound too).

## API

| Route | Returns |
|---|---|
| `GET /health` | `{ok, obs_connected, capture_enabled, live_ocr, overlay_hidden, source}` |
| `GET /api/words?limit=8&source=` | Newest words, see below |
| `GET /api/today?source=` | `{new_words, screenshots}` for the local calendar day |
| `GET /api/live?after=<id>&limit=50` | Live OCR lines newer than `after`, each enriched with translation/speaker once `/translate` has ingested that file |
| `GET /api/state` | `{overlay_hidden}` (overlay polls this) |
| `POST /api/capture` | Force a capture now (same as scan hotkey). For Stream Deck / OBS hotkeys |
| `POST /api/overlay/toggle` | Flip `overlay_hidden` |
| `GET /overlay` | `static/overlay.html` |
| `GET /reader` | `static/reader.html` (`?obs=1` = transparent subtitle mode) |

### `/api/words` query rules

- Order by **screenshot id descending**, then join to vocabulary, de-duplicated by vocabulary id. Don't sort on timestamps: `screenshots.processed_at` is SQLite `CURRENT_TIMESTAMP` (UTC, `YYYY-MM-DD HH:MM:SS`) while `vocabulary.last_seen_at` is Python ISO format, so string ordering across them is wrong.
- Fields: `word, reading, meaning, form, jlpt_level, category, section, speaker, encounter_count, is_new` (`is_new` = `encounter_count == 1`).
- `source` filters through `screenshots.source_id → sources.name`. Default is the session source (`session.load()["source"]`), or `--source` if passed to `stream`.
- `/api/today` converts the local day's start and end to UTC before comparing with `first_seen_at` / `processed_at`.

### DB access

The server reads only. Add helpers in `db.py` that open a fresh read-only connection per request (`sqlite3.connect("file:...?mode=ro", uri=True)`). Don't construct `Database()` in request handlers: its constructor runs the schema script and migrations, which write.

## Change detection (`detect.py`)

Pure functions plus a small state class, so it's testable with synthetic numpy frames.

```
crop      = frame[region]                     # region from config, per game
small     = grayscale(crop) resized to 160 px wide
diff      = mean(|small - prev_small|)

if diff > change_threshold:       moving = True; changed_at = now
elif moving and now - changed_at >= settle_ms:
    moving = False
    if ink_ok(small) and hamming(dhash(small), last_saved_hash) > dup_hash_distance:
        SAVE
```

Why this handles Gen 3 Pokémon:

- **Typewriter text** changes every tick, so it only saves once the line finishes.
- **The blinking ▼ arrow** is a few pixels; its mean diff stays below the threshold.
- **Re-showing the same box** (menus opened and closed) is caught by the dHash check.
- **Map scrolling** behind no box fails the ink check, or is skipped by `only_scenes`.

A forced capture (hotkey or `POST /api/capture`) bypasses settle, ink and dedupe checks.

## Phases

Build in order. Each phase ends in something runnable. Commit at every milestone. If the repo isn't a git repo yet, run `git init` and commit the current tree first.

### Phase 0: prep (manual, 5 minutes)

1. OBS → Tools → WebSocket Server Settings → enable, port 4455, set a password.
2. In the terminal you'll launch from: `$env:OBS_WS_PASSWORD = "<password>"`. Don't echo it on stream.
3. Change the OBS `Vocab Overlay` URL to `http://localhost:8770/overlay`.
4. Install Yomitan in Chrome and import JMdict (Yomitan's welcome page links the recommended dictionaries). In Yomitan settings, leave **Anki integration off**.

### Phase 1: `serve` + overlay (DB only, no OBS)

Deliverables: `[stream]` extra (`fastapi`, `uvicorn`), `StreamConfig`, `stream/server.py`, `stream/state.py`, `static/overlay.html`, `db.py` read helpers, `wabisanji serve [--port] [--source]`, `tests/test_api.py`.

Overlay design: 340×1080 transparent column, Noto Sans JP (system font fallback, no CDN). Each word is a card: **word** large, reading small in amber `#e8a04a`, meaning in cream `#f2ecdc`, card indigo `#2b2f55` at 80% alpha. Newest on top, slide-in for new entries, max 8 cards. A small "NEW" pip when `is_new`. Footer card: `今日: N words` from `/api/today`. Polls `/api/words` and `/api/state` every 2 s and fades out when `overlay_hidden`.

Done when:
- `wabisanji serve` starts on 8770 and `/health` returns ok.
- `/api/words` shows the most recent LeafGreen words from the existing DB, newest first.
- The OBS `Vocab Overlay` source shows cards. Running `/translate` makes new cards slide in within 2 s.
- `pytest` passes. `test_api.py` builds a temp DB from `schema.sql`, inserts two screenshots with words and asserts ordering, `is_new` and source filtering.

### Phase 2: OBS auto-capture

Deliverables: `obsws-python` in `[stream]`, `stream/obs_client.py`, `stream/detect.py`, `stream/capture.py`, `tests/test_detect.py`, `wabisanji stream` (serve + capture), `wabisanji stream calibrate`.

- `obs_client.py` wraps `obsws_python.ReqClient`. Use `get_source_screenshot` for frames and `get_current_program_scene` for `only_scenes`. Check the installed library's method signatures before coding against them. Decode the base64 data URI to a PIL image.
- Reconnect every 5 s if OBS isn't reachable. `/health.obs_connected` reflects it. The server must keep running without OBS.
- Save the **full frame** (not the crop) as `screenshot_dir/<prefix><YYYYMMDD-HHMMSS-fff>.png`. Write to a temp name then rename, so the watcher never sees half a file.
- `stream calibrate`: grab one frame, draw the configured region as a red rectangle, save `data/calibrate.png`, print its path. Used to tune `regions` per game.
- Frame grabbing runs in a background thread started by the FastAPI lifespan. Log one line per save: `captured obs-....png (diff 14.2, ink 3.1%)`.

Done when:
- Playing LeafGreen with `wabisanji stream` running saves exactly one PNG per dialogue box, with no F8 presses.
- Text scrolling in doesn't produce partial captures. The idle ▼ arrow doesn't produce repeats.
- Switching to the BRB scene stops captures.
- `/translate` processes the `obs-` files normally (`pending` lists them).
- `test_detect.py` covers typewriter frames → one save, blinking arrow → no save, same box shown twice → one save, blank crop → no save (ink check), forced capture → always saves.

### Phase 3: live OCR + reader page

Deliverables: `stream/liveocr.py`, `analyzer.preprocess_pil()`, `static/reader.html`, `/api/live`.

- Load manga-ocr once with `MangaOcr(force_cpu=True)` so the GPU stays free for the OBS encoder. Confirm `force_cpu` exists in the installed `manga_ocr` version.
- The OCR worker uses a queue of size 1: if a new crop arrives while one is processing, drop the stale one. OCR runs on the **crop**, preprocessed with the same grayscale + CLAHE + 2x upscale as `preprocess_image_for_ocr` (new `preprocess_pil(img)` shares the code; the path-based function calls it).
- `LiveLine = {id, text, captured_at, file_path, source, section}` in a 50-item ring buffer in `state.py`.
- `/api/live` enriches each line: if a `screenshots` row exists with that `file_path`, add `translation` and `speaker`.
- If manga-ocr isn't installed or `live_ocr: none`, log one line and skip. Capture still works.

Reader page (`/reader`), for the streamer's monitor in Chrome:
- Large selectable plain text, newest line at the bottom, auto-scroll unless the user has scrolled up.
- Translation hidden under each line, click to reveal. Speaker label when known.
- Font size +/- buttons, light/dark following the system. No frameworks, no external requests.
- Plain text nodes only (no canvas, no per-character spans) so Yomitan's scanner works.

Subtitle mode (`/reader?obs=1`), as an OBS browser source:
- Transparent background, last 2 lines only, large text with a dark outline.

Done when:
- A new dialogue box appears on the reader page within about 2 s of settling.
- Shift-hover in Chrome pops up Yomitan definitions on reader text.
- After `/translate`, the matching lines gain a reveal-able translation.
- OBS encoding stats show no extra dropped frames with live OCR on.

### Phase 4: hotkeys + polish

Deliverables: `pynput` in `[stream]`, `stream/hotkeys.py`, `POST /api/capture`, `POST /api/overlay/toggle`, flags `--no-capture`, `--no-ocr`, `--no-hotkeys`, `--port`.

- `serve` becomes `stream --no-capture --no-ocr --no-hotkeys`. Keep it as an alias.
- Startup banner prints: URLs for overlay/reader/health, OBS status, OCR status, active hotkeys, current game + section.
- Ctrl+C shuts down the threads cleanly.

Done when:
- The scan hotkey saves a capture immediately, even mid-scroll.
- The overlay hotkey fades the overlay out and back in.
- Each `--no-*` flag disables only its part.

### Phase 5: docs

- `apps/screenshot-parser/README.md`: a "Stream mode" section covering setup, config and commands.
- `CLAUDE.md`: add `serve|stream` to the CLI list and note stream mode in Streaming.
- `.claude/commands/translate.md`: `obs-` captures have no RetroArch toast. Keep the toast rule for F8 shots.

## Stretch (not now)

- **SSE push** instead of 2 s polling, for instant overlay updates.
- **Mine button** on reader lines: `POST /api/mine {line_id}` adds the file to a priority list that `/translate` processes first.
- **Click-through overlay on the game window**, Kamui style. The OBS overlay covers the stream, so this only matters off-stream.
- **Audio clip per line** from the OBS replay buffer, for sentence audio on cards.

## Don't

- Copy anything from the Kamui download (model, exes, JS). Ideas only.
- Call Ollama or any paid API from stream mode.
- Turn on Yomitan's Anki export into the WabiSanji decks. It would create a second note type and duplicate cards.
- Put the OBS password in config, logs or the banner.

## Claude Code prompts

Paste one per phase. Each assumes the repo root as the working directory.

**Phase 1**
> Read docs/stream-mode-spec.md, then build Phase 1 only. Add a `[stream]` optional extra (fastapi, uvicorn) and a dev group with pytest to apps/screenshot-parser/pyproject.toml, StreamConfig in config.py with the `stream:` section from the spec, read-only query helpers in db.py, and the `stream/` package with server.py, state.py and static/overlay.html. Add a `wabisanji serve [--port] [--source]` subcommand on port 8770. Follow the /api/words ordering rules in the spec exactly. Write tests/test_api.py against a temp DB built from schema.sql. Don't touch analyzer.py, anki.py or watcher.py. Stop when the Phase 1 "Done when" list passes and show me how to run it.

**Phase 2**
> Continue docs/stream-mode-spec.md with Phase 2 only: obs_client.py, detect.py, capture.py, `wabisanji stream` and `wabisanji stream calibrate`. Check the installed obsws-python method signatures before using them. Keep detect.py pure and write tests/test_detect.py with synthetic numpy frames covering every case in the spec. The server must keep running when OBS is unreachable. Read the OBS password only from the env var named in config, and never print it.

**Phase 3**
> Continue docs/stream-mode-spec.md with Phase 3 only: live OCR on the CPU with manga-ocr (confirm force_cpu exists in the installed version), preprocess_pil() in analyzer.py without changing existing behaviour, /api/live with translation enrichment, and static/reader.html including the ?obs=1 subtitle mode. Reader text must be plain text nodes so Yomitan can scan it.

**Phase 4 + 5**
> Finish docs/stream-mode-spec.md: Phase 4 (pynput hotkeys, POST endpoints, --no-* flags, startup banner, clean shutdown, serve as an alias) and Phase 5 (README, CLAUDE.md, translate.md updates). Then run the full test suite and list anything from the spec that's still unverified because it needs OBS or a live game.
