"""In-process state for ARIA's two widgets.

Memes are one-shot events: the meme layer polls `since=<seq>` and plays each
new one once. The answer card is a single snapshot with a version number, so
an OBS browser source that reloads mid-answer still shows the current card.
"""

from __future__ import annotations

import threading
import time
from collections import deque

CARD_STATUSES = ("listening", "thinking", "hint", "expanded")


class LiveState:
    def __init__(self, keep: int = 50):
        self._lock = threading.Lock()
        self._memes: deque[dict] = deque(maxlen=keep)
        self._seq = 0
        self._card: dict | None = None
        self._version = 0

    # --- memes ------------------------------------------------------------

    def push_meme(self, **data) -> dict:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "at": time.time(), **data}
            self._memes.append(event)
            return event

    def memes_since(self, seq: int | None) -> dict:
        """seq=None (first load) returns no backlog: old memes shouldn't replay."""
        with self._lock:
            events = [] if seq is None else [e for e in self._memes if e["seq"] > seq]
            return {"seq": self._seq, "memes": events}

    # --- answer card ------------------------------------------------------

    def card(self) -> dict:
        with self._lock:
            return {"version": self._version, "card": dict(self._card) if self._card else None}

    def set_card(self, status: str, **fields) -> dict:
        """Start a new card, or update the current one if `id` matches it."""
        if status not in CARD_STATUSES:
            raise ValueError(f"unknown card status: {status}")
        with self._lock:
            same = self._card and fields.get("id") in (None, self._card.get("id"))
            base = self._card if same else {}
            self._card = {**base, **fields, "status": status, "updated": time.time()}
            self._version += 1
            return dict(self._card)

    def expand(self) -> dict | None:
        """Show the steps of the current answer. None if there's nothing to expand."""
        with self._lock:
            if not self._card or not self._card.get("steps"):
                return None
            self._card = {**self._card, "status": "expanded", "updated": time.time()}
            self._version += 1
            return dict(self._card)

    def clear(self) -> None:
        with self._lock:
            self._card = None
            self._version += 1
