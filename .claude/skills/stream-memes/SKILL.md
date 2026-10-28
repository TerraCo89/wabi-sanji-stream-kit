---
name: stream-memes
description: Pick meme pop-overs for each episode of a transcribed stream session (the editor's job, Real Civil Engineer style but sparse) and write memes.yaml. No approval gate; /stream-vods burns them in.
argument-hint: "<session id> [segment N]"
---

# /stream-memes: memes an editor would have added

ARIA already popped memes live, and those are in the recording. This adds the
ones an editor would: a meme that answers the line the streamer just said. The streamer chose
"punchy but sparse" (2026-10-20). There's no gate: `stream-cut-vods` burns these
in, and each episode's "Check before publishing" note lists them. Args: **$ARGUMENTS**

## Steps

1. Needs `status: transcribed` or later (the `transcript/seg<N>.txt` files).
2. Run `uv run --directory tools/stream-pipeline python pipeline.py stream-memes <session> --library` and keep the list
   in mind. Only these ids exist. Then run `stream-memes <session>` to see each
   segment's density cap, ARIA's live memes and whether family is in it.
3. For each publishable segment, read `transcript/seg<N>.txt` in full. Look for a
   **setup line with a payoff a meme can answer**:
   - The streamer's own reaction: "oh no", "why is it so big", "I've got this", "wait, what".
   - A clear game beat in the words: a death, a win, a reveal, a dumb mistake, a
     time skip, a number like 69.
   The meme goes **right after the line ends**: `at` is the end time of the line
   from the transcript, plus 0.3 s or so.
4. Pick the best ones, about one every 3 to 5 minutes of episode, never more than
   the cap. A mediocre fit is worse than no meme. Spread them out and vary them:
   no meme more than twice per episode, apart from running gags (noice on a 69).
5. Write `memes.yaml` in the session folder (all segments in one file, recording times):
   ```yaml
   memes:
     - segment: 4
       at: "01:12:31.4"
       id: oh-no-anyway
       line: "and the whole base is on fire"   # the setup line, verbatim
       why: "base burns down, the streamer shrugs and keeps building"
   ```
6. Run `stream-memes <session>` again. It applies the rules (cuts, gaps, live memes,
   cap, family) and prints `add`/`drop` per pick. Fix anything dropped for a reason
   you can fix (move `at` out of a cut, swap a not-family meme). Accept the rest.
7. Tell the streamer in a short table: segment, time, meme, the line. Then carry on with
   `/stream-vods`. If episodes were already cut, re-cut with `stream-cut-vods
   <session> --segment N --force` (GPU: ask first if OBS may be live).

## Rules

- **Japanese study segments:** no memes over the streamer reading a line or working out a
  word; the learning moment is the content. Use the Japanese category (nani, objection,
  omae wa mou shindeiru) for reactions around it, sparingly.
- **Family:** memes marked `NOT family` never go in a segment with the kids (the check
  enforces it) or in any moment involving kids, family or anything sad or serious.
- **Viewer moments:** never meme a viewer's personal news, a raid thank-you or a sub.
- **Content ID:** med and high risk memes are TV, film and game audio. The streamer accepted
  occasional claims on episodes (2026-10-20). Shorts don't get added memes.
- Never invent ids, and never meme a line that isn't in the transcript.
