# Stream setup and troubleshooting

The parts of the streaming rig the post-stream pipeline depends on. If you change one of these, the note says what to update.

## OBS

| Setting | Value | Why it matters |
|---|---|---|
| Scene collection | `WabiSanji_2` | |
| Recording path | `<recordings root>` | The pipeline and `stream-inventory` look for recordings here. |
| Recording file name | OBS default `%CCYY-%MM-%DD %hh-%mm-%ss` (e.g. `2026-10-05 10-37-11.mkv`) | The recording's start time comes from the file name. Rename a file and `stream-init` needs `--start`. |
| Format | MKV | Episode files are cut from MKV directly with stream copy. |
| Recording resolution | 2560x1440 (2K) from 2026-10-18_2; 1920x1080 before | Episodes stay at the recording's size. Shorts crops, blur boxes and face-cam crops are in 1920x1080 reference pixels and scaled to the recording, so either size works. A new size is fine too as long as it's 16:9. |
| Record while streaming | Start recording when the stream starts, and don't stop it early | If the recording stops before the stream ends, the rest can only come back from the Twitch VOD (`stream-vod-tail`), and only while the VOD exists. |
| Audio tracks (now) | Track 1 = full mix, Track 2 = same full mix, Track 3 silent | Sessions use `audio_track: 0` (the mix). |
| Audio tracks (planned) | Track 1 mix, Track 2 mic, Track 3 game/desktop, Track 4 music | Once this is set up, new sessions should use `audio_track: 1` (mic only), which gives cleaner transcripts and lets Shorts drop the music. |
| Vocab overlay | Browser source `Vocab Overlay` → `http://localhost:8770/overlay` (needs `wabisanji serve` running) | AnkiConnect owns port 8765, so don't use it for the overlay. |
| Screenshot hotkey (PC games) | OBS screenshot hotkey, saved to `<recordings root>` as `Screenshot YYYY-MM-DD hh-mm-ss.png` | Word cards in Shorts and the "Words from this episode" list are timed from these file names. |
| RetroArch screenshots | F8, saved to RetroArch's `screenshots` folder | The same timing comes from the RetroArch file names. |

### Scene layouts (Shorts crops)

`tools/stream-pipeline/src/wabisanji_post/layouts.yaml` (or the file `WS_LAYOUTS` points at) crops each Short from fixed positions in the scene, given in 1920x1080 reference pixels (a 2K recording is scaled 4/3 at render time):

- **Japanese scene:** game capture 1600x900 at (228, 89), the 340 px vocab column on the left, facecam bottom right, chat on the right while active.
- **Gaming scene:** game full screen, chat bottom left, facecam bottom right.

If you move or resize the game capture, facecam, chat or vocab column, the crops need re-measuring. Tell Claude, and test with `stream-render-shorts <session> --still`.

Viewer TTS (rewards, cheers, alerts) is set up in [tts-setup.md](tts-setup.md). It plays on Sonar - Chat, which the OBS "TTS" source captures.

### Still to do

- Audio routing to 4 tracks (above).
- A stream-marker hotkey or chat command, so good moments get flagged live. `stream-signals` already reads Twitch markers.
- Optional: log Twitch category changes (EventSub `channel.update`) on an always-on machine next to the chat recorder, so segmenting can use them instead of frames.



## Instagram (Reels from Shorts)

`stream-social` posts approved Shorts as Reels through Meta's Graph API ("Instagram API with Facebook Login"). It's free, and the mp4 uploads straight from disk. One-time setup:

1. **Make the Instagram account professional.** In the Instagram app: Settings → Account type and tools → Switch to professional account → **Creator**. It's free and you can switch back.
2. **Create a Facebook Page** called Wabi Sanji from your Facebook account, then link the Instagram account to it: Page settings → Linked accounts → Instagram → Connect.
3. **The Meta app** is "Wabi Sanji Pipeline" (created 2026-10-14, no business portfolio; the App ID is `META_APP_ID` in `.env`). It has the use case "Manage messaging & content on Instagram", with the content permissions added under Customize → **API setup with Facebook login**: `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`, `business_management` and `pages_show_list`.
   - Leave the app in **Development mode** (Unpublished). As the app's admin you can post to your own account without App Review.
   - It uses Facebook Login for Business with "Enforce HTTPS" locked on, so an `http://localhost` redirect can't be saved. Log in with a Graph API Explorer token instead (step 5).
4. Copy the App ID and App Secret from App settings → Basic into `.env` as `META_APP_ID` and `META_APP_SECRET`. The "Instagram app ID/secret" shown on the use-case page is a different pair, for Instagram Login, so don't use it.
5. **Log in.** Open Graph API Explorer (developers.facebook.com/tools/explorer) and pick Meta App "Wabi Sanji Pipeline". Under "User or Page", choose Get User Access Token, add the five permissions above, and click Generate Access Token. In the Facebook dialog, choose the Wabi Sanji Page and Instagram account, then copy the token.
   - In your own terminal (so the token stays out of chat), from `tools\stream-pipeline`, run `uv run python pipeline.py stream-auth instagram --token <token>`. It swaps the token for a Page token that never expires.
   - Then `stream-auth instagram --check` should come back all green.

Each session's captions live in `social.yaml`, drafted by `/stream-social`. Only entries you set to `approved: true` post, and **Reels are public as soon as they post**.

## Troubleshooting

| Problem | Fix |
|---|---|
| `stream-social` stops with a login error (code 190), or `--check` says the data access window is ending | Get a fresh Graph API Explorer token and run `stream-auth instagram --token <token>` again (setup step 5). This also happens if you change your Facebook password or remove the app. |
| `stream-auth instagram` says several Pages have Instagram linked | Set `INSTAGRAM_PAGE=Wabi Sanji` (or the Page id) in `.env`. |
| A Reel fails with "couldn't process the video" | The error text includes Meta's reason. Reels need H.264/AAC MP4, 3 s to 15 min, 23 to 60 fps, under 300 MB. `stream-render-shorts` output already fits. |
| RetroArch has no sound, or runs too fast (at the monitor's 140 Hz) | Its audio driver must be **XAudio2**, not WASAPI: WASAPI can't output to SteelSeries Sonar Gaming (8 channels, 96 kHz, 24-bit). Run `retroarch.exe --verbose --log-file=<path>` to see audio errors. |
| Stream stutters or drops frames | Is Ollama running? Keep it off while streaming: it takes the GPU from OBS. `/translate` works without it. |
| Claude asks before GPU work | Transcription and Shorts rendering use the GPU. Run them when you're not live. |
| Twitch clips fail with 401 or "not logged in" | `uv run python pipeline.py stream-auth twitch` from `tools\stream-pipeline` (browser login). If you made a new secret on the Twitch app, update `.env` first. |
| `stream-publish` says the VOD offset isn't measured | `stream-vod-offset <session>` (about 3 min). |
| Twitch clips land a few seconds off | Check one clip's first frame. The offset is measured from frames, and clips made since 2026-10-05 landed within 0.5 s. |
| Captions appear twice when you watch a Short | Your player loaded a same-name `.ass` file. They live in `shorts\_captions\` now, so this only happens if one gets copied next to the `.mp4`. |
| A `session.yaml` won't load | `stream-validate <session>` shows the exact problem. |
