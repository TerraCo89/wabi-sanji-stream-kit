---
name: stream-status
description: Show the board of stream sessions (status, segments, clip counts, what's waiting on the streamer) and the next command for each.
argument-hint: "[session id]"
---

# /stream-status

Run `uv run --directory tools/stream-pipeline python pipeline.py stream-status [<session>]`. If it doesn't exist yet, read each `<sessions root>\*\session.yaml` and `clips.yaml` directly.

Show one row per session: id, status, publishable segments (games), clips proposed/approved, and **waiting on** (the streamer gate, GPU step, publish). Put sessions waiting on the streamer at the top. End with the single most useful next command.
