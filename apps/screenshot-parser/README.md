# Wabi Sanji Screenshot Parser

Parses Japanese text from game screenshots (RetroArch etc.) and pushes vocabulary to Anki. Anki owns the spaced repetition; the local SQLite DB is just a capture log of what you've seen and what's been synced.

**Fully local — no API keys, no cost per screenshot.** OCR runs on [manga-ocr](https://github.com/kha-white/manga-ocr), and readings/translation/vocabulary come from a model served by [Ollama](https://ollama.com) on your own machine.

## How it works

1. A watcher (or one-shot `analyze`) picks up a screenshot.
2. **hybrid** analysis (default): a local Ollama vision model reads the whole screen — dialogue, menus, battle options, HUD — and produces readings, an English translation, and a vocabulary breakdown. manga-ocr's dialogue text is passed along as an anchor hint, and the model discards it when it doesn't match the image (OCR hallucinates on menu-only screens). `vision`-only (no manga-ocr) and `manga-ocr`-only modes also exist.
3. Results land in `data/wabisanji.db` (dedup by image checksum).
4. New vocabulary is pushed to Anki via [AnkiConnect](https://ankiweb.net/shared/info/2055492159) — one note per word, with reading, meaning, JLPT level, the sentence it appeared in, and the screenshot itself on the card.

## Setup

1. **Install [Ollama](https://ollama.com)** and pull the default model (~6GB, needs roughly 6GB of VRAM/RAM):

   ```powershell
   ollama pull qwen3-vl:8b
   ```

2. **Install the parser:**

   ```powershell
   cd apps/screenshot-parser
   uv venv && uv pip install -e .[ocr]   # drop [ocr] to skip the ~1GB manga-ocr/torch install
   ```

3. **Anki desktop** must be running with the AnkiConnect add-on (code `2055492159`).

Edit `config.yaml` for your screenshot folder and deck names. The model lives there too (`ollama.model`, any vision-capable Ollama model): `qwen3-vl:8b` is the accessible default and handles dialogue screens well; a bigger model like `qwen3.6:27b` (17GB, ~18GB VRAM) also reads menus and battle screens reliably and segments vocabulary noticeably better.

## Usage

```powershell
wabisanji analyze path\to\screenshot.png --source "Dragon Quest III"
wabisanji watch --source "Dragon Quest III"   # process screenshots as you play
wabisanji anki-sync                            # push any pending vocab to Anki
wabisanji stats
wabisanji pending                              # screenshots not yet in the capture log
wabisanji ingest results.json --source "..."   # save analysis made elsewhere, then sync
wabisanji section "Pallet Town" --source "Pokemon LeafGreen"   # set the current town/route
```

Cards are filed by section: `WabiSanji::<game>::01 Pallet Town`, `02 Route 1`, … (numbered in play order), plus `Menus`, `Battles`, and `Pokémon & Names`. A word's card stays in the section where it was first seen; each later section it turns up in adds a `wabisanji::<game>::<section>` tag. The card's source line shows game · section · speaker.

**While streaming**, don't run `watch`: the Ollama model competes with OBS for the GPU and drops encoder frames. Instead press F8 in RetroArch and run `/translate` in Claude Code (repo root). Claude reads the pending screenshots, writes the same result JSON the Ollama analyzer produces, and `ingest` saves it (`ocr_method = claude`) and pushes the vocab to Anki.

The first run creates the `WabiSanji Vocab` note type automatically, and section decks are created as cards land in them. Re-syncing an existing word updates the note rather than duplicating it.

## Notes

- The first analysis after a cold start is slow while Ollama loads the model into VRAM; later ones take a few seconds each.
- Local models are decent but not perfect at furigana and JLPT tagging — skim new cards in Anki before trusting them. Readings on common game vocabulary are generally solid.
