"""Episode memes: a few meme pop-overs added to each episode, no editor needed (the streamer 2026-10-20).

ARIA already pops memes live, and those are in the recording. This adds the
ones an editor would have: Claude (`/stream-memes`) reads the transcript and
writes ``memes.yaml`` in the session folder, one entry per pop-over:

    memes:
      - segment: 3
        at: "01:12:31.4"      # recording time the meme starts (just after the setup line)
        id: oh-no-anyway      # from content/stream/memes.yaml, the same library ARIA uses
        line: "and the whole base is on fire"
        why: "base burns down, the streamer shrugs"

There is no approval gate. ``plan`` applies the rules before anything is
rendered, and the episode's "Check before publishing" note lists every meme.
The rules: the meme must exist, sit in a kept part, keep its distance from the
others (live ones included), stay under the density cap, and family: false
memes never go into segments where family comes up (WS_FAMILY_PATTERN).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from . import settings
from .models import Segment, Session, fmt_tc, parse_tc

LIBRARY_YAML = settings.content_root() / "stream" / "memes.yaml"
PARSER_CONFIG = settings.parser_dir() / "config.yaml"
LIVE_EVENTS = settings.parser_dir() / "data" / "live" / "events.jsonl"
SESSION_MEMES = "memes.yaml"

MIN_GAP_S = 90.0  # between two inserted memes
LIVE_GAP_S = 45.0  # from a meme ARIA already popped live (it's in the recording)
SECONDS_PER_MEME = 150.0  # density cap: at most one per 2.5 min of episode, on average
DUCK = 0.35  # stream audio volume under a meme's sound
SHOW_IMAGE_S = 3.0  # images have no length of their own
MAX_MEME_S = 8.0
CLIP_EXT = {".mp4", ".webm", ".mov", ".m4v"}
SOUND_EXT = {".mp3", ".m4a", ".wav", ".ogg"}


@dataclass
class LibraryMeme:
    id: str
    file: str
    caption: str = ""
    category: str = ""
    risk: str = "high"
    family: bool = True
    about: str = ""

    @property
    def kind(self) -> str:
        ext = Path(self.file).suffix.lower()
        return "clip" if ext in CLIP_EXT else "sound" if ext in SOUND_EXT else "image"


@dataclass
class Insert:
    segment: int
    at: float  # recording time
    id: str
    line: str = ""
    why: str = ""


@dataclass
class Placed:
    insert: Insert
    meme: LibraryMeme
    path: Path
    duration: float
    file_at: float = 0.0  # position in the episode file, set once the segment is cut


def memes_dir() -> Path:
    """Where the files live: WABISANJI_MEMES, else the parser's stream.live.memes_dir."""
    if settings.memes_dir_override():
        return Path(settings.memes_dir_override())
    cfg = yaml.safe_load(PARSER_CONFIG.read_text(encoding="utf-8")) if PARSER_CONFIG.exists() else {}
    folder = (((cfg or {}).get("stream") or {}).get("live") or {}).get("memes_dir")
    if not folder:
        raise FileNotFoundError(f"no memes folder: set WABISANJI_MEMES or stream.live.memes_dir in {PARSER_CONFIG}")
    return Path(folder)


def load_library(path: Path = LIBRARY_YAML) -> dict[str, LibraryMeme]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    out = {}
    for m in (data or {}).get("memes") or []:
        out[m["id"]] = LibraryMeme(id=m["id"], file=m["file"], caption=m.get("caption") or "",
                                   category=m.get("category") or "", risk=m.get("risk") or "high",
                                   family=bool(m.get("family", True)), about=m.get("about") or "")
    return out


def load_inserts(session_dir: Path) -> list[Insert]:
    path = session_dir / SESSION_MEMES
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [Insert(segment=int(m["segment"]), at=parse_tc(m["at"]), id=str(m["id"]),
                   line=str(m.get("line") or ""), why=str(m.get("why") or ""))
            for m in data.get("memes") or []]


def live_memes(session: Session, events: Path = LIVE_EVENTS) -> list[tuple[float, str, str]]:
    """``(recording time, id, via)`` for memes ARIA popped during this recording."""
    if not events.exists():
        return []
    start = session.recording_start
    end = parse_tc(session.duration)
    out = []
    for line in events.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "meme":
            continue
        t = (dt.datetime.fromisoformat(e["t"]) - start).total_seconds()
        if 0 <= t <= end:
            out.append((t, e["id"], e.get("via", "")))
    return out


def kids_in(seg: Segment, session_dir: Path) -> bool:
    """A family word (``WS_FAMILY_PATTERN``) anywhere in the segment's transcript or notes."""
    text = seg.notes or ""
    tr = session_dir / "transcript" / f"seg{seg.id}.txt"
    if tr.exists():
        text += tr.read_text(encoding="utf-8")
    return bool(settings.family_pattern().search(text))


def media_duration(path: Path, kind: str) -> float:
    if kind == "image":
        return SHOW_IMAGE_S
    from .media import probe_duration
    return min(MAX_MEME_S, probe_duration(path))


def plan(session: Session, seg: Segment, session_dir: Path, *,
         library: Optional[dict[str, LibraryMeme]] = None, folder: Optional[Path] = None,
         inserts: Optional[list[Insert]] = None, live: Optional[list[tuple[float, str, str]]] = None,
         duration=media_duration) -> tuple[list[Placed], list[tuple[Insert, str]]]:
    """The inserts for ``seg`` that pass the rules, and the dropped ones with a reason."""
    library = load_library() if library is None else library
    folder = memes_dir() if folder is None else folder
    inserts = load_inserts(session_dir) if inserts is None else inserts
    live = live_memes(session) if live is None else live
    kids = kids_in(seg, session_dir)
    cap = max(1, int(seg.duration // SECONDS_PER_MEME))
    kept: list[Placed] = []
    dropped: list[tuple[Insert, str]] = []
    for ins in sorted((i for i in inserts if i.segment == seg.id), key=lambda i: i.at):
        meme = library.get(ins.id)
        path = folder / meme.file if meme else None
        if meme is None:
            why = f"no meme {ins.id!r} in the library"
        elif not path.is_file():
            why = f"file missing: {path}"
        elif not seg.contains(ins.at):
            why = "outside the segment or inside a cut"
        elif not meme.family and kids:
            why = "not family-friendly and family is in this segment"
        elif kept and ins.at - kept[-1].insert.at < MIN_GAP_S:
            why = f"within {MIN_GAP_S:.0f}s of the meme at {fmt_tc(kept[-1].insert.at)}"
        elif any(abs(ins.at - t) < LIVE_GAP_S for t, _, _ in live):
            why = "ARIA already popped a meme here live"
        elif len(kept) >= cap:
            why = f"over the cap of {cap} for this episode"
        else:
            length = duration(path, meme.kind)
            if not any(a <= ins.at and ins.at + length <= b for a, b in seg.parts):
                why = "runs into a cut or past the end"
            else:
                kept.append(Placed(ins, meme, path, length))
                continue
        dropped.append((ins, why))
    return kept, dropped


def caption_png(text: str, video_height: int, target: Path) -> Path:
    """A sound bite's line as a big speech pill: the live overlay's look, burned in."""
    from PIL import Image, ImageDraw, ImageFont

    from .render import ensure_fonts

    s = video_height / 1080
    font = ImageFont.truetype(str(ensure_fonts() / "NotoSansJP-Bold.otf"), max(12, round(46 * s)))
    pad_x, pad_y, edge = round(28 * s), round(14 * s), max(1, round(4 * s))
    left, top, right, bottom = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), text, font=font)
    w, h = right - left + 2 * pad_x, bottom - top + 2 * pad_y
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=round(14 * s), fill=(0x2B, 0x2F, 0x55, 235),
                        outline=(0xE8, 0xA0, 0x4A, 255), width=edge)
    d.text((pad_x - left, pad_y - top), text, font=font, fill=(0xF2, 0xEC, 0xDC, 255))
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target)
    return target


def check_lines(placed: list[Placed], live: list[tuple[float, str, str]], seg: Segment,
                library: dict[str, LibraryMeme]) -> list[str]:
    """Lines for the episode's "Check before publishing" note (recording times)."""
    lines = []
    for t, mid, via in live:
        if seg.contains(t):
            m = library.get(mid)
            lines.append(f"{fmt_tc(t)[:8]} live meme {mid} ({m.risk if m else '?'} risk, {via or 'live'})")
    for p in placed:
        lines.append(f"{fmt_tc(p.insert.at)[:8]} added meme {p.meme.id} ({p.meme.risk} risk)")
    return sorted(lines)
