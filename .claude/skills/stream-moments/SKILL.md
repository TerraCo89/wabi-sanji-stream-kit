---
name: stream-moments
description: Pick the best clip moments from a transcribed stream session (transcripts plus vocab, loudness, marker and chat signals) and write clips.yaml for the streamer to approve.
argument-hint: "<session id> [segment N] [max clips per segment, default 8]"
---

# /stream-moments: propose clips (Gate 2)

Spec sections: "Moment signals", "What makes a good clip" and "clips.yaml". Args: **$ARGUMENTS**

1. If `signals/` is missing or stale, run `stream-signals <session>`.
2. For each non-skip segment, read `transcript/seg<N>.txt` in full, plus `signals/*.json` for that time range.
3. Find candidates where signals stack up: a vocab capture next to a reaction, a loudness spike on a laugh, a marker, a burst of chat (`signals/chat.json`: `bursts`, plus `messages` to see what chat said and `events` for subs and raids; a viewer interaction a clip can show). Then read the surrounding transcript to set clean in and out points (start on the hook, end on the payoff, 20 to 60 s).
4. Japanese segments: favour moments that teach something a viewer can follow (the line, the word, the "oh!"). Gaming segments: wins, fails, reveals, viewer interactions.
5. Write `clips.yaml` (`status: proposed`), up to the max per segment, best first. Give each clip a short `why` that cites the signals and timestamps. Pick a `layout` key from `layouts.yaml`.
   - **Dead air in a Short:** a gap of 10 s or more with nothing said (usually ARIA looking something up, 25 to 40 s) gets a jump-cut: `cuts: [{start, end, reason}]`, leaving about 0.5 to 1 s either side of the words. Shorts only; the Twitch clip keeps the full span, so `end - start` still has to fit 60 s. Keep the Short 20 s or longer after the cuts.
   - **At most 6 YouTube Shorts per stream (the streamer, 2026-10-14).** Across the whole session, only the 6 strongest clips get `short` in `targets`; the rest are `[twitch]`. Streams make 10 to 18 good clips but the channel posts 5 to 6 Shorts a day, so more Shorts only grow the backlog. Spread the 6 across the session's episodes where the moments allow (a Short needs its episode live to go out), and favour Japanese-learning moments and the games currently being streamed. Twitch clips aren't capped. Don't add `discord` to `targets`: Discord gets the long-form episodes only (the streamer, 2026-10-14).
6. To sanity-check a moment visually, run `stream-frames <session> --at <t>` and read the frame.
7. Set the session to `moments_proposed`. Show the streamer a compact table (id, segment, time, length, title, why) and ask for approval, edits or clips to drop. The streamer can also edit the file directly.

Don't render anything here.
