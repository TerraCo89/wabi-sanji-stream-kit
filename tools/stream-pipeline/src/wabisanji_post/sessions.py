"""Session folders, OBS filename parsing, and session.yaml / clips.yaml I/O.

Gate files are hand-edited (the streamer approves them and leaves comments such as
``# APPROVED by the streamer 2026-10-05``), so the pipeline never re-dumps an existing
session.yaml. Status changes go through :func:`set_status`, which rewrites only
the ``status:`` line.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from typing import Optional

import yaml

from . import settings
from .models import STATUS_ORDER, ClipsFile, Session



SESSION_YAML = "session.yaml"
CLIPS_YAML = "clips.yaml"


def sessions_root() -> Path:
    return settings.sessions_root()


def recordings_root() -> Path:
    return settings.recordings_root()


# ---------------------------------------------------------------------------
# OBS filenames
# ---------------------------------------------------------------------------

# OBS default "%CCYY-%MM-%DD %hh-%mm-%ss" → "2026-10-05 10-37-11.mkv"
_OBS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})$")


def parse_obs_filename(name: str | Path, tz: Optional[dt.tzinfo] = None) -> dt.datetime:
    """Recording start (local time, with offset) from an OBS output filename.

    ``tz`` defaults to this machine's local zone, DST included, which is what
    OBS used when it named the file. Raises ``ValueError`` for anything that
    isn't an OBS timestamp name (e.g. the older ``Alchemy Factory 1.mp4``).
    """
    stem = Path(name).stem
    m = _OBS_RE.match(stem)
    if not m:
        raise ValueError(f"not an OBS timestamp filename: {Path(name).name!r}")
    naive = dt.datetime(*(int(g) for g in m.groups()))  # also range-checks the fields
    if tz is not None:
        return naive.replace(tzinfo=tz)
    return naive.astimezone()  # naive → local zone with the right DST offset


# ---------------------------------------------------------------------------
# Session folders
# ---------------------------------------------------------------------------

def resolve_session_dir(ref: str | Path) -> Path:
    """Accept a session id (``2026-10-05``), a session folder, or a session.yaml path."""
    p = Path(ref)
    if p.name == SESSION_YAML:
        p = p.parent
    if p.is_dir() and (p / SESSION_YAML).exists():
        return p
    candidate = sessions_root() / str(ref)
    if (candidate / SESSION_YAML).exists():
        return candidate
    raise FileNotFoundError(
        f"No session {ref!r}: expected {candidate / SESSION_YAML}. Run stream-init first."
    )


def next_session_id(date: dt.date, root: Path) -> str:
    """``2026-10-05``, or ``2026-10-05_2`` / ``_3`` … when that folder is taken."""
    base = date.isoformat()
    if not (root / base).exists():
        return base
    n = 2
    while (root / f"{base}_{n}").exists():
        n += 1
    return f"{base}_{n}"


def find_session_for_recording(recording: Path, root: Path) -> Optional[Path]:
    """The session folder whose session.yaml points at ``recording``, if any."""
    target = os.path.normcase(str(recording.resolve()))
    if not root.exists():
        return None
    for yml in sorted(root.glob(f"*/{SESSION_YAML}")):
        try:
            data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
        except Exception:  # a broken session.yaml mustn't stop the scan
            continue
        rec = data.get("recording")
        if rec and os.path.normcase(str(Path(rec).resolve())) == target:
            return yml.parent
    return None


# ---------------------------------------------------------------------------
# YAML I/O
# ---------------------------------------------------------------------------

def load_session(session_dir: Path) -> Session:
    raw = yaml.safe_load((session_dir / SESSION_YAML).read_text(encoding="utf-8"))
    return Session.model_validate(raw)


def load_clips(session_dir: Path) -> ClipsFile:
    raw = yaml.safe_load((session_dir / CLIPS_YAML).read_text(encoding="utf-8"))
    return ClipsFile.model_validate(raw)


class _Timestamp(str):
    """Emitted unquoted (``session: 2026-10-05``) like the hand-written files."""


class _Dumper(yaml.SafeDumper):
    pass


_TIMECODE_STR = re.compile(r"^\d+:\d{2}:\d{2}(\.\d)?$")


def _repr_str(dumper: yaml.SafeDumper, value: str):
    style = '"' if _TIMECODE_STR.match(value) else None  # "00:15:00", as in the spec
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


def _repr_timestamp(dumper: yaml.SafeDumper, value: _Timestamp):
    return dumper.represent_scalar("tag:yaml.org,2002:timestamp", str(value))


_Dumper.add_representer(str, _repr_str)
_Dumper.add_representer(_Timestamp, _repr_timestamp)


def dump_yaml(model, header: str = "") -> str:
    data = model.model_dump(mode="json")
    resolver = yaml.resolver.Resolver()
    for key in ("session", "recording_start"):
        value = data.get(key)
        # Only real dates/times go out unquoted. "2026-09-15_2" isn't one: tagging it as a
        # timestamp wrote `!!timestamp '2026-09-15_2'`, which can't be read back.
        if isinstance(value, str) and resolver.resolve(yaml.ScalarNode, value, (True, False)) == "tag:yaml.org,2002:timestamp":
            data[key] = _Timestamp(value)
    body = yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120)
    return (header.rstrip("\n") + "\n" if header else "") + body


def write_new_session(session_dir: Path, session: Session, header: str) -> Path:
    """Write a fresh session.yaml. Refuses to overwrite: gate files are hand-edited."""
    path = session_dir / SESSION_YAML
    if path.exists():
        raise FileExistsError(f"{path} already exists; edit it by hand or use set_status")
    session_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dump_yaml(session, header).encode("utf-8"))
    return path


_RECORDING_LINE = re.compile(r"^(recording:[ \t]*)([^\r\n#]*?)([ \t]*(?:#[^\r\n]*)?)(?=\r?$)", re.MULTILINE)


def set_recording(session_dir: Path, new_path: Path) -> None:
    """Point ``recording:`` at a moved file, keeping comments and layout (stream-archive)."""
    path = session_dir / SESSION_YAML
    text = path.read_bytes().decode("utf-8")
    new, n = _RECORDING_LINE.subn(lambda m: m.group(1) + str(new_path) + m.group(3), text, count=1)
    if n != 1:
        raise ValueError(f"no top-level recording: line in {path}")
    Session.model_validate(yaml.safe_load(new))
    path.write_bytes(new.encode("utf-8"))


_STATUS_LINE = re.compile(r"^(status:[ \t]*)([A-Za-z_]+)", re.MULTILINE)


def set_status(session_dir: Path, status: str) -> None:
    """Change ``status:`` in session.yaml in place, keeping comments and layout."""
    if status not in STATUS_ORDER:
        raise ValueError(f"unknown status {status!r}")
    path = session_dir / SESSION_YAML
    text = path.read_bytes().decode("utf-8")  # bytes: keep the file's line endings
    new, n = _STATUS_LINE.subn(lambda m: m.group(1) + status, text, count=1)
    if n != 1:
        raise ValueError(f"no top-level status: line in {path}")
    Session.model_validate(yaml.safe_load(new))  # never write an invalid gate file
    path.write_bytes(new.encode("utf-8"))
