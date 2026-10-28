"""Paths and per-channel preferences, from the environment or the repo-root ``.env``.

Each setting is resolved environment variable → ``.env`` → default. The defaults are
portable (no drive letters, no personal names); put your own values in ``.env``
(see ``.env.example``).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, tzinfo
from functools import lru_cache
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def repo_root() -> Path:
    """The checkout this package lives in: ``WABISANJI_ROOT``, else the first parent with
    ``content/stream``, else the current folder."""
    if os.environ.get("WABISANJI_ROOT"):
        return Path(os.environ["WABISANJI_ROOT"])
    for parent in PACKAGE_DIR.parents:
        if (parent / "content" / "stream").is_dir():
            return parent
    return Path.cwd()


def _get(key: str, default: str = "") -> str:
    from .secrets import get  # secrets imports repo_root from here

    return get(key, default)


def _path(key: str, default: Path) -> Path:
    value = _get(key)
    return Path(value).expanduser() if value else default


def recordings_root() -> Path:
    """Where OBS saves recordings."""
    return _path("WS_RECORDINGS_ROOT", Path.home() / "Videos")


def sessions_root() -> Path:
    """Per-stream working folders (session.yaml, transcripts, renders)."""
    return _path("WS_SESSIONS_ROOT", recordings_root() / "WabiSanji" / "sessions")


def content_root() -> Path:
    """``content/`` with ``stream/series.yaml`` and ``stream/memes.yaml``."""
    return _path("WS_CONTENT_ROOT", repo_root() / "content")


def parser_dir() -> Path:
    """The screenshot parser app (its config.yaml, capture log and live events)."""
    return _path("WS_PARSER_DIR", repo_root() / "apps" / "screenshot-parser")


def memes_dir_override() -> str:
    """``WABISANJI_MEMES`` if set; otherwise the parser config's ``stream.live.memes_dir`` is used."""
    return _get("WABISANJI_MEMES")


def layouts_path() -> Path:
    """Crop layouts for Shorts and thumbnails (one per OBS scene setup)."""
    return _path("WS_LAYOUTS", PACKAGE_DIR / "layouts.yaml")


def fonts_dir() -> Path:
    return PACKAGE_DIR / "fonts"


def local_tz() -> tzinfo:
    """``WS_TIMEZONE`` (IANA name, e.g. ``Australia/Melbourne``), else the system's zone."""
    name = _get("WS_TIMEZONE")
    if name:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo


def family_pattern() -> re.Pattern:
    """Words in a transcript that mean family is on stream, so not-family memes stay out."""
    return re.compile(_get("WS_FAMILY_PATTERN") or r"\b(kiddo|kids)\b", re.IGNORECASE)


def streamer_name() -> str:
    """How messages and prompts refer to the person streaming."""
    return _get("WS_STREAMER_NAME") or "the streamer"


def twitch_login() -> str:
    return _get("WS_TWITCH_LOGIN") or "wabisanji"


def channel_name() -> str:
    return _get("WS_CHANNEL_NAME") or "Wabi Sanji"


def discord_url() -> str:
    return _get("WS_DISCORD_URL") or "https://discord.gg/7E9mJQ795K"


def whisper_device() -> str:
    """``auto`` (CUDA, else CPU), ``cuda`` or ``cpu``."""
    return (_get("WS_WHISPER_DEVICE") or "auto").lower()
