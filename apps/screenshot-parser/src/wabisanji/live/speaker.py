"""Make ARIA talk through Speaker.bot's websocket server (127.0.0.1:7580).

Speaker.bot owns the voices and the output device (Sonar - Chat, which OBS
picks up as the TTS source), so ARIA's voice is just another alias there:
`ARIA` = ElevenLabs Jessica. See docs/aria-setup.md.
"""

from __future__ import annotations

import json
import time
import uuid

from .store import LiveStore

CHARS_PER_SECOND = 14  # roughly how fast Jessica talks; used to mute the listener meanwhile
LEAD_IN_S = 2.0  # ElevenLabs round-trip before audio starts


class SpeakerError(RuntimeError):
    pass


def speak(text: str, url: str, voice: str, store: LiveStore | None = None) -> None:
    """Queue `text` in Speaker.bot. Returns once Speaker.bot accepts it, not when it's spoken."""
    if not text.strip():
        return
    try:
        from websockets.sync.client import connect
    except ImportError as e:
        raise SpeakerError("websockets is missing: uv sync --extra listen") from e

    request = {
        "request": "Speak",
        "id": str(uuid.uuid4()),
        "voice": voice,
        "message": text,
        "badWordFilter": False,
    }
    try:
        with connect(url, open_timeout=3, close_timeout=1) as ws:
            ws.send(json.dumps(request))
            reply = _reply_for(ws, request["id"])
    except (OSError, TimeoutError) as e:
        raise SpeakerError(f"Speaker.bot not reachable at {url}: {e}") from e
    if reply.get("status") not in (None, "ok"):
        raise SpeakerError(f"Speaker.bot refused: {reply}")

    if store is not None:
        # Mute the listener while ARIA talks, in case the voice bleeds into the mic
        until = max(store.state()["speaking_until"], time.time()) + LEAD_IN_S
        store.set_state(speaking_until=until + len(text) / CHARS_PER_SECOND)


def _reply_for(ws, request_id: str, timeout: float = 3.0) -> dict:
    """Speaker.bot may send a Hello first; wait for the reply that echoes our id."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
        except TimeoutError:
            break
        if msg.get("id") == request_id:
            return msg
    return {}
