---
name: stream-archive
description: Tidy a published stream session by moving the raw recording into the session folder or archive, and removing regenerable intermediates. Only when the streamer runs it.
argument-hint: "<session id> [--dry-run]"
disable-model-invocation: true
---

# /stream-archive

Args: **$ARGUMENTS**

1. Require `status: published` (or the streamer explicitly says to archive anyway).
2. Run `uv run --directory tools/stream-pipeline python pipeline.py stream-archive <session> --dry-run` and show what moves and what gets removed:
   - **Moves:** raw recording into the session folder, or the archive location the streamer has chosen.
   - **Removable (regenerable):** `frames/`, `fine/`, temp WAVs.
   - **Never removed:** session.yaml, clips.yaml, publish.yaml, transcripts, rendered Shorts, VOD files (unless the streamer says so).
3. Run for real only after the streamer confirms. Raw recordings are never deleted by this skill.
4. Set `status: archived` and report the space freed.
