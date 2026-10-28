---
name: stream-vods
description: Cut each publishable segment of a stream session into its own YouTube episode file and write its title, description, chapters, tags and composed thumbnail.
argument-hint: "<session id> [segment N]"
---

# /stream-vods: one YouTube episode per segment

Spec section: "Segment VODs". Args: **$ARGUMENTS**

1. Require `status: segments_approved` or later. Skip `skip` segments and anything flagged `under_10_min` unless the streamer says otherwise.
2. **Memes first:** if the session has no `memes.yaml`, run the `stream-memes` skill (no gate). Then run `uv run --directory tools/stream-pipeline python pipeline.py stream-cut-vods <session> [--segment N]` (stream copy, fast; segments with memes, banners or blurs get one re-encode, which uses the GPU).
3. **Numbers:** run `stream-number`. Titles use `#<n>` only when the series is `numbering: locked`; otherwise `#?` and don't publish.
4. **Thumbnail frames:** `stream-thumbs <session> [--segment N] --count 12` (candidate frames; the thumbnail itself is step 6).
5. **Copy:** read the segment's transcript (`transcript/seg<N>.txt`) in full and write `vods/seg<N>.meta.json`:
   `{"hook": "<= 45 chars", "about": ["2-3 sentences", "optional short para"], "chapters": [{"at": "HH:MM:SS", "title": "..."}], "tags_extra": [...], "check": "...", "thumb": {...}}`.
   - `thumb` is the big text on the thumbnail. Japanese-learning episodes: `{"mode": "word", "text": "殺気", "reading": "さっき", "sub": "KILLING INTENT"}`, one word from this episode's captures that's striking or funny in context (the moment the streamer reacted to, not a menu word); `reading` only when the word has kanji; `sub` a 1-3 word English gloss in caps. Everything else: `{"mode": "hook", "text": "20,000 *SILVER*?!"}`, 2-4 words in caps that tease the episode without repeating the title, with the one word to colour amber in `*...*`. Optional `at` / `face_at` (recording times) pin the game frame or the streamer's face frame.
   - The title is `<series> #N<sep><hook>`, with `sep` from the series' `title_sep` in series.yaml (default ` | `). Rough Edges (coding) uses `: ` and its hook is what was built, in Title Case, which may run past 45 chars: "Rough Edges #1: Building a Twitch Chat Tab for VS Code with Claude Code". Whole title ≤ 100.
   - **Single-track recordings** (the MP4s before 2026-09-24, `audio_track: 0` with one track): Spotify may be in the mix. The streamer keeps them as they are (2026-10-06), so add this to `check`: "Possible Spotify music in the mix: check the Content ID result in Studio's Checks step (upload Private) and use Erase song or trim if a claim blocks the video."
   - Chapter times are RECORDING offsets (transcript times), 4 to 8, at least 3 min apart, the first at the segment's first kept part, none inside a cut. They're mapped to the episode file automatically.
   - `check`: swearing hotspots, personal things the streamer says (work, family, health, location), viewer names, and other people's audio (trailers, guide videos, another streamer). Credit and link a guide or creator in `about` when the streamer used their design.
   - Voice: The streamer, first person, friendly and plain, Australian English. No em dashes and no "It's not X, it's Y" lines. Don't invent anything that isn't in the transcript.
6. **Thumbnail:** `stream-thumbnail <session> [--segment N]` composes `vods/seg<N>_thumbs/pick1-3.jpg` (game frame, the streamer's reaction cut out of the cam, the `thumb` text, a series + episode badge). It picks the frames itself; there is no approval gate. Read each `pick1.jpg` back as a sanity check: the text is readable, the cutout is the streamer (not game art), the badge is right. If something's off, set `at`/`face_at` or change the text in `thumb` and rerun with `--force`. Without a `thumb` block it falls back to the most-heard captured word (Japanese) or the first words of the hook.
7. **Build the page:** `stream-youtube-md <session>` writes `vods/seg<N>.youtube.md` (title, description with Twitch/Discord/playlist links, chapters, the Japanese word list, tags, thumbnail picks, check notes in episode-file times). Then `stream-series-checklist "<series>"` refreshes `sessions/_upload_<series>.md`, the in-order upload list the streamer works from.
8. Set the session to `packaged` when all segments are done. Show the streamer the titles, the pick1 thumbnails and every `check` note for sign-off.

Episodes already on YouTube: after rebuilding thumbnails, `stream-thumbnail-push <session>` lists what would change and `--go` sets them (it changes public videos, so only when the streamer asks). One publish command at a time.
