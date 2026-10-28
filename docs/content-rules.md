# Content rules

What keeps stream episodes, Shorts and clips publishable. Claude follows these when segmenting and writing episode details, and flags anything that breaks them in each episode's "Check before publishing" note.

## While streaming

- **Other people's audio doesn't go in episodes.** Game trailers, another streamer's broadcast or a YouTube video playing on stream all risk Content ID claims, or YouTube's reused-content rules. Mute it when you can. Otherwise it gets cut out of the episode.
- **If you follow someone's guide or build, say whose it is on stream.** The episode then credits them by name and links the original in the description.
- **Say what you're switching to** ("okay, let's jump into Alchemy Factory"). It makes the segment cuts and chapters cleaner.
- **Use the marker hotkey or chat command on good moments** (once it's set up). Markers feed straight into clip picking.
- **Memes use the original audio** (the streamer 2026-10-20), the way Real Civil Engineer's edits do: TV, film and viral clips, and well-known sound effects. That's other people's audio, so expect the occasional Content ID claim on an episode. A claim takes or shares that video's money; it isn't a strike.
  - **Risk levels:** every meme in `content/stream/memes.yaml` has a `risk`. Low is library sound effects, med is TV, viral and game clips, high is film studios.
  - **No music memes:** Roundabout ("To Be Continued"), coffin dance, Rickroll and Yakety Sax stay out of the library. Music is what gets reliably claimed, and it earns Twitch DMCA strikes on VODs.
  - **Episodes keep the memes:** both ARIA's live ones (they're in the recording) and the ones `/stream-memes` adds. The "Check before publishing" note lists every meme with its time and risk, so a claim in Studio's Checks step can be traced straight to one. Trim, mute, or accept the claim.
  - **Shorts:** they don't get added memes. A Short over a minute with any claim gets blocked, so check a Short's Checks step if a live meme is in it.
  - **Family:** memes marked `family: false` (that's what she said, noice) never go into an episode with the kids in it, and ARIA's "family mode" keeps them quiet live.
- Hints ARIA reads out credit their source on the card.
- **Keep the recording running until the stream ends.** If it stops early, the rest has to be rebuilt from the Twitch VOD.

## Episodes

- **One episode per game per stream.** Breaks, BRBs and other games in between are cut out of the episode file.
- **Skipped, never published:** Starting Soon, BRB and ending screens, daily guessing games, and anything under about 10 minutes (flagged instead).
- **IRL walks are the "Wabi Sanji Outside" series** (the streamer 2026-10-09), whatever "go" game is on the phone. Titles: "Wabi Sanji Outside #N: <hook>". IRL BRB cards are cut. A walk where the camera or audio failed is skipped.

- **Family stays private.** Family members who appear on stream go by a stream name, never their real name. Any moment where a real name is said gets cut or muted. A passing mention with no name is fine. When segmenting, search the transcript for the real names and the stream names (Whisper misspells them), and list every hit in the episode's "Check before publishing" note.
- **Numbers are by stream date** within each series, starting from the earliest stream. A series is only `numbering: locked` once every earlier stream of that game is segmented. Until then titles say `#?` and the episode doesn't go up.
- **Uploads stay manual in YouTube Studio:** upload as Private, then set public or scheduled.
- **Before uploading, read the episode's "Check before publishing" note.** It lists:
  - swearing hotspots;
  - personal things said on stream (work, family, health, where you live);
  - viewer names;
  - other people's audio.

  Decide whether to trim, mute or leave each one.

## Shorts and clips

- Shorts are 20 to 60 seconds, with a hook in the first two seconds, and must make sense without having watched the stream.
- Japanese Shorts favour a moment a viewer can learn from: the line, the word, the "oh!".
- A clip crossing an OBS scene switch (code to game, black frames) doesn't work as a Short. Keep it for Twitch and Discord only.
- Twitch clips are made from the VOD, so they need the VOD to still exist. Twitch only promises 14 days for Affiliates.
- Discord posts link the YouTube Short and the Twitch clip. Video files are never uploaded to Discord.

## Writing (titles, descriptions, Shorts copy)

- In your voice: first person, friendly and plain, Australian English.
- No em dashes, no "It's not X, it's Y" lines and no hype words ("epic", "insane").
- Only what actually happens in the transcript. Nothing invented.
- Refer to viewers as "chat" or "a viewer", unless you visited their world or they're central to the episode.
