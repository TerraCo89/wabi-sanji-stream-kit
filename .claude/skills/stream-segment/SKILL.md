---
name: stream-segment
description: Split a stream recording into segments (Starting Soon, Japanese study, each game, IRL, ending) and draft session.yaml. Uses Twitch/OBS event logs when present, otherwise frame contact sheets. Used by /stream-post and /stream-backlog.
argument-hint: "<session id>"
---

# /stream-segment: draft session.yaml

Spec sections: "session.yaml" and "Segmenting" in `docs/post-stream-spec.md`. Session: **$ARGUMENTS**

## Steps

1. **Event logs first.** If there's a category-change log (EventSub JSONL) or OBS scene log covering the recording, convert event times to offsets (event time minus `recording_start`) and use those as boundaries. Still sample frames to confirm.
2. **Frames.** Run `stream-frames <session>` (1 per minute) and `stream-sheets <session>`. `Read` every contact sheet in order and note what's on screen per minute:
   - Starting Soon / BRB / ending cards: skip
   - VS Code or Claude Code on screen: coding (inside a Japanese segment this is a chapter, not its own segment, unless it's long and standalone)
   - IRL or walk footage (Pokemon GO and other "go" games): `type: talk`, `series: Wabi Sanji Outside`, `game` = the app, `label` = the hook ("Pokemon GO Walk"). Cut IRL BRB cards. Skip it only if the camera or audio is broken
   - Game title screens, menus, gameplay: name the game. Japanese games with the vocab overlay column: `type: japanese`
3. **Refine.** For each boundary, run `stream-frames <session> --fine <a>-<b>` over the minute either side, read the fine sheet, and set the cut to the nearest 10 s.
4. **Write `session.yaml`** with `status: draft`: segments, chapters, `series` names (check `content/stream/series.yaml` for existing series; add a new series there with `numbering: open`), flags (`under_10_min`), and a `reason` on every skip.
   - **One episode per game per stream.** If a game is played in several stretches (around a BRB, or around another game), make it ONE segment from its first start to its last end, with the in-between stretches in `cuts: [{start, end, reason}]`. Other segments can sit inside a cut. Cut trailers and other unwanted stretches the same way.
   - **Other people's audio.** Flag anything that would be another creator's content in the episode: a trailer, a guide video playing, another streamer's broadcast audio. The streamer decides: cut it, or credit and link it.
   - **Recording shorter than the Twitch VOD?** Compare with the VOD length (`_backlog.yaml` or Helix). If the stream ran on after OBS stopped recording, offer `stream-vod-offset` then `stream-vod-tail <session> --go` to add the rest from the VOD (check what's in it before keeping it).
5. **Unsure about a game name?** Say so in the report and suggest checking SullyGnome's stream list for that date. Don't guess.

## Gate 1

Show the streamer a table: time range, type, game, notes. Ask for approval or corrections. Once it's approved, set `status: segments_approved` and write a first-line comment `# APPROVED by the streamer <date>`.
