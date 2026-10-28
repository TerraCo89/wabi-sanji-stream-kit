---
name: stream-publish
description: Publish a packaged stream session. Uploads to YouTube (private or scheduled, via stream-upload), creates Twitch clips from the VOD, and posts links to the Discord clips channel. Only when the streamer runs it.
argument-hint: "<session id> [--dry-run] [youtube|twitch|discord]"
disable-model-invocation: true
---

# /stream-publish

Spec section: "Publishing". Args: **$ARGUMENTS**

Always do a dry run first and show it to the streamer. Publish for real only after the streamer confirms in this conversation.

1. Preconditions: `status: packaged` (VODs) and/or `rendered` (Shorts), approved `clips.yaml`, `.env` keys present (check names only, never print values).
2. Read `publish.yaml`. Anything with an ID is already done; never redo it.
3. `uv run --directory tools/stream-pipeline python pipeline.py stream-publish <session>` (a dry run unless `--go`) and show the plan: the upload checklist entries, episode numbers, Twitch clip offsets (VOD start is measured from the VOD's `created_at`), and Discord posts. Real runs: `--go`, optionally `--only twitch|discord|youtube` and `--clip <id>`. Record uploads with `--youtube <id>=<url> --go`.
4. **YouTube uploads go through the API** (`stream-upload`, the publishing app set up 2026-10-13; uploads can be scheduled and go public). Run `stream-number` first. If any series in this session isn't `numbering: locked`, stop and say which backlog sessions need segmenting first. `stream-upload <session>` is a dry run; show it, then ask the streamer when each item goes public (the slots are in `sessions/_posting_schedule.md`). Real run: `--go` with `--schedule <id>=<ISO time with offset>` per item; `<id>=now` only when the streamer asks for public straight away. Anything without `--schedule` stays private. Episodes get thumbnail pick1.jpg and their series playlist; a series with no `playlist_id` uploads without one. Append the run to `sessions/_upload_run.log`. `upload-checklist.md` (written by the dry run) stays as the Studio fallback.
5. `stream-upload` records each URL in `publish.yaml`. For anything the streamer uploaded in Studio by hand, record it with `stream-publish --youtube <id>=<url> --go`, or find it by exact title with videos.list (read-only).
6. Twitch clips need the VOD to still exist (14 days). If it's gone, skip and note it. Before the first clip of a session, run `stream-vod-offset <session>`: the VOD's created_at is not its first frame (5 s off on 2026-10-05), so clip times use the frame-matched offset. After creating clips, spot-check one: its thumbnail is its first frame.
7. Discord: one post per long-form episode (title, first line of its description, YouTube link) to the clips webhook. Shorts and Twitch clips aren't posted to Discord (the streamer, 2026-10-14: it spammed a channel followers already follow).
8. Discord posts need the episodes' YouTube URLs, so they go last. On success, set `status: published` and list every URL.
