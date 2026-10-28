# ARIA on stream

ARIA listens to your mic while you stream. Say **"Hey ARIA, how do I…"** and a hint for the current game shows up on screen and is read out in Jessica's voice. Quote a line from the meme library and the meme pops up.

Streamer.bot isn't involved. Its Voice Control only knows fixed phrases, so it can't catch a question or a loose reference.

## How it fits together

| Piece | What it does |
|---|---|
| `wabisanji listen` | Records the mic and turns speech into text with Whisper `base.en` **on the CPU**, so the GPU stays free for OBS. It handles the wake word, the follow-up commands and phrase-matched memes itself. |
| `wabisanji serve` (:8770) | Serves the two OBS browser sources below, as well as the vocab overlay. |
| `/aria watch` in Claude Code | Answers questions with a web search for the current game, and picks memes for looser references from the transcript. Run it in **its own terminal**, next to `/translate watch`. |
| Speaker.bot (:7580) | ARIA's voice: the `ARIA` alias. |

Files (none are in git) go in `apps/screenshot-parser/data/live/`:
- `transcript.jsonl` has what you said.
- `events.jsonl` has the questions, answers and memes, timestamped for clip picking later.
- `state.json` holds whether memes are on.

## One-time setup

1. **Python extras:** in `apps/screenshot-parser`, run `uv sync --extra ocr --extra stream --extra listen`. The first `listen` downloads the Whisper model, about 140 MB.
2. **Speaker.bot → Settings → Voice Aliases → add `ARIA`** (capitals):
   - Engine: ElevenLabs.
   - Voice: **Jessica**, Turbo v2.5 or Flash v2.5.
   - Output device: **Sonar - Chat**, the same as the other aliases, so OBS's TTS source picks it up.
   - Use the alias Test button, then run `wabisanji live say "Hi, I'm ARIA"`.
3. **OBS browser sources** (both "Shutdown source when not visible" off). Type the URLs with `http://`: OBS won't load `localhost:8770/...` without it. When `wabisanji serve` starts it reloads every OBS browser source pointing at :8770 over obs-websocket, and adds a missing `http://`. That covers OBS opening before the server after a reboot. The websocket password comes from `OBS_WS_PASSWORD`, else from obs-websocket's own config on this PC.
   - **ARIA memes**: `http://localhost:8770/memes-overlay`, 1920×1080, above the game.
     - The meme is centred at 50% across and 42% down. To move it, add `?x=0.7&y=0.3` (fractions of the canvas).
     - Tick **Control audio via OBS**, so meme clips get their own mixer fader and track routing. Without it, the clip sound plays on the PC's default output, which the stream may never hear.
     - Clips are levelled to about −20 LUFS. Turn them down with `?volume=0.7` or the fader.
   - **ARIA card**: `http://localhost:8770/aria`, 600×340. Put it where it won't cover the facecam or the vocab column. The card grows up from the bottom of the source.
4. **Mic:** `wabisanji listen --list-devices`. The default input (`*`) is Sonar - Microphone, which is right. To use a different one, set `stream.live.mic` in `config.yaml`.
5. **Memes:** the library is `content/stream/memes.yaml`, and the files are in the memes folder (`stream.live.memes_dir` in `config.yaml`, or `WABISANJI_MEMES`). `wabisanji live memes` lists them and flags any missing file.
   - **What's in it** (45 memes, 2026-10-20):
     - Real Civil Engineer's recurring cutaways, e.g. "Oh no. Anyway.", "Hello, I like money", "Are you sure about that?", "A few moments later".
     - The standard meme-editing sounds, e.g. vine boom, bruh, sad trombone, metal pipe, MGS alert.
     - Game-screen images: YOU DIED, wasted, mission passed.
     - Anime bites for Japanese study: nani, omae wa mou shindeiru, objection.
   - **Three kinds:**
     - Clips (.mp4) pop up with sound.
     - Sounds (.mp3) play with their `caption` shown as a speech pill.
     - Images show for 3 s.
   - **Add one in a single command.** It searches YouTube, keeps the first short result where Whisper hears the line, trims it tight, levels it to −20 LUFS and adds the entry:
     `wabisanji live fetch "ytsearch:mr krabs i like money" --expect "i like money" --id i-like-money --caption "Hello, I like money." --category win --risk med --about "..."`.
     - `myinstants:<name>` gets a sound effect.
     - A YouTube URL with `--from/--to`, a URL or a file also work.
     - Check new clips by eye: the audio is verified, the picture isn't. Use `--audio-only` when the video is a green screen or a still.
   - **Rights:**
     - Every meme has a `risk` for its audio (see content-rules).
     - Music-based memes (Roundabout, coffin dance, Rickroll, Yakety Sax) stay out.
     - GIPHY's API terms don't allow keeping downloaded copies without their approval, so new memes come from the original source rather than GIPHY.

## Each stream

1. `wabisanji serve` (already part of stream mode).
2. In a second Claude Code terminal: `/aria watch`. It starts `wabisanji listen` itself through `apps/screenshot-parser/scripts/listen_autorestart.sh`, which restarts the listener whenever `memes.yaml` changes, so a meme added mid-stream fires on its phrase a few seconds later. The listener's output goes to `apps/screenshot-parser/data/live/listen.log`. To watch the lines live, run `wabisanji listen` in a terminal yourself instead; `/aria watch` then leaves yours alone, but new memes need a restart.
3. Set the game: say "Hey ARIA, we're playing <game>", or use `/translate game …` or `wabisanji section "<area>" --source "<game>"`. ARIA uses it to search the right game.

## What you can say

| Say | What happens | Claude needed? |
|---|---|---|
| "Hey ARIA, how do I get past the barnacles?" | The card shows what it heard, then a hint 10 to 20 s later. Jessica reads the hint. | yes |
| "Hey ARIA." … "how do I crouch jump?" | Same, if the question comes within 10 s | yes |
| "ARIA, more" / "tell me more" | Steps, read out too | no |
| "ARIA, repeat" | Reads the hint or steps again | no |
| "ARIA, never mind" / "thanks" / "Hey ARIA, stop searching" | Hides the card. A late answer to that question won't show or be read out. | no |
| "Stop that" / "Stop searching" (no "Hey ARIA") | The same, but only within 90 s of asking, so it doesn't fire when you say it to the game or the dog later. | no |
| "Hey ARIA, memes off" / "memes on" | Turns all memes off or on, both the instant and the Claude-picked ones | no |
| "Hey ARIA, we're playing Enshrouded" / "switch game to …" / "the game is …" | Sets the game hints search for. A misheard name snaps to a game in series.yaml or one you've played before. ARIA says "Okay, Enshrouded." | no |
| "Hey ARIA, family mode" / "family mode off" | While on, memes marked `family: false` (that's what she said, noice) stay quiet. Say it when the kids join. | no |
| "That's what she said" / "69" / "noice" | The clip plays 1 to 2 s after you finish the sentence | no |
| Any other line from `phrases` in memes.yaml | Its meme pops 1 to 2 s after you finish the sentence | no |
| A looser reference | Claude may pop a meme within about a minute. Most of the time it won't. | yes |

Hints are spoiler-light on purpose. Ask for "more" when you want the full solution.

## Limits worth knowing

- **ElevenLabs characters:** the free plan's characters are shared with the FAMOUS big-cheer voice. Hints are about 15 words, but "more" reads every step. If ARIA goes quiet mid-stream, the plan has probably run out. Put an Azure voice in `ARIA` until it resets.
- **Spacing between memes:** at least 20 s between any two, and 5 min before the same meme repeats (`cooldown_s` changes that). Claude's picks also wait 60 s after any meme.
- **Mishearing:** Whisper writes "Aria" as Arya, Area and so on. Those spellings are in `stream.live.wake_words`. "Area" only counts after "hey"/"okay", and a lone "Aria." never wakes it, because Whisper invents that from mumbles.
- **Headphones:** ARIA only hears the mic, so viewer TTS can't trigger it unless it bleeds through speakers. The listener also ignores you while ARIA is talking.
- **CPU:** `base.en` handled 3 minutes of stream audio in about 10 s on this PC. `small.en` is a little more accurate but about 3 times slower; set `stream.live.model` to try it.

## Testing without going live

- `wabisanji listen --file "<recordings root>\<recording>.mkv" --start 1200 --duration 180` runs the listener over a recording and prints what it heard, wake words and meme hits.
  - It's a dry run: nothing is logged, posted or spoken.
  - The recordings only have the full mix for now (`--track 0`). Once Track 2 is mic-only, use `--track 1`, which is cleaner.
- `wabisanji live meme <id>` pops a meme by hand (`--force` skips the rules). `wabisanji live answer answer.json --no-speak` shows a card.

## After the stream: memes in episodes

Memes ARIA popped live are already in the recording, so episodes keep them. On top of those, `/stream-memes` does an editor's job:
1. It reads each episode's transcript.
2. It picks about one meme every 3 to 5 minutes, each answering the line the streamer just said, and writes `memes.yaml` in the session folder.
3. `stream-memes <session>` checks the picks. It drops any that land in a cut, sit within 90 s of another meme, fall near a live meme, go over the cap (one per 2.5 min) or are not-family in a segment with the kids.
4. `stream-cut-vods` pops the survivors over the gameplay in the same re-encode as banners and blurs, with the episode audio ducked under each meme's sound.
5. The episode's "Check before publishing" note lists every meme, live and added, with its risk.

There's no approval gate: The streamer reviews the episode before uploading anyway. Shorts don't get added memes.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Nothing happens when you say "Hey ARIA" | Watch the `listen` terminal. If your line isn't printed, check the mic (`--list-devices`). If it's printed with a different spelling, add that spelling to `wake_words`. |
| The card says "looking it up" forever | `/aria watch` isn't running, or its Monitor expired and wasn't re-armed. Run `/aria watch` again; open questions from the last 10 minutes are picked up. |
| "overlay server not reachable" | Start `wabisanji serve`. |
| The listener prints `-> meme …` but nothing shows in OBS | Check the `obs:` line `serve` printed. If OBS wasn't reachable, right-click the source → Properties → Refresh cache of current page. |
| Answers show but ARIA is silent | The `ARIA` alias is missing or has no voice, Speaker.bot isn't running, or the ElevenLabs characters ran out. `wabisanji live say test` shows the error. |
| A meme is listed but never shows | `wabisanji live memes` flags `[MISSING FILE]`. The file name must match exactly. |
