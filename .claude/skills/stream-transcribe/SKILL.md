---
name: stream-transcribe
description: Transcribe the approved segments of a stream session on the GPU with faster-whisper, producing word-level JSON and readable transcripts per segment.
argument-hint: "<session id> [segment N] [model]"
---

# /stream-transcribe

Spec section: "Transcription" in `docs/post-stream-spec.md`. Args: **$ARGUMENTS**

1. Make sure `session.yaml` has `status: segments_approved` or later. Don't transcribe a draft.
2. Ask before starting if OBS might be live (GPU contention).
3. Run `uv run --directory tools/stream-pipeline python pipeline.py stream-transcribe <session> [--segment N] [--model M]`. It uses `audio_track` from session.yaml: 0 for the backlog (full mix), 1 after the OBS routing fix.
4. Spot-check each `transcript/seg<N>.txt`: open it, read a few minutes near the start and middle. Watch for hallucinated repeats over music, which usually mean VAD settings need tuning. For Japanese segments, check that the Japanese the streamer reads aloud is kept: with the default `language="en"` it appears as romaji ("Ashita, that's tomorrow"), which is expected (see "Chosen settings" in the spec).
5. Set `status: transcribed` once all non-skip segments are done. Report the time taken per segment and the model.
