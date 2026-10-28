"""Pull chat and live viewer counts from an optional chat recorder.

The recorder is a small always-on service that keeps a permanent chat archive and a
once-a-minute viewer log, one file per UTC day each, and serves them over HTTP behind a
bearer token. The pipeline only reads:

- ``GET /v1/archive/<channel>?date=YYYY-MM-DD`` → ``{lines: [{t, line}]}`` (raw IRC lines)
- ``GET /v1/viewers/<channel>?date=YYYY-MM-DD`` → ``{samples: [{utc, viewers, game, title, started_at}]}``

``.env``: ``RECORDER_URL``, ``RECORDER_TOKEN`` (the recorder's ``HISTORY_TOKEN``) and optional
``RECORDER_CHANNEL`` (default ``WS_TWITCH_LOGIN``). Without a recorder, chat signals are skipped.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Iterable

import requests

from . import secrets, settings



class RecorderError(RuntimeError):
    pass


def _config() -> tuple[str, str, str]:
    token = secrets.get("RECORDER_TOKEN")
    if not token:
        raise RecorderError("RECORDER_TOKEN isn't in .env (it's the recorder's HISTORY_TOKEN)")
    url = secrets.get("RECORDER_URL").rstrip("/")
    if not url:
        raise RecorderError("RECORDER_URL isn't in .env (the chat recorder is optional)")
    return url, token, secrets.get("RECORDER_CHANNEL") or settings.twitch_login()


def _get(route: str, date: str) -> dict:
    url, token, channel = _config()
    try:
        r = requests.get(f"{url}/v1/{route}/{channel}", params={"date": date},
                         headers={"Authorization": f"Bearer {token}"}, timeout=20)
    except requests.RequestException as exc:
        raise RecorderError(f"recorder at {url} unreachable: {exc.__class__.__name__}") from exc
    if r.status_code == 401:
        raise RecorderError("recorder rejected RECORDER_TOKEN (it must match its HISTORY_TOKEN)")
    if r.status_code == 404 and route in ("archive", "viewers") and "not found" in r.text:
        raise RecorderError(f"recorder at {url} has no /v1/{route}: redeploy it with the archive update")
    if not r.ok:
        raise RecorderError(f"recorder /v1/{route} failed ({r.status_code}): {r.text[:200]}")
    return r.json()


def utc_dates(start: dt.datetime, end: dt.datetime) -> list[str]:
    """Every UTC date from ``start`` to ``end`` inclusive."""
    a = start.astimezone(dt.timezone.utc).date()
    b = end.astimezone(dt.timezone.utc).date()
    return [(a + dt.timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def chat_lines(start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Archived chat between ``start`` and ``end``: ``[{t: datetime, line: raw IRC}]``, oldest first."""
    out = []
    for date in utc_dates(start, end):
        for row in _get("archive", date).get("lines", []):
            t = dt.datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
            if start <= t <= end:
                out.append({"t": t, "line": row["line"]})
    return out


def pull_viewers(viewers_dir: Path, dates: Iterable[str]) -> int:
    """Save the recorder's viewer samples as ``recorder_<date>.jsonl`` next to stream-viewers'
    own logs (same fields), rewriting each day in full. Returns the number of samples."""
    viewers_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for date in sorted(set(dates)):
        samples = _get("viewers", date).get("samples", [])
        if not samples:
            continue
        (viewers_dir / f"recorder_{date}.jsonl").write_text(
            "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in samples), encoding="utf-8")
        n += len(samples)
    return n


__all__ = ["RecorderError", "chat_lines", "pull_viewers", "utc_dates"]
