"""Japanese Word of the Day for the Twitch channel-point reward.

Streamer.bot runs `wabisanji wotd` when the reward is redeemed, then reads
data/wotd.txt (line 1 word, line 2 reading, line 3 meaning) to post the word
in chat and have Speaker.bot's JP voice say it. Words come from what was
recently captured on stream, skipping any picked in the last few days.
"""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .config import APP_ROOT
from .db import recent_words

OUT_PATH = APP_ROOT / "data" / "wotd.txt"
HISTORY_PATH = APP_ROOT / "data" / "wotd_history.json"

POOL = 60          # newest words considered
SHORTLIST = 15     # random pick among the newest fresh ones
AVOID_DAYS = 14    # don't repeat a word picked within this many days


def pick(
    conn: sqlite3.Connection,
    history: dict[str, str],
    today: date,
    rng: random.Random | None = None,
) -> dict[str, Any] | None:
    """A recent speech word with a reading and meaning, not picked lately.

    Falls back to recently picked words if every candidate was used.
    Returns None when the capture log has no usable words.
    """
    rng = rng or random.Random()
    cutoff = (today - timedelta(days=AVOID_DAYS)).isoformat()
    usable = [
        w for w in recent_words(conn, limit=POOL)
        if w["reading"] and w["meaning"] and w["category"] in (None, "speech")
    ]
    fresh = [w for w in usable if history.get(w["word"], "") < cutoff]
    candidates = (fresh or usable)[:SHORTLIST]
    return rng.choice(candidates) if candidates else None


def load_history(path: Path = HISTORY_PATH) -> dict[str, str]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save(word: str, reading: str, meaning: str, today: date,
         out: Path = OUT_PATH, history_path: Path = HISTORY_PATH) -> None:
    """Write the 3-line file Streamer.bot reads and record the pick."""
    clean = lambda s: " ".join((s or "").split())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"{clean(word)}\n{clean(reading) or clean(word)}\n{clean(meaning)}\n",
                   encoding="utf-8")
    history = load_history(history_path)
    history[word] = today.isoformat()
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
