"""Read secrets from the repo-root ``.env`` (gitignored). Never print values.

A tiny parser instead of python-dotenv: ``KEY=value`` lines, ``#`` comments,
optional surrounding quotes. Process environment variables win over the file.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


from .settings import repo_root

REPO_ROOT = repo_root()
ENV_FILE = REPO_ROOT / ".env"


@lru_cache(maxsize=1)
def _file_values(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def get(key: str, default: str = "") -> str:
    return os.environ.get(key) or _file_values().get(key) or default


def require(*keys: str) -> dict[str, str]:
    """Values for ``keys``; raises naming (never showing) any that are missing."""
    missing = [k for k in keys if not get(k)]
    if missing:
        raise RuntimeError(f"missing in {ENV_FILE}: {', '.join(missing)} (see .env.example)")
    return {k: get(k) for k in keys}


def status(*keys: str) -> dict[str, bool]:
    return {k: bool(get(k)) for k in keys}
