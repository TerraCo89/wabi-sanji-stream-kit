---
name: stream-post
description: Run the post-stream pipeline for one stream recording, from raw OBS file to an approved session.yaml and a clips.yaml ready for review. Use after a stream, or when the streamer names a recording in <recordings root>.
argument-hint: "<recording path | session id> (e.g. a .mkv from the recordings folder, or 2026-10-05)"
---

# /stream-post: recording to reviewable clips

Spec: `docs/post-stream-spec.md` (read it first if this is your first run in the session). Input: **$ARGUMENTS**

This skill is a resumable state machine. Read the session's `status` and run only the next step. Stop at each gate.

Commands run from the repo root as `uv run --directory tools/stream-pipeline python pipeline.py stream-<cmd>`. If a command doesn't exist yet, say which build phase it belongs to and stop. Don't improvise ffmpeg pipelines in its place.

## 0. Resolve the session

- A path to a recording: find a session whose `recording` matches. If there isn't one, run `stream-inventory` (classifies it and finds its Twitch VOD), then `stream-init "<path>" --vod-id <id>` (the VOD lets Twitch clips be made, within about 14 days).
- A session id: open `<sessions root>\<id>\session.yaml`.
- If OBS looks live (the newest file in `<recordings root>` is still growing), ask before running GPU steps.

## 1. status: draft (no approved segments)

Run the `stream-segment` skill. It drafts segments and stops for **Gate 1**: The streamer approves `session.yaml`. Then stop.

## 2. status: segments_approved

Run the `stream-transcribe` skill for every non-skip segment, then set `status: transcribed`.

## 3. status: transcribed

Run `stream-signals <session>`, then the `stream-moments` skill. It writes `clips.yaml` and sets `status: moments_proposed`. Stop for **Gate 2**.

## 4. status: moments_proposed

Check `clips.yaml`. If the streamer has approved it (`status: approved`), set the session to `clips_approved` and tell the streamer the next commands are `/stream-shorts` and `/stream-vods`. If not, summarise what's waiting and stop.

## 5. status: rendered / packaged

Publishing is `/stream-publish` (the streamer runs it). Instagram Reels: `/stream-social <session>` drafts captions into `social.yaml` for the streamer to approve. Reels are public on posting, so only post when the streamer asks. Before the first Twitch clip of a session run `stream-vod-offset <session>`: the VOD's created_at isn't its first frame. Episodes only get a final number once their series is `numbering: locked` (run `stream-number`); for a series with open numbering, say which backlog sessions still need segmenting.

## Report

End with: session id, status before and after, what the streamer needs to review (file paths), and the next command. Keep it short.
