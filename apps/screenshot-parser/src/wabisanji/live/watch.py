"""`wabisanji live watch`: the event stream `/aria watch` runs under Monitor.

Every stdout line wakes Claude, so this stays quiet unless there's work:
- QUESTION <id>               as soon as the streamer asks ARIA something
- TRANSCRIPT <n> since=<seq>  new speech for meme picks, at most every
                              transcript_batch_s, and only when memes are on
                              and the library has any
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from .memes import load_index
from .store import LiveStore


def watch(
    store: LiveStore,
    index_path: Path,
    batch_s: float = 45,
    poll_s: float = 1.0,
    out: Callable[[str], None] = lambda line: print(line, flush=True),
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    rounds: int | None = None,  # tests stop after this many polls
) -> None:
    told: set[str] = set()
    handed = store.last_seq()  # transcript Claude has been told about
    last_batch = clock()
    out(f"WATCHING (transcript at {handed}, {len(store.unanswered())} question(s) open)")

    while rounds is None or rounds > 0:
        for q in store.unanswered():
            if q["id"] not in told:
                told.add(q["id"])
                out(f"QUESTION {q['id']}")

        if clock() - last_batch >= batch_s:
            last_batch = clock()
            rows = store.transcript(since=handed)
            if rows:
                if store.memes_enabled() and load_index(index_path):
                    out(f"TRANSCRIPT {len(rows)} since={handed}")
                handed = rows[-1]["seq"]  # memes off: drop it rather than pile up a backlog

        if rounds is not None:
            rounds -= 1
        sleep(poll_s)
