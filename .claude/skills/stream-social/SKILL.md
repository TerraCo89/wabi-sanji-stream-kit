---
name: stream-social
description: Draft Instagram Reel captions for a stream session's rendered Shorts into social.yaml for the streamer to approve, and post the approved ones when the streamer asks. Instagram posts are public straight away.
argument-hint: "<session id> [clip id ...] [post]"
---

# /stream-social

Spec section: "Publishing" (Instagram). Args: **$ARGUMENTS**

Commands run from the repo root as `uv run --directory tools/stream-pipeline python pipeline.py <cmd>`.

**Instagram has no private or scheduled stage.** A Reel is public the moment it posts. Claude drafts and the streamer approves. Never set `approved: true` yourself, and never run `--go` unless the streamer has asked you to post in this conversation.

## Draft (default)

1. Preconditions: approved `clips.yaml`, session at least `rendered`, `shorts/<id>.mp4` present.
2. `stream-social <session> --init` adds an empty, unapproved entry for every approved Short that isn't in `social.yaml` yet. Existing entries are the streamer's: don't rewrite a caption the streamer has edited or approved unless asked.
3. For each empty caption, read `clips.yaml` (title, hook, why), `shorts/<id>.copy.md` (the YouTube copy), and the transcript slice in `transcript/seg<N>.txt` around the clip. For Japanese clips, also read `signals/vocab.json` near the clip's time. Write the caption into `social.yaml` (`items.<id>.instagram.caption`, a `|` block):
   - Line 1: the hook, in the streamer's voice. Instagram cuts the caption after about 125 characters, so the first line has to work on its own.
   - 1 to 3 short lines on what happens. For a Japanese moment, give the word or line as it appears in the clip: Japanese, then reading, then meaning, e.g. `読めた (yometa): "I could read it!"`.
   - `Full streams on Twitch, episodes on YouTube. Link in bio.` No URLs: they aren't clickable in captions.
   - A final line of 5 to 10 hashtags: `#wabisanji`, the game, and topic tags that fit (`#learnjapanese #japanese #日本語` for Japanese clips; `#retrogaming` etc. for games).
   - Follow `docs/content-rules.md` "Writing": first person, plain, Australian English, no em dashes, no "It's not X, it's Y" lines, no hype words, and nothing that isn't in the transcript. Kids' real names never appear.
4. Optional `cover_ms`: if the first frame is a weak cover (black, mid-blink, a menu), pick a stronger moment from `shorts/_preview/` and set `cover_ms` to its time in the Short, in milliseconds.
5. Run `stream-social <session>` (a dry run) and fix anything it marks blocked (length, hashtags, links, em dashes).
6. Tell the streamer the file path, show each caption's first line, and say: set `approved: true` on the ones to post, then run `stream-social <session> --go` (or ask me to post them).

## Post (only when the streamer asks)

1. `stream-auth instagram --check`. If it reports a problem, stop and tell the streamer (re-login: `stream-auth instagram`).
2. Run a dry run first and show the streamer exactly which items will go public. Only entries with `approved: true` are ready.
3. After the streamer confirms, run `stream-social <session> --go` (optionally `-i <id>`). Each Reel is recorded in `publish.yaml` (`instagram:`) as soon as it posts. Reruns skip it, and a Reel already on the account with the same caption gets recorded instead of posted again.
4. If it stops on a login or rate-limit error, report it and stop. Instagram allows 100 API posts per rolling 24 hours.
5. List the permalinks.
