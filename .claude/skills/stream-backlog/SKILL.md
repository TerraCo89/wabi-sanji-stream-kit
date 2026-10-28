---
name: stream-backlog
description: Work through the backlog of past stream recordings in <recordings root> by inventorying them, telling streamed sessions from local tests and false starts, and drafting segments in batches so the streamer can approve many at once.
argument-hint: "[inventory | segment <n sessions> | transcribe | moments | status]"
---

# /stream-backlog

Spec section: "Backlog". Args: **$ARGUMENTS** (blank = `status`)

Only start the backlog once the workflow is locked in on the 2026-10-05 test session (check the Phases in the spec).

- **inventory:** run `stream-inventory`. List recordings in the `<recordings root>` root plus the 4 older files in `<recordings root>\WabiSanji\`, with date, size and duration. Classify each as likely stream, local test, or false start (under about 0.2 GB or under 5 min). Cross-check stream dates with the streamer (or SullyGnome's stream list). Several files close together on one day may be one stream split by an OBS restart, or separate streams: ask. Write the result to `sessions/_backlog.yaml` and show it for confirmation.
- **segment N:** for the next N confirmed recordings, `stream-init` then the `stream-segment` skill, but don't stop per session. Collect all the draft tables and present them together for one Gate 1 review.
- **transcribe:** run `stream-transcribe` for all `segments_approved` sessions, oldest first. Long run: report progress per session.
- **moments:** run `stream-moments` for all transcribed sessions and present the clips for one Gate 2 review.
- **status:** same as `/stream-status`.

Never touch the unrelated folders in `<recordings root>` (adhd-*, memes, Resolve Project Backups, CacheClip, Audio, _proof_*, _review_*, _magenta_test).
