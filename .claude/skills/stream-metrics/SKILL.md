---
name: stream-metrics
description: Pull YouTube, Instagram and Twitch numbers for every published Short, Reel, clip and episode, and report what content performs best on which platform and which kinds of stream do best.
argument-hint: "[--twitch-csv <path>] [--public-only]"
---

# /stream-metrics

Spec section: "Metrics" in `docs/post-stream-spec.md`. Read-only on every platform.

1. Run `uv run --directory tools/stream-pipeline python pipeline.py stream-metrics`. It writes `<sessions root>\_metrics_report.md` and a raw snapshot in `sessions/_metrics/<date>.json`.
2. If it notes missing permissions, tell the streamer the one-time fix (on the PC, browser login):
   - YouTube watch time and retention: `stream-auth youtube`.
   - YouTube impressions and CTR: enable the YouTube Reporting API in the pipeline's Google Cloud project (the note has the link). The first run then sets up a reporting job; YouTube writes daily reports from about two days later, kept in `sessions/_metrics/reach/`. The report's thumbnail section compares each episode's CTR 14 days before and after its thumbnail changed (`seg<N>_thumbs/pushed.json`).
   - Instagram views, reach, saves, shares: `stream-auth instagram`. If the app uses Facebook Login for Business (`META_LOGIN_CONFIG_ID`), `instagram_manage_insights` must also be added to that login configuration in the Meta dashboard first.
3. Twitch: follows per day come from the API (`stream-auth twitch` once for `moderator:read:followers`). Viewers per game come from `stream-viewers` logs in `sessions/_viewers/` (it must be running while the streamer is live: `uv run --directory tools/stream-pipeline python pipeline.py stream-viewers`, or `--forever` as a startup task). History before the logger comes from CSVs the streamer exports: dashboard.twitch.tv/u/wabisanji/analytics/viewer-engagement, Day view, Export Data. It shows 30 days at a time, so export each period back to 2026-09-08 (the Revenue page export can add subs and time streamed). Every CSV in `sessions/_metrics/` is merged by day (or pass `--twitch-csv` per file). If the export has columns the parser doesn't know, the report's notes list them: map them in `metrics.TWITCH_COLUMNS`.
4. Read the report in full, then write the streamer a short take: what wins on each platform, where the same clip does differently across platforms, which games or stream types draw viewers and follows, and the caveats that matter (sample size, post age, the channel's early posts having no audience yet). Compare content from the same stream where you can: it controls for channel growth. Plain Australian English, no em dashes, nothing the numbers don't show.
5. Don't change posting plans or schedules from this. Suggest; the streamer decides.
