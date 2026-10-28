# Post-stream pipeline spec

Turn every Twitch stream recording into per-game YouTube episodes, YouTube Shorts, Instagram Reels, Twitch clips and Discord posts, with Claude Code skills driving it and the streamer approving at two gates.

Status: designed in Cowork on 2026-10-05, first test session segmented and approved. Build it phase by phase (see Phases). Kickoff prompts are in `docs/post-stream-kickoff.md`.

## Goal

After a stream, the streamer runs `/stream-post` and comes back to:

1. `session.yaml`: the recording split into segments (Starting Soon, Japanese study, each game). **Gate 1: The streamer approves.**
2. Transcripts and moment signals per segment.
3. `clips.yaml`: 5 to 10 candidate moments per segment. **Gate 2: The streamer approves or edits.**
4. Rendered Shorts, one YouTube VOD file per segment with metadata and thumbnail picks.
5. `stream-upload` uploads to YouTube (private or scheduled), `/stream-publish` creates Twitch clips and posts to Discord, and `/stream-social` drafts Instagram captions into `social.yaml` (a third gate: Reels are public on posting).

The same pipeline works through the backlog of past recordings in `<recordings root>`.

## Decisions (binding)

- **One YouTube video per segment.** No full-length VOD upload. Each segment goes in its game's playlist with an episode number from `series.yaml`.
- **Episodes are numbered chronologically by stream date** within each series, starting from the earliest backlog stream. The 2026-10-05 pipeline test gets whatever number its date gives it, not #1 by default. Numbers are assigned once every earlier session of that series is segmented, and never by upload order.
- **YouTube uploads go through the API** (`stream-upload`): private by default, `--schedule <id>=<time>` to go public at a set time, `<id>=now` only when the streamer asks for public straight away.
- **Two human gates:** `session.yaml` and `clips.yaml`. Nothing renders or publishes before its gate is approved. Nothing goes public automatically.
- **Skip, don't publish:** Starting Soon, ending screens, and segments under about 10 minutes (flag them instead).
- **Discord:** post each long-form episode's YouTube link through a channel webhook into `clips・クリップ`. Not Shorts or Twitch clips (the streamer, 2026-10-14: they spammed a channel followers already follow). Do not upload video files (10 MB limit on unboosted servers).
- **GPU work runs here on Windows** (NVIDIA GPU). The `tools/stream-pipeline` uv env runs faster-whisper on CUDA without torch: the `gpu` extra installs the CUDA libraries as pip wheels.
- **Backlog first test, then batch.** Lock the workflow on `2026-10-05`, then process the rest.

## Where things live

| What | Where |
|---|---|
| Raw recordings (OBS output) | `<recordings root>\<YYYY-MM-DD hh-mm-ss>.mkv` (older ones `.mp4`) |
| Session working folders | `<sessions root>\<YYYY-MM-DD>[_<n>]\` (env `WS_SESSIONS_ROOT`) |
| Code | `tools/stream-pipeline/src/wabisanji_post/` + `stream-*` commands in its `pipeline.py` |
| Layout crops for Shorts | `tools/stream-pipeline/src/wabisanji_post/layouts.yaml` (or `WS_LAYOUTS`) |
| Series registry | `content/stream/series.yaml` (tracked in git) |
| Secrets | `.env` at repo root (gitignored): see Phase 0 |
| Vocab capture log | `apps/screenshot-parser/data/wabisanji.db` (read only from this pipeline) |
| Streaming docs | your own notes folder |

Run commands from the repo root as `uv run --directory tools/stream-pipeline python pipeline.py stream-<cmd> ...`. Paths and channel settings come from `.env` (see `.env.example`).

## Session folder layout

```
sessions/2026-10-05/
  session.yaml            # segments, status, audio track (gate 1)
  frames/                 # 1 frame/min, 480 px wide, f_<seconds>.jpg
  fine/                   # ~10 s frames around boundaries
  at/                     # single 960 px stills from stream-frames --at
  sheets/                 # contact sheets Claude reads (20 frames, 5 cols, timestamp + segment labels)
                          #   sheet_NN.jpg (per minute), fine_<a>-<b>.jpg (one per boundary run)
  transcript/seg<N>.words.json, seg<N>.txt, seg<N>.meta.json
  audio/                  # temp 16 kHz WAVs while transcribing (removed afterwards)
  signals/vocab.json, loudness.json, markers.json, chat.jsonl (optional)
  clips.yaml              # candidate moments (gate 2)
  shorts/<clip-id>.mp4, _captions/<clip-id>.ass   # .ass kept out of the mp4 folder: players auto-load same-name subs
  vods/seg<N>_<slug>.mp4, seg<N>.youtube.md, seg<N>_thumbs/
  shorts/_instagram/<clip-id>.mp4   # smaller copies of Shorts over 80 MB (Instagram's upload fails on big files); regenerable
  social.yaml             # Instagram captions + approved flags (drafted by /stream-social, approved by the streamer)
  publish.yaml            # every uploaded ID/URL; makes publish idempotent
```

The raw recording stays in `<recordings root>` until `/stream-archive`. Remux to MP4 only when a step needs it (cutting VODs with stream copy works from MKV directly).

## session.yaml

```yaml
session: 2026-10-05
status: segments_approved   # draft | segments_approved | transcribed | moments_proposed | clips_approved | rendered | packaged | published | archived
recording: <recordings root>\2026-10-05 10-37-11.mkv
recording_start: 2026-10-05T10:37:11+11:00   # from the OBS filename (local time)
duration: "02:26:38"
audio_track: 1              # ffmpeg a:<n>. MKV (3 tracks): 1 = Track 2, everything but Spotify. Single-track MP4 backlog: 0
streamed: true
twitch_vod_id: null         # fill when the VOD is still on Twitch (14-day window as Affiliate)
excluded_files:
  - file: <recordings root>\2026-10-05 10-16-02.mkv
    reason: local walk-n-talk test, not streamed
segments:
  - id: 2
    type: japanese          # skip | talk | japanese | gaming | coding
    game: Breath of Fire IV (JP)
    series: Learning Japanese with Breath of Fire IV
    start: "00:15:00"
    end: "00:51:50"
    chapters:
      - { at: "00:15:00", title: Setting up the wabisanji parser for Breath of Fire IV }
      - { at: "00:32:00", title: Gameplay with the vocab overlay }
    reason: null            # required when type is skip
    flags: []               # e.g. under_10_min; shorts_only = clip source only, never an episode
    # full_of: 1            # optional: the full version of segment 1 (e.g. the whole watch-along behind a response edit).
    #                       # Own episode file, same episode number, may overlap segment 1, titled "#1 (Full Version)".
```

Times are offsets into the recording as `HH:MM:SS[.s]`. Validate with pydantic: segments sorted, non-overlapping, inside duration, `reason` set for skips.

**One episode per game per stream (the streamer, 2026-10-05).**
- A game played in several stretches of one stream (around a BRB, or around another game) is one segment, with the in-between stretches listed as `cuts: [{start, end, reason}]`. A trailer or other unwanted stretch is cut the same way.
- Other segments may sit inside a cut, e.g. World of Warcraft inside the Alchemy Factory cut on 2026-09-21_2. Kept parts may not overlap.
- Everything downstream uses only the kept parts: transcripts, loudness, thumbnails, clips (which may not cross a cut), chapters and the episode file.

## Segmenting

Signal priority:

1. **Twitch category changes.** Future streams: an EventSub `channel.update` listener (a Twitch dev app, run on an always-on machine next to the chat logger) logs `{utc, category, title}` to a JSONL file. Offset = event time minus `recording_start`.
2. **OBS scene switches.** OBS WebSocket `CurrentProgramSceneChanged` events, or the OBS log files for recent sessions (OBS keeps only the most recent logs).
3. **Visual fallback (the backlog uses this).** Proven in Cowork on 2026-10-05:
   - One frame per minute at `t = 30, 90, 150, ...` using input seeking (`ffmpeg -ss t -i file -frames:v 1 -vf scale=480:-1`). 147 frames for a 2h26m file took about 30 s.
   - Contact sheets of 20 frames (5 columns) with a yellow `H:MM:SS` label on each tile. Claude reads the sheets and names what is on screen (Starting Soon card, VS Code / Claude Code, IRL footage, game title screens, gameplay).
   - Refine each boundary with frames every 10 s across the minute either side.
   - The transcript later pins exact cuts ("okay, let's switch to...").
   - Cross-check game names against SullyGnome's stream list for the channel when unsure.

Twitch has no API or export for past category history (Helix returns only the current category; VOD chapters exist only while the VOD does; the Analytics CSV is daily totals).

## Transcription

- faster-whisper on CUDA, float16, `word_timestamps=True`, `vad_filter=True`.
- Transcribe each non-skip segment separately from the session's `audio_track`, cut with ffmpeg to 16 kHz mono WAV (`audio/seg<N>.wav`, deleted afterwards unless `--keep-audio`).
- Output `seg<N>.words.json` as `[{"w", "s", "e"}]` with times as **recording offsets** (add the segment start), matching the word format in `shorts.py`, plus a readable `seg<N>.txt` with `[HH:MM:SS]` lines and `seg<N>.meta.json` (model, settings, timing).

### Chosen settings (tested 2026-10-05)

`stream-transcribe` defaults: **`large-v3-turbo`, `language="en"` for every segment type (Japanese included), `vad_filter=True`, `beam_size=5`, `condition_on_previous_text=True`, initial prompt = game + series names + "Wabi Sanji stream, Twitch chat."** faster-whisper 1.2.1, CTranslate2 4.7.1, NVIDIA GPU.

Model comparison on 00:40:00 to 00:45:00 of 2026-10-05 (segment 2, the streamer reading Breath of Fire IV dialogue and chatting). Ground truth from the on-screen dialogue boxes:

| | large-v3 | large-v3-turbo |
|---|---|---|
| Speed (warm, 5 min slice) | 6.3 s, ~48x realtime | 2.3 to 4.5 s, 70 to 130x |
| Model load | 55 s first run (includes download) | 35 s first run (includes download), 4 s cached |
| English | Good; one mishear ("don't you only go for a year" for "done Duolingo for a year") | Better ("done Duolingo for like a year") |
| Japanese read aloud | Slightly cleaner romaji ("Watashi o osutekitan dashou ka") | Choppier romaji ("Toshii, o, osu, te, ki, tan, da shou ka") |
| Dropouts | **Dropped 00:40:00 to 00:40:27** (the streamer reading 「ここまで来て あわてる事はない」) with the prompt on | Kept it ("…Up to here so far, yep", heard *awateru* as "how are you") |

Turbo wins: same or better English, no dropout, about twice as fast. Full segments with turbo: segment 2 (36:50) in 24 s (~90x), segment 3 (1:34:20) in 39 to 45 s (~130x); 72 s wall time for the session including audio cuts and model load.

**Japanese segments (mixed English + Japanese speech):**

- `language="en"` keeps the Japanese the streamer reads aloud as **romaji** inline with the English ("Ashita, that's tomorrow", "Deijoubu desu… Are you okay?", "Iwakagi… Mitsukite"). This is what we use. Phase 3 can match vocab captures by converting their readings to romaji and fuzzy-matching.
- `multilingual=True` is supported in 1.2.1, but it detects language per 30 s decode window, not per phrase. Every window came out English (p ≥ 0.97) and the output was identical to `en`.
- `language="ja"` translates the streamer's English into Japanese ("僕のボスと一緒にこれを作って…"). Its average log-prob was lower than `en` on every segment, so "en + ja passes, keep the higher-probability text" always picks `en`.
- Per-segment re-decode (en pass, then `detect_language` on each segment's audio and redo it as `ja` if Japanese wins) flipped only 2 of 38 segments. The streamer's reading is mixed into English segments and accented, so detection rarely fires. The kana it produced (わたしをおすってきたんだしょうか) was no more useful than the romaji. Not used.

**Other settings tested:**

- `vad_filter=False`: 3x slower, and hallucinates over game music ("Oh? Oh? Oh?", "Or something." ×3, "Thank you." on silent stretches). Keep VAD on. In segment 3, VAD removed 64 of 94 minutes. The three largest gaps (69 to 104 s) were checked without VAD: Silero found no speech and Whisper produced only "Thank you." hallucinations, so they are genuine quiet building stretches.
- `condition_on_previous_text=False`: lost two read-aloud lines and wrote one line twice (kana, then romaji). Keep it on.
- `hallucination_silence_threshold=2`: no change. Not used.
- Without the initial prompt, large-v3 recovered the 00:40:14 line but turbo dropped it. Keep the prompt.
- Known small artefact: one repeated phrase ("I don't know what that means" at 00:44:49, segment 2). Watch for repeats when checking captions.

Viewer names are the usual mishears (Asmodean / Asmodeen / Aspideen). Fix them per clip with `caption_fixes`.

### Audio finding (2026-10-05 recording)

Track 1 and Track 2 carry the same full mix; Track 3 is silent. There is no isolated mic, game or music track, so the backlog is transcribed from the mix (`audio_track: 0`). The streamer is fixing OBS routing: Track 1 full mix, Track 2 mic, Track 3 game/desktop, Track 4 music.

**Update 2026-10-06: the routing was already music-safe.** OBS sends Game, Mic, Osmo Mic and webcam to Tracks 1 and 2, and Spotify to Tracks 1 and 3 only. Streaming uses Track 1; the Twitch VOD track and Track 2 have no Spotify. Recordings keep Tracks 1-3. So Track 2 (`audio_track: 1`) is the music-free audio for every 3-track MKV (2026-09-24 12:53 onward). Where Spotify wasn't playing, Tracks 1 and 2 are identical and Track 3 is silent (as on 2026-10-05). Spotify plays on Track 1 from 2026-09-28. Every MKV session uses `audio_track: 1` for transcripts, signals, Shorts and episode files. The single-track MP4s from before 2026-09-24 have only the mixed track, with Spotify in it if it was playing.

## Moment signals

| Signal | Source | Notes |
|---|---|---|
| Transcript | `transcript/` | Primary. Laughs, exclamations, "chat, look at this", breakthroughs |
| Vocab captures | `wabisanji.db` `screenshots` + `screenshot_vocabulary` + `vocabulary` | Screenshot file names carry local time (`Screenshot 2026-10-05 11-28-16.png`); `processed_at` is UTC. Offset = local time minus `recording_start`. Each capture = the streamer engaging with a line of Japanese |
| Loudness peaks | ffmpeg `ebur128` on `audio_track` | Short-term loudness spikes above the segment median |
| Stream markers | Helix Get Stream Markers | Only while the VOD exists. The streamer is adding a marker hotkey / chat command |
| Chat | The chat recorder (a small HTTP service at `RECORDER_URL` on an always-on machine): permanent daily archive, pulled by `stream-signals` into `signals/chat.json` (messages, subs/raids, per-minute bursts, chatters per segment; moderator-deleted messages left out) | Needs `RECORDER_TOKEN` (its `HISTORY_TOKEN`) in `.env`. Only streams after the archive update are covered |

### What makes a good clip

- **Japanese segments:** The streamer working out a word or line, funny misreadings, a vocab card popping on the overlay right as the word clicks, a "today I learned" moment. Context must make sense without the stream.
- **Gaming segments:** wins, fails, surprises, chat interactions, a clean "look what I built" reveal (Alchemy Factory), visiting a viewer's world.
- Shorts 20 to 60 s with a hook in the first 2 s. Twitch clips up to 60 s.
- Never clip Starting Soon, BRB, ending, or skip segments.

## clips.yaml

```yaml
session: 2026-10-05
status: proposed            # proposed | approved
clips:
  - id: c01
    segment: 2
    start: "00:41:12.5"
    end: "00:41:48.0"
    targets: [short, twitch]   # "discord" is accepted but ignored
    layout: japanese        # key in layouts.yaml
    title: "I finally read this line without help"
    hook: first spoken line or on-screen text
    why: vocab capture at 00:41:20 plus laugh at 00:41:31
    caption_fixes: []       # [[heard, correct], ...]
    cuts: []                # Short only: [{start, end, reason}] jump-cuts over dead air (an ARIA lookup wait).
                            # The Twitch clip is cut from the VOD in one piece and keeps them.
    approved: false
```

Gate 2 = the streamer sets `approved: true` (or deletes clips) and `status: approved`.

## Rendering Shorts

- 1080x1920. Stack the game area (top) and facecam (bottom) using rects from `layouts.yaml`; measure them from frames of each OBS scene (the Japanese scene has a 340 px vocab column on the left; gaming scenes have chat on the left and facecam bottom right).
- **Resolution (2026-10-19):** recordings are 2560x1440 from 2026-10-18_2. Every box the pipeline stores (layout crops, `blurs` in session.yaml, the face-cam cache, thumbnail `hide` boxes) is in 1920x1080 reference pixels, and `media.to_source` scales it to the recording's real size just before ffmpeg uses it. So 2K Shorts are cropped at full resolution, old 1080p sessions and layouts work unchanged, and episodes (including `stream-vod-tail` joins) keep the recording's size. When measuring a box from a full-res 2K frame, multiply by 0.75. OBS scenes scaled up to the 2K canvas keep their reference positions; a moved source (like the chat box bottom right on the 2026-10-18_2 desk scene, `desk_chat_right`) needs its own layout.
- Caption helpers (`slice_words`, `apply_caption_fixes`, `build_ass`) live in `captions.py`; extend them rather than fork.
- Captions in the Wabi Sanji palette: indigo `#2b2f55` (80% boxes), amber `#e8a04a` highlight, cream `#f2ecdc` text, Noto Sans JP. Japanese segments: when a captured vocab word is on screen, show a word card (word large, reading small amber, meaning cream), same style as the stream overlay.
- **Twitch badge** (the streamer, 2026-10-13): a pixel-art `twitch.tv/WabiSanji` badge (`stream/badge.py`, 496x152) at the top centre of every Short, under the captions. The panel and lettering are pixel art; the Glitch is drawn unchanged from Twitch's official geometry (white one-colour version on purple), because Twitch's brand rules don't allow altering it. A layout can move it (`badge: {y, x}`) or turn it off (`badge: false`, as the Japanese layouts do: the word card holds that band). A clip can opt out with `badge: false` in clips.yaml.
- Loudness normalise to -14 LUFS. NVENC encode.

## Segment VODs

- Cut each publishable segment with stream copy (`-ss` before `-i`, `-c copy`, map video + `audio_track`), starting on a keyframe.
- **Seeking:** a stream-copy seek to the keyframe's own time lands one keyframe early (seen on 2026-10-05: a seek to 40.0 started at 38.0). Seek to the middle of the keyframe interval instead; it lands exactly on the keyframe.
- **Segments with cuts:** each kept part is copied (the first starting on the keyframe at or before it, later ones on the keyframe at or after, so no BRB card leaks in) and the parts are joined with the concat demuxer. `vods/seg<N>.cut.json` maps each part to its position in the file for chapters.
- **Banners** (the streamer, 2026-10-10): a segment's `banners: [{start, end, text}]` draws a small notice across the top of the episode for that stretch (first use: "Mic muted for a few minutes. Back soon!" on 2026-10-10 seg 4). The banner uses the Shorts palette: indigo box, amber edge, cream Noto Sans JP Bold. Each banner must sit inside one kept part. A segment with a banner is cut as usual, then re-encoded once with NVENC at about 1.3x the source bitrate (GPU decode; the audio is copied; CQ 19 made a 17 GB, 16 Mbps file from a 4.3 Mbps recording), so its timeline and `cut.json` don't change. Re-encoding only the bannered stretch and joining it to stream copies was tried first. It failed: stream-copied pieces lead with the B-frame delay and run a few frames past their end keyframe, so frames overlapped at the joins.
- **Blurs** (the streamer, 2026-10-18): a segment's `blurs: [{start, end, box: [x, y, w, h], reason}]` blurs a box (recording pixels) for that stretch, for things on screen that shouldn't go out, like a viewer's personal news sitting in the chat overlay. Each blur must sit inside one kept part. It rides the banner pass (one re-encode of the whole file, blurs drawn first), and `cut.json` records it under `blurs`. Chat overlay messages stay up until newer ones push them out, so find when the lines clear (frames every 10 s) rather than blurring a minute. Cut what the streamer says about it with `cuts` as usual. First use: 2026-10-17_2 seg 1, a viewer's funeral message (cut 0:56:01-0:56:31, chat blurred to 1:12:51).
- **Memes** (the streamer, 2026-10-20): `/stream-memes` writes `memes.yaml` in the session folder.
  - **What's in it:** `[{segment, at, id, line, why}]`, using recording times and ids from the ARIA library (`content/stream/memes.yaml`). They're picked from the transcript, about one every 3 to 5 minutes, each answering the line before it (Real Civil Engineer's cutaway style, but sparse).
  - **No gate.** `wabisanji_post.memes.plan` enforces the rules. It drops picks that:
    - are unknown or missing their file
    - land in a cut, or run into one
    - are within 90 s of another pick, or 45 s of a live ARIA meme (from `apps/screenshot-parser/data/live/events.jsonl`)
    - go over one per 2.5 min
    - are not-family in a segment whose transcript mentions the kids
  - **Render:** the survivors ride the banner pass. They pop over the picture centred at 42% height, clips and images framed in amber like the live layer, sounds shown as their caption pill. The episode audio ducks to 35% under each meme's sound, so a segment with memes re-encodes its audio (AAC) instead of copying it.
  - **Records:** `cut.json` keeps `memes` and `memes_dropped`. `youtube.md` lists every live and added meme with its Content ID risk under "Check before publishing". `stream-memes <session>` shows the plan, and `--library` prints the library.
- **Recording stopped before the stream ended** (2026-09-18_3: 77 min missing): `stream-vod-offset`, then `stream-vod-tail <session> --go`. This downloads the rest of the VOD at source quality and NVENC re-encodes recording + tail into `<session>/source/<recording> + VOD tail.mp4`, and session.yaml points at that file. The original recording is untouched.
- `seg<N>.youtube.md`: title (series + episode, e.g. "Learning Japanese with Breath of Fire IV #1"), description with chapters (first chapter at 00:00), words-learned list for Japanese segments (from the vocab DB), tags, playlist. Follow the shape of `.claude/commands/youtube-meta.md` but in the Wabi Sanji voice. Drafted content: no em dashes and no "It's not X, it's Y" lines.
- Three thumbnail candidate frames per segment (reuse `thumbnail.py` where it helps).
- Episode numbers come from `content/stream/series.yaml`. `stream-number` rebuilds each series' `episodes` list from every session.yaml with approved segments, sorted by `recording_start`. A title only gets a final number when the series is `numbering: locked` (all earlier backlog sessions of that game are segmented). Until then use `#?` and don't publish.

## Publishing

- **YouTube:** `stream-upload <session>` uploads approved Shorts and episodes with the copy from `shorts/<id>.copy.md` and `seg<N>.youtube.md` (private, or `--schedule`), plus thumbnail and series playlist for episodes. It skips anything in `publish.yaml` or already on the channel with the exact title. `stream-schedule-sync` keeps `_posting_schedule.md` in step with YouTube.
- **Instagram Reels** (the streamer, 2026-10-14): every approved Short can also go out as a Reel, through the Graph API with Facebook Login (Creator account linked to the Wabi Sanji Facebook Page; setup in `docs/stream-setup.md`). Instagram has no private or scheduled state, so `social.yaml` is a gate: `/stream-social` drafts an Instagram-specific caption per Short (hook first line, the Japanese word with reading and meaning, "link in bio", 5 to 10 hashtags, no URLs) with `approved: false`, the streamer sets `approved: true`, and `stream-social <session> --go` posts only those, straight from disk with Meta's resumable upload (nothing hosted publicly). Shorts only for now: no long-form promotion, Stories or scheduling.
- **Twitch clips:** the Clip From VOD endpoint (open beta), scope `channel:manage:clips`. Needs Store past broadcasts on and the VOD still present (14 days as Affiliate). Read the current API reference before coding: beta clips have been reported ending a few seconds late, so verify and trim.
- **Discord:** webhook POST per episode: its title, the first sentence of its description and the YouTube link. Shorts and clips aren't posted (2026-10-14).
- Every created ID/URL goes into `publish.yaml`. Publishing checks it first so a rerun never duplicates an upload or post.
- **One publishing command at a time per session.** Each loads `publish.yaml` once and saves its own copy, so two running together overwrite each other's records (2026-10-15: a background `stream-upload` erased five Instagram records written by `stream-social`).

## Metrics

`stream-metrics` (read-only on every platform) joins every post in every `publish.yaml` to its content (segment type, game, series from `session.yaml` / `clips.yaml`), pulls the numbers and writes `sessions/_metrics_report.md`, with the raw rows in `sessions/_metrics/<date>.json` so later runs can track growth.

- **YouTube:** views, likes, comments (`youtube.readonly`); watch time, average view %, subscribers gained from YouTube Analytics (`yt-analytics.readonly`, added to `stream-auth youtube`). Analytics lags about two days. Thumbnail impressions and CTR aren't in the Analytics API: they come from the Reporting API's `channel_reach_basic_a1` daily reports (same scope; the API must be enabled in the Google Cloud project). `stream-metrics` creates the job once and downloads new days into `sessions/_metrics/reach/`; the report shows CTR by content type, per episode, and 14 days before against 14 days after each thumbnail change.
- **Instagram:** likes and comments; views, reach, saves, shares and average watch time need `instagram_manage_insights` (added to `stream-auth instagram`; with Facebook Login for Business, also add it to the login configuration in the Meta dashboard).
- **Twitch:** clip and VOD view counts from Helix, and follows per day from Get Channel Followers (`moderator:read:followers`, added to `stream-auth twitch`; current followers only). Live viewers per game come from the chat recorder, which polls Get Streams once a minute (app token) and serves the samples at `/v1/viewers`; `stream-metrics` pulls each stream's days into `sessions/_viewers/recorder_<date>.jsonl`. `stream-viewers` does the same from this PC (a fallback; samples are de-duplicated to one a minute); `stream-metrics` lays the samples over each session's segments. Twitch has no API for past viewer counts or chatters, so the history before the logger (from 2026-09-08) comes from the dashboard: dashboard.twitch.tv/u/wabisanji/analytics/viewer-engagement, Day view, Export Data, one export per 30-day period, into `sessions/_metrics/`. Every CSV there is merged by day, joined to sessions by date and shared between that day's games by minutes on stream. Chat per game comes from each session's `signals/chat.json`.
- Comparisons use total views of posts at least 3 days old (Shorts and Reels get most of their views in the first days), and an index per platform (a group's median over the platform's median), so platforms with different audiences compare. Early posts (the channel's first Shorts, 2026-10-05) had no audience yet: compare content from the same stream where possible.

## Backlog

- 36 recordings in `<recordings root>` root, 2026-09-15 to 2026-10-05, about 370 GB. Not every recording was streamed (`2026-10-05 10-16-02` was a local test). Tiny files (under about 0.2 GB) are likely false starts: flag, don't process.
- `<recordings root>\WabiSanji\` has 4 older game-named files from 2026-09-11 (Alchemy Factory 1-2, Star Wars Zero Company 1-2).
- Unrelated folders in `<recordings root>` (adhd-*, memes, Resolve backups, CacheClip, Audio) are never touched.
- Batch the gates: draft `session.yaml` for many sessions, the streamer approves them together, then the same for `clips.yaml`.
- Backlog sessions older than the Twitch VOD window get no Twitch clips. (On 2026-10-05 every VOD back to 2026-09-08 was still available.)
- Episode numbering depends on the backlog: segment all sessions of a game before publishing any episode of it.

## Phases

### Phase 0: prep (manual, the streamer)

- OBS audio routing (Track 1 mix, 2 mic, 3 game/desktop, 4 music). Check the live recording path is `<recordings root>`.
- Twitch: Store past broadcasts on. Token for the dev app with `channel:manage:clips`, `user:read:broadcast`.
- Google Cloud project with YouTube Data API v3 and an OAuth desktop client; download the client JSON.
- Discord: webhook on `clips・クリップ`.
- `.env` keys: `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TWITCH_BROADCASTER_ID`, `YOUTUBE_CLIENT_SECRETS` (path to JSON), `DISCORD_CLIPS_WEBHOOK`, optional `WS_SESSIONS_ROOT`, `WS_RECORDINGS_ROOT`.

### Phase 1: sessions + segmenting

`stream` package (now `tools/stream-pipeline`) with pydantic models for session.yaml / clips.yaml, and commands `stream-init <recording>` (create session folder, probe duration and audio tracks, `recording_start` from the filename), `stream-frames <session> [--fine a-b,...]`, `stream-sheets <session>`. Migrate `<recordings root>\WabiSanji\_work\2026-10-05_stream\` to `sessions\2026-10-05\` keeping the approved session.yaml.
Done when: regenerated sheets for 2026-10-05 match the approved segments, models validate it, tests cover the models and filename parsing.

**Done 2026-10-05.** Models: `stream/models.py` (unknown keys rejected; `label` and `notes` added as optional segment fields because the approved file uses them; clips cross-checked against session.yaml by `stream-validate`). Extra command `stream-validate <session>`. `stream-frames --at t` writes 960 px stills to `at/`. Sheet labels show the segment each frame falls in, so the sheets double as a check of the cut points. 147 per-minute frames take 8 s. The pipeline never re-dumps an approved session.yaml: status changes rewrite only the `status:` line, keeping the streamer's comments. The `_work` migration was already done in Cowork. Tests: `tools/stream-pipeline/tests/test_stream_*.py` (`uv run pytest tests`).

### Phase 2: transcription

`stream-transcribe <session> [--segment N] [--model]` per the Transcription section.
Done when: segments 2 and 3 of 2026-10-05 are transcribed on the GPU, speed and model choice are recorded in this spec, and the Japanese-segment language handling is decided.

**Done 2026-10-05.** See "Chosen settings" under Transcription. 2026-10-05 is now `status: transcribed`.

### Phase 3: signals + moments

`stream-signals <session>` (vocab from the parser DB, loudness peaks, markers if available). The `/stream-moments` skill reads transcripts + signals and writes `clips.yaml`.
Done when: 2026-10-05 has a clips.yaml the streamer can review.

**Done 2026-10-05.** `stream-signals` writes `signals/vocab.json`, `loudness.json` and `markers.json` in about 7 s for 2026-10-05:

- **Vocab:** the DB is opened read-only (`mode=ro`). Capture times come from the screenshot filename (OBS `Screenshot YYYY-MM-DD hh-mm-ss`, RetroArch `-yyMMdd-HHmmss`), with `processed_at` (UTC) only as a fallback. Each word's reading is converted to romaji and fuzzy-matched against the transcript within ±2 min, giving a `heard` list. Readings of 4 letters or fewer need an exact match. On 2026-10-05: 53 captures, 114 words, 17 heard (e.g. 必ず → "Kanara" at 00:34:03, だいじょうぶ → "Deijoubu" at 00:35:35). A capture next to its heard reading is the strongest Japanese-segment clip signal.
- **D: is a spinning hard drive.** Run anything that reads whole recordings (signals, frames, VOD cuts) one session at a time. In batch 2 (2026-10-06), 6 parallel `stream-signals` runs took 15 to 60 min each, left D: 100% busy with a queue of 12 at 24 MB/s, and slowed the streamer's YouTube upload to 0.38 MB/s. Run one at a time, they took 15 to 90 s each, and the upload went back up to 6.5 MB/s. Don't run disk-heavy steps while the streamer is uploading from D:.
- **Loudness:** ebur128 short-term loudness, sampled once per second. Peaks are at least 6 LU over the segment median and 20 s apart, top 40 per segment. Track 0 is the full mix, so game sound effects count too. On 2026-10-05 the strongest peaks landed on punchlines (01:46:59 "too close to work", 01:15:56 the Affiliate announcement).
- **Markers:** skipped. There's no `twitch_vod_id`, and the Helix markers call needs a user token (Phase 0/6).
- **Chat:** from the recorder's archive (see Moment signals). Streams before the archive update have no chat history: the recorder only kept its last 500 messages until then.

clips.yaml for 2026-10-05 has 16 proposals (8 per segment), with caption fixes for romaji and Duolingo. Layout keys are `japanese` and `gaming` until Phase 4 measures them. Session status is `moments_proposed`.

### Phase 4: Shorts

`layouts.yaml` measured from real frames, `stream-render-shorts <session>` for approved clips.
Done when: approved 2026-10-05 clips render and the streamer signs off the look.

**Done 2026-10-05: The streamer signed off the look** (after moving the `.ass` files to `shorts/_captions/`: players auto-loaded the same-name sidecar and drew the captions twice). c07 dropped as a Short (crosses scene switches). Shorts copy per clip in `shorts/<id>.copy.md`.

- **`layouts.yaml`:** `japanese` and `gaming`, measured from full-res frames.
  - Japanese game capture is 1600x900 at (228, 89).
  - Both scenes use a background-removed facecam over the game, bottom right. Its crop stays inside the game capture, because outside it the canvas is black.
  - The gaming crop starts at x 570 to clear the chat overlay, which grows upward as messages arrive.
- **Layout (Japanese):** word card band at the top, captions just under it (the dialogue box at the bottom of the game stays clear), game, then cam.
- **Layout (gaming):** game, captions along its bottom edge, then cam. Faces stay above y 1540, clear of the Shorts UI.
- **Captions:** `shorts.build_ass` gained an optional `CaptionStyle` (legacy output unchanged). Noto Sans JP Bold 70 in cream, on an 80 % indigo box, with the spoken word in amber, up to 3 words per chunk.
- **Vocab cards (Japanese only):** an indigo box with an amber edge showing word (Black 84), reading (amber 40) and meaning (36).
  - A card appears when the streamer says the word in the clip, otherwise at its capture time.
  - The clip opens on the last capture before it, matching the stream overlay.
  - Each card shows until the next one (at least 2 s).
- **Fonts:** Noto Sans JP is downloaded on first render into `templates/fonts/` (gitignored, about 14 MB).
- **Audio:** linear two-pass `loudnorm` fell short on peaky clips (-16.8 LUFS). We now measure with loudnorm, apply the exact gain, and run `alimiter` at -1.5 dBFS, oversampled 4x. Result: -14.2 to -15.6 LUFS, peaks ≤ -0.9 dBFS.
- **Video:** h264_nvenc p6 VBR CQ 20, 1080x1920 at 60 fps, about 12 to 16 Mbps. 13 clips render in about 110 s.
- **Previews:** `--still` renders one preview frame per clip for layout checks. A full render writes `shorts/_preview/<id>_25.jpg` / `_75.jpg` and `sheet.jpg`.
- **Known limits:**
  - Clips that cross OBS scene switches (c07: VS Code, black, then the game) don't suit a single fixed crop.
  - The cutout cam shows zoomed game behind the streamer.
  - When chat is active, a sliver of it can show at the right edge of the Japanese crop.

### Phase 5: VODs + metadata

`stream-cut-vods <session>`, thumbnail candidates, `seg<N>.youtube.md`, `series.yaml` handling.
Done when: both 2026-10-05 segment VODs and their metadata are ready to upload.

**Done 2026-10-05.**

- **`stream-cut-vods`:** stream copy, about 3 s for a 37 min segment and about 1 min for 1h34. It cuts exactly on the keyframe at or before the segment start (found with ffprobe) and records the real start in `vods/seg<N>.cut.json`, so chapters are relative to the file. On 2026-10-05 both cuts started 0.38 s early.
- **Series numbering:** read from `content/stream/series.yaml`. Titles say `#?` until a series is `numbering: locked`. `stream-number` (Phase 6) fills the episode numbers.
- **`stream-thumbs`:** 16 full-res candidate frames per segment at loudness peaks (reactions), spoken vocab and chapter starts, topped up evenly, at least 45 s apart. Writes `candidates_sheet.jpg`.
- **`stream-thumbnail <session> [-s N] [--force]`:** composes `vods/seg<N>_thumbs/pick1-3.jpg` (1280x720, under 2 MB) with no approval gate (the streamer, 2026-10-16). Background: the best game frame (colourful, sharp, no fades or letterbox bars) cropped by the layout's `thumb` box in `layouts.yaml` (whole frame if none); the layout is the one most of the segment's clips use, else `japanese_43` / `gaming` by type. Face: The streamer cut out of the cam box with rembg (CPU) at the loudest moment with a detected face, with a cream sticker outline. Text: the `thumb` block in `seg<N>.meta.json` (a Japanese word with reading and gloss, or a 2-4 word English hook), else the most-heard captured word or the hook's first words. Badge: game (or `thumb_label` in series.yaml, or the series name) and `EP n` once numbering is locked. `thumb.json` records the frames and text; unchanged inputs aren't rebuilt.
- **`stream-thumbnail-push <session> [--go] [-i segN]`:** sets pick1 on episodes already on YouTube (publish.yaml). Dry run by default; `seg<N>_thumbs/pushed.json` remembers what YouTube has (stream-upload writes it too).
- **`vods/seg<N>.youtube.md`:** title, description (about, links, chapters, and for Japanese segments the "Words from this episode" list: 107 words, about 2,000 chars), tags, playlist and thumbnail picks with reasons.
  - Before upload, fill in the Discord invite and playlist link.
  - Extra chapters from the transcript are listed in each file. They aren't written back to session.yaml.
  - YouTube needs at least 3 chapters, so segment 3 got 4 more.
- **Session status:** `packaged`.

### Phase 6: publish + archive

`stream-number` (series episode numbering), `stream-publish <session> [--dry-run]` (YouTube upload checklist, Twitch clips, Discord posts after the YouTube URLs are in, `publish.yaml`), `stream-archive <session>`.
Done when: a dry run prints exactly what would happen, the checklist makes a Studio upload quick, and Twitch + Discord work end to end on one clip.

**Done 2026-10-05.** The dry run and checklist work, and c07 went end to end (Twitch clip, then Discord post).

**VOD offset lesson:** the first c07 clip landed 5 s early. Twitch placed it exactly as asked, but the VOD's `created_at` is not its first frame: it said +13.0 s, while frame matching showed +7.8 s, constant across the stream. The fix is `stream-vod-offset <session>`, which reads the VOD's HLS playlist with yt-dlp (exact segment times from `#EXTINF`), matches VOD frames to recording frames at 10 fps, keeps only sharp matches, needs two samples to agree, and saves the result to `publish.yaml` with method `frames`. Twitch clips are blocked until it has run (or `--vod-offset` is passed). The re-created c07's first frame matched the recording at 1910 ± 0.5 s against a requested 1910.0 s. The first clip and its Discord post were deleted: Helix has no delete-clip endpoint, so the streamer deleted the clip in the dashboard, and the webhook deleted its own message.

- **`stream-auth twitch`:** one browser approval using the `wabisanji-chat-recorder` app (redirect `http://localhost:17563/callback`), scopes `channel:manage:clips` and `user:read:broadcast`. The token pair is kept in `twitch.token.json` at the repo root (gitignored) and refreshed automatically.
- **Secrets:** read from the repo-root `.env` (template: `.env.example`) by a tiny parser; values are never printed.
- **Clip From VOD:** checked against the API reference on 2026-10-05 (open beta):
  - `POST /helix/videos/clips` with `editor_id`, `broadcaster_id`, `vod_id`, `vod_offset` (integer: where the clip ENDS), `duration` (5 to 60 s, 0.1 s precision) and `title` (required, AutoMod-checked). Returns 202, so we poll Get Clips for the `url` and the real `vod_offset` (where it STARTS).
  - The clip start is sent exact and the end rounded up to a whole second.
  - VOD 0:00 comes from `stream-vod-offset` (frame matching; +7.8 s on 2026-10-05). The VOD's `created_at` is shown only as a rough guess, and `--vod-offset` overrides it.
  - The drift between requested and actual start is stored per clip and flagged over 2 s, since the beta was reported to land clips 2 to 9 s late. Fix drifted clips with their `edit_url`.
- **`stream-number`:** rebuilds each series' `episodes` from every approved session, in stream order, editing only those lists in `series.yaml`. It refuses to change a locked series' existing numbers without `--force`, and lists recordings not in any session yet (43 on 2026-10-05).
- **`stream-publish <session>`:** a dry run unless `--go`.
  - Writes `upload-checklist.md` with Shorts ready to upload; episodes are marked "don't upload yet" until their series is `numbering: locked`.
  - `--youtube <id>=<url>` records Studio uploads. Finding them by title with videos.list waits until YouTube API credentials exist.
  - Twitch clips are created from the VOD, then Discord posts go out last: one per episode once its YouTube URL is recorded (one per clip until 2026-10-14), posted as "Wabi Sanji" with mentions disabled.
  - Everything goes into `publish.yaml` and is skipped on reruns. The status becomes `published` when every target is done.
- **`stream-archive <session> [--dry-run] [--to DIR] [--force]`:** moves the raw recording to `<session>/raw/` (updating `recording:`) and removes regenerable folders: `frames`, `fine`, `at`, `audio`, `shorts/_preview` and thumbnail candidates.
- **`stream-signals`:** now also reads Twitch stream markers when logged in.

### Phase 7: backlog + status

`stream-inventory` (classify recordings in `<recordings root>`), `stream-status` board, and the `/stream-backlog` skill.
Done when: every backlog recording has a draft session.yaml or a recorded reason for skipping.

**Batch 1 done 2026-10-05: every Alchemy Factory stream.**
- The streamer confirmed `_backlog.yaml`.
- A coarse scan of all streams (one frame every 8 min) found Alchemy Factory in 16 of them, 5 of which were titled for other games. VOD titles are hints only: "Day 3 | Makin' Magic" on 2026-09-15_2 was Wanderburg.
- The 16 sessions were drafted by parallel agents from contact sheets, checked, and approved by the streamer with breaks cut (one episode per game per stream), the Oxygen Not Included trailer cut (2026-09-20), Gamedle/dailies skipped (2026-10-02_3), and the Pokémon setup as a Japanese segment (2026-10-03).
- **Numbering:** Alchemy Factory is numbered 1 to 16 (2026-09-10 to 2026-10-05) and locked. Breath of Fire IV was locked at #1. Six new series were added with open numbering.
- **Still to segment:** 20 streams (Graveyard Keeper II, World of Warcraft, Star Wars: Zero Company, Wanderburg, Warcraft III, the trap/tower-defense game, Minecraft Dungeons II, Half-Life, a pool roguelite, Duolingo, coding).

**Batch 2 done 2026-10-06: the other 20 streams.**
- **Drafting and approval:** 8 parallel agents drafted from 1/min contact sheets, refined to about 10 s, and the streamer approved them all in one Gate 1. Every backlog stream now has an approved session.
- **Changes the streamer made:**
  - 2026-09-24: two cuts so no password or Twitch secret can show (0:46:30-0:48:30, 1:04:40-1:07:50).
  - The Duolingo stretch on 2026-09-18 is skipped.
  - Co-op partners' voices stay in.
  - "World of Warcraft Forever" stays in the World of Warcraft series.
  - Standalone coding streams are the "Rough Edges" series. Its titles use `title_sep: ": "`: "Rough Edges #1: Building a Twitch Chat Tab for VS Code with Claude Code".
- **New series:** Star Wars: Zero Company, Ultra Pool, Orcs Must Die! Deathtrap, Half-Life, Rough Edges. Every series is now `numbering: locked`.
- **OBS restarts:** 2026-09-08/_2 and 2026-09-18/_2 stay separate episodes, since each has its own Twitch VOD.
- **Spotify:** Track 2 is music-free on the MKVs (see "Audio finding"), and every MKV session uses `audio_track: 1`. The streamer keeps the single-track MP4s as they are and checks Content ID in Studio before publishing. 2026-10-05 did have Spotify on Track 1, so its uploaded Shorts and BoF4 episode carry it. Its seg3 was re-cut from Track 2.
- **Transcription:** all 20 sessions in 72 min on large-v3-turbo (about 84k words). Quiet sessions ran at about 100x realtime, talk-heavy ones at about 10x.

**Batch 2 Shorts, 2026-10-07.** 162 Shorts from 33 sessions in 26 min, one session at a time, with copy for each.
- **Gate 2:** 170 clips approved. 10 clips from the two co-op-with-kid sessions were dropped, along with both episodes. A "kids stay out" rule is now in `docs/content-rules.md`.
- **Facecam:** the backlog scenes put the cam in different places, so the gaming layout follows the face per clip (`stream/facecam.py`, cached in `shorts/_captions/<id>.cam.json`). A clip with no detection uses its session's median position. The 2026-09-21_2 WoW clips were set by hand, because that scene has the cam bottom left.
- **Anki layouts:** `anki_left` and `anki_right` cover the 2026-10-03 Anki review scene.
- **Captions:** Alchemy Factory names items after the alchemy planet metals (Saturn = lead, Mars = iron, Jupiter = tin). Whisper writes "satin" for Saturn, so fix it to Saturn, never the other way round.

Inventory built 2026-10-05:

- **`stream-inventory`:** lists the `<recordings root>` root plus `WabiSanji\` only, read-only, and writes `sessions/_backlog.yaml` (45 recordings on 2026-10-05).
  - Each recording is matched to Twitch past broadcasts (Helix Get Videos). OBS files match by time overlap and claim their VOD; files without an OBS name match by length (±2 s) against VODs not already claimed.
  - Class is `stream` with a VOD, `false_start` under 0.2 GB or 5 min (two of these were 1 to 3 min streams), and `local_test` with no VOD.
  - Also flags possible split streams (the same VOD title within 30 min), counts vocab captures per recording, and suggests session ids. Confirmed entries survive reruns.
- **Result:** 36 streams, 7 false starts, 1 excluded test and today's session. There are no local-only recordings.
- **The 4 `WabiSanji\` files are downloaded VODs** (ffmpeg remux, length equal to a VOD to the second), now dated 8 to 11 September.
- **Twitch still had every past broadcast back to 2026-09-08** (27 days old on 2026-10-05), so the backlog can get Twitch clips too. Retention beyond that is unknown, so clip backlog moments soon.
- **`stream-status`:** the board of sessions, showing what each is waiting on and the next command, plus a backlog summary line.

## Don't

- Don't delete or move anything outside `<sessions root>\` and the recordings being processed. Never touch the unrelated folders in `<recordings root>`.
- Don't delete raw recordings. `/stream-archive` moves; deleting is the streamer's call.
- Don't publish anything public, and don't publish without approved gates.
- Don't run GPU-heavy steps while OBS is live (the streamer may be streaming). Ask first if OBS is running.
- Don't write to `wabisanji.db`.
- Don't print secrets.
