"""Live viewer log (spec: "Metrics"): viewers per game, measured while Wabi Sanji is live.

Twitch's API has no history of viewer counts; the dashboard only gives daily totals, which
mix every game played that day. So ``stream-viewers`` polls Get Streams once a minute while
the channel is live and appends ``{utc, viewers, game, title, started_at}`` to
``sessions/_viewers/<local date>_<HHMM>.jsonl``. Afterwards :func:`segment_viewers` lays the
samples over each session's segments (recording offset = sample time minus
``recording_start``), giving average and peak viewers per segment and game.

It is one HTTP call a minute and no GPU, so it can run all day (``--forever``) on the
streaming PC or any machine logged in to Twitch.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from .models import Session

VIEWERS_DIR = "_viewers"


def _utc(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def log_path(root: Path, started_at: dt.datetime) -> Path:
    local = started_at.astimezone()
    return root / VIEWERS_DIR / f"{local:%Y-%m-%d_%H%M}.jsonl"


def record(root: Path, helix, *, poll: float = 60.0, wait: bool = True, forever: bool = False,
           offline_grace: float = 600.0, max_hours: float = 24.0,
           sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
           now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
           say: Callable[[str], None] = print) -> list[Path]:
    """Log samples while live. Without ``forever``, stop ``offline_grace`` seconds after the stream
    ends (or at once if offline and not ``wait``). Returns the log files written."""
    from .twitch import TwitchError

    written: list[Path] = []
    last_live: Optional[float] = None
    start = clock()
    while forever or clock() - start < max_hours * 3600:
        try:
            s = helix.stream()
        except (TwitchError, OSError) as exc:  # a network blip: try again next poll
            say(f"Get Streams failed, retrying: {exc}")
            s = None
        if s:
            path = log_path(root, _utc(s["started_at"]))
            if path not in written:
                path.parent.mkdir(parents=True, exist_ok=True)
                written.append(path)
                say(f"live: {s.get('game_name', '')}, logging viewers to {path}")
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"utc": now().replace(microsecond=0).isoformat(), "viewers": s.get("viewer_count"),
                                    "game": s.get("game_name", ""), "title": s.get("title", ""),
                                    "started_at": s["started_at"]}, ensure_ascii=False) + "\n")
            last_live = clock()
        elif last_live is not None and clock() - last_live >= offline_grace:
            say("stream ended")
            if not forever:
                return written
            last_live = None
        elif last_live is None and not wait and not written:
            say("not live")
            return written
        sleep(poll)
    return written


def load_samples(root: Path) -> list[dict]:
    """Every logged sample, oldest first, with ``utc`` as a datetime. The garage recorder's
    pulls (``recorder_*.jsonl``) and this machine's own logs can overlap: one sample a minute."""
    by_minute: dict[dt.datetime, dict] = {}
    for path in sorted((root / VIEWERS_DIR).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                d["utc"] = _utc(d["utc"])
                by_minute.setdefault(d["utc"].replace(second=0, microsecond=0), d)
    return sorted(by_minute.values(), key=lambda d: d["utc"])


def segment_viewers(sessions: Iterable[Session], samples: list[dict]) -> list[dict]:
    """``[{session, segment, game, series, type, samples, avg, peak}]`` for every non-skip segment
    that has samples. A sample belongs to the segment whose time range holds its recording offset."""
    rows: list[dict] = []
    for s in sessions:
        t0 = s.recording_start.astimezone(dt.timezone.utc)
        offsets = [((d["utc"] - t0).total_seconds(), d["viewers"]) for d in samples if d.get("viewers") is not None]
        for seg in s.segments:
            if seg.type == "skip":
                continue
            vals = [v for t, v in offsets if seg.start <= t < seg.end]
            if vals:
                rows.append({"session": s.session, "segment": seg.id, "game": seg.game or seg.label,
                             "series": seg.series, "type": seg.type, "samples": len(vals),
                             "avg": sum(vals) / len(vals), "peak": max(vals)})
    return rows


__all__ = ["record", "load_samples", "segment_viewers", "log_path", "VIEWERS_DIR"]
