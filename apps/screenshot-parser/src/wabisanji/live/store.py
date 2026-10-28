"""Files shared by `wabisanji listen`, `wabisanji live ...` and Claude.

Everything is append-only JSON lines under data/live/, so separate processes
can write without coordinating and post-stream tools can line events up
against the recording by wall-clock time.

- transcript.jsonl: one line per utterance, {seq, t, text}
- events.jsonl: questions, answers, memes, expands, clears
- state.json: small flags (memes on/off, when ARIA stops speaking)
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from ..config import APP_ROOT

LIVE_DIR = APP_ROOT / "data" / "live"


class LiveStore:
    def __init__(self, root: str | Path = LIVE_DIR):
        self.root = Path(root)
        self.transcript_path = self.root / "transcript.jsonl"
        self.events_path = self.root / "events.jsonl"
        self.state_path = self.root / "state.json"

    # --- transcript -------------------------------------------------------

    def transcript(self, since: int = 0) -> list[dict]:
        return [r for r in _read_jsonl(self.transcript_path) if r["seq"] > since]

    def last_seq(self) -> int:
        rows = _read_jsonl(self.transcript_path)
        return rows[-1]["seq"] if rows else 0

    def add_utterance(self, text: str, seq: int | None = None) -> dict:
        row = {"seq": seq if seq is not None else self.last_seq() + 1, "t": now_iso(), "text": text}
        _append(self.transcript_path, row)
        return row

    # --- events -----------------------------------------------------------

    def events(self, kind: str | None = None) -> list[dict]:
        rows = _read_jsonl(self.events_path)
        return [r for r in rows if kind is None or r.get("type") == kind]

    def add_event(self, kind: str, **data) -> dict:
        row = {"type": kind, "t": now_iso(), **data}
        _append(self.events_path, row)
        return row

    def question(self, qid: str) -> dict | None:
        return next((q for q in reversed(self.events("question")) if q["id"] == qid), None)

    def unanswered(self, max_age_s: float = 600) -> list[dict]:
        """Questions with no answer yet, newest last, ignoring stale ones."""
        rows = self.events()
        done = {r["id"] for r in rows if r.get("type") in ("answer", "clear") and r.get("id")}
        cutoff = time.time() - max_age_s
        return [
            r for r in rows
            if r.get("type") == "question" and r["id"] not in done and _epoch(r["t"]) >= cutoff
        ]

    def seconds_since_meme(self) -> float | None:
        memes = self.events("meme")
        return time.time() - _epoch(memes[-1]["t"]) if memes else None

    # --- flags ------------------------------------------------------------

    def state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        return {"memes": True, "family": False, "speaking_until": 0.0, **data}

    def set_state(self, **changes) -> dict:
        data = {**self.state(), **changes}
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.state_path)
        return data

    def memes_enabled(self) -> bool:
        return bool(self.state()["memes"])

    def family_mode(self) -> bool:
        """On while kids are on stream: memes marked family: false stay quiet."""
        return bool(self.state()["family"])

    def is_speaking(self) -> bool:
        return time.time() < self.state()["speaking_until"]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a half-written line from another process; it'll be whole next read
    return rows
