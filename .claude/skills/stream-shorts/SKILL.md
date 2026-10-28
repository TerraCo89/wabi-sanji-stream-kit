---
name: stream-shorts
description: Render approved clips from a stream session as captioned 9:16 YouTube Shorts in the Wabi Sanji style, preview them, and write social copy.
argument-hint: "<session id> [clip id ...]"
---

# /stream-shorts

Spec section: "Rendering Shorts". Args: **$ARGUMENTS**

1. Require `clips.yaml` `status: approved`. Render only clips with `approved: true` whose `targets` include `short`.
2. For each clip, fix obvious caption mishears in `caption_fixes` first. Read the transcript slice: game names, Japanese words and viewer names are the usual misses.
3. Run `uv run --directory tools/stream-pipeline python pipeline.py stream-render-shorts <session> [--clip <id>]`.
4. Preview: extract a frame at about 25% and 75% of each render (`stream-frames --at`, or ffmpeg into `shorts/_preview/`), `Read` them, and check the crop, caption placement, that the facecam isn't cut off, and that the twitch.tv/WabiSanji badge (top centre) isn't covering anything that matters (`badge: false` on the clip drops it). Re-render with layout tweaks if needed.
5. Write `shorts/<id>.copy.md`: YouTube Shorts title (under 70 chars), 2-line description, 3 to 5 hashtags (#wabisanji plus game tags). No em dashes and no "It's not X, it's Y" lines.
6. Set the session to `rendered` once everything approved is rendered. List the output paths for the streamer to watch.
