"""Contact sheets Claude reads to segment a recording.

Per-minute sheets: 20 frames, 5 columns, a yellow ``H:MM:SS`` label on each
tile. When session.yaml already has segments, each label also carries the
segment the frame falls in (``#2 japanese``), so a glance at the sheets shows
whether the cut points line up with what's on screen.

Fine sheets: one per contiguous run of ``fine/`` frames (one boundary each).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from .media import FINE_STEP, frame_time
from .models import Session, fmt_short


COLS = 5
PER_SHEET = 20
LABEL_SIZE = 22
TIME_COLOUR = (255, 214, 0)
SEG_COLOUR = (120, 220, 255)
SKIP_COLOUR = (190, 190, 190)


FONT_CANDIDATES = [
    Path(__file__).with_name("fonts") / "DMSans-Bold.ttf",
    Path(r"C:\Windows\Fonts\arialbd.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]


def _find_font() -> Path:
    for f in FONT_CANDIDATES:
        if f.exists():
            return f
    raise RuntimeError("No font found for contact sheets (DM Sans ships in wabisanji_post/fonts).")


def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(_find_font()), size)
    except (OSError, RuntimeError):
        return ImageFont.load_default()


def _label(draw: ImageDraw.ImageDraw, x: int, y: int, t: float, session: Optional[Session], font) -> None:
    parts = [(fmt_short(t), TIME_COLOUR)]
    seg = session.segment_at(t) if session else None
    if seg is not None:
        parts.append((f"  #{seg.id} {seg.type}", SKIP_COLOUR if seg.type == "skip" else SEG_COLOUR))
    elif session is not None and session.segments:
        parts.append(("  (gap)", SKIP_COLOUR))
    width = sum(draw.textlength(text, font=font) for text, _ in parts)
    draw.rectangle([x, y, x + width + 12, y + LABEL_SIZE + 8], fill=(0, 0, 0))
    cx = x + 6
    for text, colour in parts:
        draw.text((cx, y + 3), text, font=font, fill=colour)
        cx += draw.textlength(text, font=font)


def build_sheet(frames: list[Path], target: Path, session: Optional[Session], cols: int = COLS,
                tile_w: Optional[int] = None) -> Path:
    """Tiles at the first frame's size, or scaled to ``tile_w`` (e.g. for 1920 px stills)."""
    first = Image.open(frames[0])
    tw, th = first.size
    if tile_w:
        tw, th = tile_w, round(th * tile_w / tw)
    rows = -(-len(frames) // cols)
    sheet = Image.new("RGB", (tw * cols, th * rows), (0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    font = _font(LABEL_SIZE)
    for i, f in enumerate(frames):
        x, y = (i % cols) * tw, (i // cols) * th
        with Image.open(f) as im:
            if im.size != (tw, th):
                im = im.resize((tw, th))
            sheet.paste(im.convert("RGB"), (x, y))
        _label(draw, x, y, frame_time(f), session, font)
    target.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(target, quality=85)
    return target


def _runs(frames: list[Path], step: float = FINE_STEP) -> list[list[Path]]:
    """Split time-sorted fine frames into contiguous runs (one per boundary)."""
    runs: list[list[Path]] = []
    for f in frames:
        if runs and frame_time(f) - frame_time(runs[-1][-1]) <= step * 1.5:
            runs[-1].append(f)
        else:
            runs.append([f])
    return runs


def build_all(session_dir: Path, session: Optional[Session]) -> list[tuple[Path, float, float]]:
    """Rebuild every sheet. Returns ``(sheet, first_t, last_t)`` per sheet."""
    sheets_dir = session_dir / "sheets"
    if sheets_dir.exists():
        for old in sheets_dir.glob("*.jpg"):  # sheets are derived; drop stale ones
            old.unlink()
    made = []
    minute = sorted((session_dir / "frames").glob("f_*.jpg"), key=frame_time)
    for n in range(0, len(minute), PER_SHEET):
        chunk = minute[n:n + PER_SHEET]
        target = sheets_dir / f"sheet_{n // PER_SHEET:02d}.jpg"
        made.append((build_sheet(chunk, target, session), frame_time(chunk[0]), frame_time(chunk[-1])))
    fine = sorted((session_dir / "fine").glob("f_*.jpg"), key=frame_time)
    for run in _runs(fine):
        for n in range(0, len(run), PER_SHEET):
            chunk = run[n:n + PER_SHEET]
            a, b = frame_time(chunk[0]), frame_time(chunk[-1])
            target = sheets_dir / f"fine_{int(a):05d}-{int(b):05d}.jpg"
            made.append((build_sheet(chunk, target, session), a, b))
    return made
