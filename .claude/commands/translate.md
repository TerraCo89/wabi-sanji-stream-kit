---
description: Translate new RetroArch screenshots live on stream (Claude reads the image, no Ollama/GPU), save to the capture log and push vocab to Anki.
argument-hint: "[blank = new shots | N = only the N most recent | <section name> = move to a new town/route first | game <name> | watch | stop]"
---

# /translate — live screenshot translation (stream mode)

Use this instead of `wabisanji watch` while streaming: the local Ollama model
fights OBS for the GPU. You (Claude) do the vision step; the parser just saves
and syncs. The user invoked you with: **$ARGUMENTS**

Parser lives in `apps/screenshot-parser`; run its CLI as
`apps/screenshot-parser/.venv/Scripts/wabisanji.exe`.

## Sections

Cards are filed into Anki by **section**, numbered in play order:
`WabiSanji::<game>::01 Pallet Town`, `02 Route 1`, … plus fixed `Menus`,
`Battles` and names decks (`Pokémon & Names` for Pokémon games, `Names` for
everything else). A word's card lives in the section where
it was *first* seen; later sightings add a tag for each new section.

The current game and section live in the parser's session
(`wabisanji.exe section` shows them).

Interpret `$ARGUMENTS`:
- blank → process new shots in the current section.
- a number N → only the N most recent shots.
- `game <name>` → `wabisanji.exe section "<first section>" --source "<name>"`
  is needed; ask the user for the first section name if they didn't give one.
- `watch` → hands-free mode, see **Watch mode** below.
- `stop` → stop the watch monitor with TaskStop and confirm in one line.
- any other text → it's a section (town/route/building) name. Run
  `wabisanji.exe section "<text>"` first (re-entering a visited section reuses
  its number), confirm the new section in one line, then process new shots.

If the session has no section yet, ask which section they're in before saving.

### Naming sections yourself

The user may not know the game, so by default **you pick the section names**
from what's on screen: a location banner or sign, an NPC naming the place, a
map screen, or an obvious scene change (new town, dungeon, overworld). Use the
English name if you know it with certainty, otherwise a short romanization or
plain description ("Desert", "Inn", "Castle Dungeon"). When the shots in a batch
clearly show a new place, run `wabisanji.exe section "<name>"` **before**
ingesting those shots (split the batch if it straddles the change), and say
it in the summary: "→ new section: 03 Inn". Don't split hairs: one section per
town/dungeon/region, not per room. A section the user names always wins.

## Watch mode

So the user can stay in the game: press F8, and the translation shows up here.

1. If shots are already pending, process them first (Steps below).
2. Start the Monitor tool (load it with ToolSearch if deferred):
   - command: `bash apps/screenshot-parser/scripts/watch_pending.sh`
   - description: `new RetroArch screenshots`
   - timeout_ms: `1800000` (the maximum)
   Confirm in one line: "Watching — press F8 in RetroArch, translations appear here."
3. On each `NEW_SCREENSHOTS n` event, run Steps 1–4 with no arguments (the
   10-shot cap still applies). Don't ask questions mid-watch; if a shot is
   ambiguous, make the best call and flag it in the summary.
4. On `ERROR:` lines, say what failed in one line and keep watching.
5. When the monitor expires (30 min), re-arm it silently unless the user said
   `stop`. If the user names a new section while watching, run
   `wabisanji.exe section "<text>"`; the monitor keeps running.

## Steps

1. **Find new shots.** `wabisanji.exe pending` (add `--limit N` if `$ARGUMENTS`
   is a number). Output is oldest-first, one path per line. If nothing is
   pending, say "No new screenshots — press F8 in RetroArch" and stop.
   If more than 10 are pending and no number was given, process the 10 most
   recent and mention how many older ones remain.

2. **Read each image** with the Read tool, in order. For each, produce a
   result object:

   ```json
   {
     "file_path": "<exact path from pending>",
     "screen": "dialogue|menu|battle",
     "speaker": "Mom",
     "extracted_text": [{"text": "…", "reading": "…ひらがな…"}],
     "translation": "natural English",
     "context": "one line: who is speaking / where (e.g. Mom, player's house, Pallet Town)",
     "vocabulary": [{"word": "…", "reading": "…", "meaning": "…",
                     "part_of_speech": "…", "jlpt_level": "N5|N4|N3|N2|N1|null",
                     "category": "speech|menu|pokemon",
                     "form": "あがった (past tense)"}]
   }
   ```

   `screen` decides the deck: `dialogue` (NPC talk, signs, TV, item pickups,
   yes/no prompts inside a conversation, cutscenes) → current section; `menu`
   (start/camp menu, items, equipment, status, options, shops' item lists) →
   Menus; `battle` (anything on the battle screen, including lines spoken
   during battle) → Battles.

   `speaker`: who's talking, short and consistent so chat can follow a
   conversation — "Mom", "Professor Oak", "Rival", "Girl by the fence",
   "Sign", "TV", "Narration". If a box continues the previous shot's
   conversation, reuse that speaker. Empty for menus.

   Rules (same as the Ollama prompt, so cards stay consistent):
   - Ignore RetroArch's own overlays: the "Screenshot saved" toast in the top
     corner contains a thumbnail of the *previous* screenshot — never read text
     from it. Only the game screen counts.
   - PC games (e.g. Breath of Fire IV) are captured with OBS's screenshot
     hotkey, so the shot is the whole stream layout: read only the game
     capture. Ignore the vocab overlay, webcam, chat and any other scene
     elements, even when they contain Japanese.
   - Read each kana carefully; similar shapes get confused at pixel-font sizes
     (む/も, ぐ/ぞ, ホ/ポ, ソ/ン, シ/ツ). If a word doesn't exist with your reading,
     re-check the glyph before inventing a meaning.
   - Transcribe exactly what's on screen in `text`, in the script the game
     uses. Gen 3 Pokémon is kana-only with spaces between words (keep the
     spaces); most other games (e.g. Breath of Fire IV) mix kanji and kana —
     keep the kanji. If a game shows furigana, put the base text in `text`.
   - Readings in hiragana, never romaji — katakana words too (ポケモン → ぽけもん),
     matching the existing cards.
   - Vocabulary: content words only (nouns, verbs, adjectives, adverbs, set
     expressions), in dictionary form. Skip bare particles and grammar fragments.
     Keep `word` in the spelling the game uses (Anki dedups on word+reading,
     so a different spelling would create a duplicate). For a kana-only game,
     put the standard kanji spelling at the end of `meaning` when one exists,
     e.g. であう → "to meet; to encounter (出会う)". When the game writes the
     word in kanji, `word` is the kanji and `reading` is its hiragana.
   - `form`: when the on-screen form differs from the dictionary form, give it
     with a short plain-English label of the conjugation, e.g. あがった
     "(past tense)", つかまえたり "(-tari: doing things like…)", かかれていく
     "(passive + -te iku: gets written over time)". Empty string when the word
     appears in dictionary form. This shows on the card back as "on screen: …".
   - Category: `pokemon` for character, monster and Pokémon species names
     (it's the internal value for the names deck in every game), `menu` for
     UI/battle/HUD terms, `speech` for dialogue.
   - Proper nouns: official English name only if certain, else romanize.
   - JLPT null if unsure.
   - Skip screens with no Japanese text (don't include them, but still note
     them in the summary so the user knows why).
   - If the text box is cut off mid-sentence (▼ arrow), translate what's
     visible and say "(continues)".

3. **Save.** Write the list of result objects to
   `apps/screenshot-parser/data/claude_batch.json` (overwrite), then run:
   `wabisanji.exe ingest apps/screenshot-parser/data/claude_batch.json`
   It uses the session's game and section, routes each shot by `screen`,
   cleans the vocab, records `ocr_method=claude`, and pushes to Anki. Each
   saved line shows the section it went to — check they look right. If it
   reports the Anki sync was skipped, tell the user to open Anki and run
   `wabisanji.exe anki-sync`.

4. **Stream-friendly summary.** For each screenshot, in order, keep it short
   enough to read aloud:

   ```
   ### <speaker> — <section>
   いま テレビで…            ← on-screen text
   ima terebi de…            ← romaji, so chat can follow along
   "There's a movie on TV…"  ← translation
   Words: テレビ (terebi) TV · えいが (eiga) movie · …
   ```

   Finish with one line: how many new words went to Anki. Add a 1–2 sentence
   "teaching moment" only if there's a genuinely interesting grammar point or
   pun (game dialogue is full of casual speech worth a note); otherwise skip it.
