"""In-process shared state for stream mode.

Cross-process data (vocabulary, screenshots) lives in SQLite. Flags and status
that only this process knows live here, behind a lock, because the capture,
OCR and hotkey threads added in later phases update them while request
handlers read them.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass


@dataclass
class Status:
    overlay_hidden: bool = False
    obs_connected: bool = False
    capture_enabled: bool = False
    live_ocr: str = "off"  # what's actually running: off | manga-ocr


class StreamState:
    def __init__(self, source: str | None = None):
        # Game passed via --source; None means follow the session file
        self.source = source
        self._status = Status()
        self._lock = threading.Lock()

    def status(self) -> dict:
        with self._lock:
            return asdict(self._status)

    def update(self, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                if not hasattr(self._status, key):
                    raise AttributeError(f"unknown status field: {key}")
                setattr(self._status, key, value)

    def toggle_overlay(self) -> bool:
        with self._lock:
            self._status.overlay_hidden = not self._status.overlay_hidden
            return self._status.overlay_hidden
