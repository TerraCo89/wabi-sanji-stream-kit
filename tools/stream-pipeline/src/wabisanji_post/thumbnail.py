"""Episode thumbnails: 1280x720 game frame, the streamer's reaction cutout, a big word or hook, series badge.

Fully automatic (the streamer, 2026-10-16): no approval gate. ``build_episode_thumbs`` scores the
``stream-thumbs`` candidate frames twice, once for the game (colourful, sharp, not a fade
or loading screen) and once for the streamer's face (a face in the cam box at a loud moment), so
the background and the face can come from different moments. It renders the best three
pairings to ``vods/seg<N>_thumbs/pick1-3.jpg``; ``stream-upload`` sets pick1.

The text comes from the ``thumb`` block Claude writes into ``vods/seg<N>.meta.json``:

    {"mode": "word", "text": "殺気", "reading": "さっき", "sub": "KILLING INTENT"}
    {"mode": "hook", "text": "20,000 *SILVER*?!"}         # *word* is drawn in amber

``at`` / ``face_at`` (recording times) in the block pin the game or face frame, and
``layout`` (a layouts.yaml key) overrides the one taken from the segment's clips. Without
a block: Japanese episodes use the captured word the streamer says most, others the episode hook.

Drawn with Pillow (ffmpeg drawtext has no font fallback for Japanese), in the Shorts
palette (render.py) with Noto Sans JP Black. Background removal is rembg on the CPU, so
this is safe while OBS is live.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from . import settings
from .models import Segment, Session, parse_tc

_cutout_sessions: dict[str, object] = {}  # rembg sessions by model: loading one takes seconds


def cutout_image(img: Image.Image, *, model: str = "isnet-general-use", alpha_threshold: int = 8) -> Image.Image:
    """Background-removed, tight-cropped RGBA copy of ``img`` (needs the `cutout` extra)."""
    try:
        from rembg import new_session, remove
    except ImportError as exc:
        raise RuntimeError("Reaction cutouts need rembg: uv sync --extra cutout") from exc
    if model not in _cutout_sessions:
        _cutout_sessions[model] = new_session(model)
    cut = remove(img.convert("RGBA"), session=_cutout_sessions[model])
    bbox = cut.getchannel("A").point(lambda a: 255 if a > alpha_threshold else 0).getbbox()
    if bbox is None:
        raise RuntimeError(f"Background removal left no subject pixels (model={model})")
    return cut.crop(bbox)


W, H = 1280, 720
SRC_W, SRC_H = 1920, 1080
MAX_BYTES = 2_000_000           # YouTube's thumbnail limit
VERSION = 6                     # bump when the look changes, so unchanged episodes rebuild

INDIGO = (0x2B, 0x2F, 0x55)
AMBER = (0xE8, 0xA0, 0x4A)
CREAM = (0xF2, 0xEC, 0xDC)
INK = (0x14, 0x16, 0x2B)        # text outline: darker than indigo so it holds on bright game art

DEFAULT_LAYOUT = {"japanese": "japanese_43", "gaming": "gaming"}
MARGIN = 48
FACE_H = 0.80                   # cutout height as a share of the canvas
PICKS = 3


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

TRAILING = {"a", "an", "and", "the", "of", "to", "with", "in", "on", "for", "i", "my", "but", "or", "at", "from"}


@dataclass
class ThumbText:
    mode: str                   # "word" or "hook"
    text: str
    reading: Optional[str] = None
    sub: Optional[str] = None


def _word_counts(session_dir: Path, seg: Segment) -> list[dict]:
    """Vocab captured in the segment, most-heard first (ties: first captured)."""
    p = session_dir / "signals" / "vocab.json"
    if not p.exists():
        return []
    words: dict[str, dict] = {}
    heard: Counter = Counter()
    for cap in json.loads(p.read_text(encoding="utf-8")).get("captures", []):
        if not seg.contains(cap["t"]):
            continue
        for w in cap.get("vocab", []):
            if w.get("category") == "pokemon" or not w.get("word"):
                continue
            words.setdefault(w["word"], w)
            heard[w["word"]] += len(w.get("heard") or [])
    order = list(words)
    return [words[k] for k in sorted(order, key=lambda k: (-heard[k], order.index(k)))]


def thumb_text(meta: dict, seg: Segment, session_dir: Path, japanese: bool) -> ThumbText:
    """The ``thumb`` block from meta.json, else a word (Japanese) or the hook (others)."""
    from .vods import _short_meaning

    block = meta.get("thumb") or {}
    if block.get("text"):
        mode = block.get("mode") or ("word" if japanese else "hook")
        return ThumbText(mode, block["text"], block.get("reading"), block.get("sub"))
    if japanese:
        for w in _word_counts(session_dir, seg):
            reading = w.get("reading") if w.get("reading") and w["reading"] != w["word"] else None
            return ThumbText("word", w["word"], reading, _short_meaning(w.get("meaning", "")).upper() or None)
    words = (meta.get("hook") or "").split()[:4]
    while len(words) > 1 and words[-1].lower().strip(",.:;") in TRAILING:   # "PODRACER TESTS AND" reads cut off
        words.pop()
    if words:
        return ThumbText("hook", " ".join(words).upper().rstrip(",.:;"))
    return ThumbText("hook", (seg.series or seg.game or seg.label or "").upper())


def badge_label(seg: Segment, series: dict, session_id: str) -> str:
    from .vods import episode_label

    name = series.get("thumb_label") or (seg.game or "").replace(" (JP)", "") or seg.series or seg.label or ""
    label = episode_label(series, session_id, seg.episode_segment) if series else "#?"
    return name.upper() + (f" · EP {label[1:]}" if label != "#?" else "")


# ---------------------------------------------------------------------------
# Layout and frame scoring
# ---------------------------------------------------------------------------

def pick_layout(session_dir: Path, seg: Segment, layouts: dict) -> Optional[str]:
    """The layout most of the segment's clips use; else a default by segment type."""
    from . import sessions

    keys: Counter = Counter()
    if (session_dir / sessions.CLIPS_YAML).exists():
        try:
            keys.update(c.layout for c in sessions.load_clips(session_dir).clips
                        if c.segment == seg.id and c.layout in layouts)
        except Exception:  # an invalid clips.yaml shouldn't stop thumbnails
            pass
    if keys:
        return keys.most_common(1)[0][0]
    if seg.full_of is not None and seg.full_of != seg.id:   # the full version looks like its edit
        try:
            from . import sessions as _s
            return pick_layout(session_dir, _s.load_session(session_dir).segment(seg.full_of), layouts)
        except (KeyError, FileNotFoundError):
            pass
    key = DEFAULT_LAYOUT.get(seg.type)
    return key if key in layouts else None


def _signal(t: float, scores: list[tuple[float, float]], window: float = 3.0) -> float:
    return max((s for s, at in scores if abs(at - t) <= window), default=0.0)


def game_stats(img: Image.Image) -> tuple[float, float, float, float]:
    """(mean luma, luma spread, colourfulness, sharpness) on a 320 px copy."""
    import cv2
    import numpy as np

    small = np.asarray(img.convert("RGB").resize((320, max(1, round(320 * img.height / img.width)))), dtype=np.float32)
    r, g, b = small[..., 0], small[..., 1], small[..., 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    rg, yb = r - g, 0.5 * (r + g) - b   # Hasler and Süsstrunk colourfulness
    colour = float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean()))
    sharp = float(cv2.Laplacian(luma, cv2.CV_32F).var())
    return float(luma.mean()), float(luma.std()), colour, sharp


def usable_game(stats: tuple[float, float, float, float]) -> bool:
    mean, spread, _, _ = stats
    return 30 <= mean <= 235 and spread >= 18   # fades, black loading screens, white flashes


def rank_games(stats: dict[float, tuple], scores: list[tuple[float, float]],
               bonus_at: list[float] = (), start: Optional[float] = None) -> list[float]:
    """Frame times, best background first: colourful and sharp, near the featured word if any.
    Frames in the first few minutes after ``start`` lose out: title screens and menus are
    colourful but say nothing about the episode (Alchemy Factory's title art won twice)."""
    ok = {t: s for t, s in stats.items() if usable_game(s)} or stats
    top_c = max((s[2] for s in ok.values()), default=1.0) or 1.0
    top_s = max((s[3] for s in ok.values()), default=1.0) or 1.0

    def score(t: float) -> float:
        _, _, c, sh = ok[t]
        near = any(abs(t - b) <= 20 for b in bonus_at)
        early = start is not None and t - start < 240
        return (c / top_c + 0.7 * sh / top_s + (0.5 if near else 0.0) + 0.02 * _signal(t, scores)
                - (0.8 if early else 0.0))
    return sorted(ok, key=score, reverse=True)


def rank_faces(faces: dict[float, tuple[float, float, float]], scores: list[tuple[float, float]]) -> list[float]:
    """Frame times with a face, best reaction first: loud moments, then bigger faces."""
    if not faces:
        return []
    top_sig = max((_signal(t, scores) for t in faces), default=0.0) or 1.0
    top_size = max(f[2] for f in faces.values())
    return sorted(faces, key=lambda t: _signal(t, scores) / top_sig + 0.5 * faces[t][2] / top_size, reverse=True)


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def _font(size: int, weight: str = "Black") -> ImageFont.FreeTypeFont:
    from .render import ensure_fonts
    return ImageFont.truetype(str(ensure_fonts() / f"NotoSansJP-{weight}.otf"), size)


def _measure(text: str, font: ImageFont.FreeTypeFont, stroke: int = 0) -> tuple[int, int, int, int]:
    return ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), text, font=font, stroke_width=stroke)


def fit_size(text: str, max_w: int, max_h: int, start: int, floor: int = 28, weight: str = "Black") -> int:
    """Largest size from ``start`` down whose single line fits ``max_w`` x ``max_h``."""
    size = start
    while size > floor:
        l, t, r, b = _measure(text, _font(size, weight), max(2, size // 14))
        if r - l <= max_w and b - t <= max_h:
            break
        size -= 4
    return size


def wrap_hook(text: str, max_w: int, max_h: int, max_lines: int = 3, start: int = 150,
              floor: int = 40) -> tuple[int, list[str]]:
    """(size, lines) for the biggest size at which the words wrap into ``max_lines`` lines."""
    words = text.split()
    size = start
    while True:
        font, stroke = _font(size), max(2, size // 14)
        lines: list[str] = []
        for w in words:
            cand = f"{lines[-1]} {w}" if lines else w
            if lines and _width(cand.replace("*", ""), font, stroke) <= max_w:
                lines[-1] = cand
            else:
                lines.append(w)
        widest = max((_width(ln.replace("*", ""), font, stroke) for ln in lines), default=0)
        if (len(lines) <= max_lines and widest <= max_w and len(lines) * size * 1.12 <= max_h) or size <= floor:
            return size, lines
        size -= 4


def _width(text: str, font, stroke: int) -> int:
    l, _, r, _ = _measure(text, font, stroke)
    return r - l


def _outlined(d: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font, fill, stroke: int) -> None:
    x, y = xy
    off = max(3, stroke // 2)
    d.text((x + off, y + off), text, font=font, fill=(0, 0, 0, 150), stroke_width=stroke, stroke_fill=(0, 0, 0, 150))
    d.text((x, y), text, font=font, fill=fill, stroke_width=stroke, stroke_fill=INK)


def hide_boxes(frame: Image.Image, boxes) -> Image.Image:
    """Blur and darken ``boxes`` (source pixels) so chat overlays can't be read."""
    if not boxes:
        return frame
    frame = frame.convert("RGB").copy()
    sx, sy = frame.width / SRC_W, frame.height / SRC_H
    for x, y, w, h in boxes:
        box = (round(x * sx), round(y * sy), round(min(SRC_W, x + w) * sx), round(min(SRC_H, y + h) * sy))
        region = frame.crop(box).filter(ImageFilter.GaussianBlur(round(28 * sx)))
        frame.paste(ImageEnhance.Brightness(region).enhance(0.6), box[:2])
    return frame


def _background(frame: Image.Image, crop: Optional[tuple[int, int, int, int]], hide=()) -> Image.Image:
    frame = hide_boxes(frame, hide)
    if crop:
        sx, sy = frame.width / SRC_W, frame.height / SRC_H
        x, y, w, h = crop
        frame = frame.crop((round(x * sx), round(y * sy), round((x + w) * sx), round((y + h) * sy)))
    bg = ImageOps.fit(trim_bars(frame.convert("RGB")), (W, H), Image.LANCZOS)
    bg = ImageEnhance.Color(bg).enhance(1.25)
    return ImageEnhance.Contrast(bg).enhance(1.1)


def trim_bars(img: Image.Image, limit: float = 0.2) -> Image.Image:
    """Cut black letterbox/pillarbox bars (cutscenes, 4:3 games), up to ``limit`` of each side."""
    import numpy as np

    luma = np.asarray(img.convert("L"), dtype=np.float32)
    rows, cols = np.percentile(luma, 70, axis=1), np.percentile(luma, 70, axis=0)   # the facecam may overlap a bar

    def edge(v, n):
        k = 0
        while k < int(n * limit) and v[k] < 14:
            k += 1
        return k
    top, bottom = edge(rows, len(rows)), edge(rows[::-1], len(rows))
    left, right = edge(cols, len(cols)), edge(cols[::-1], len(cols))
    if not (top or bottom or left or right):
        return img
    return img.crop((left, top, img.width - right, img.height - bottom))


def _scrim(width: int) -> Image.Image:
    """Indigo fading out left to right over ``width`` px, for the text side."""
    grad = Image.new("L", (W, 1), 0)
    for x in range(min(W, width)):
        grad.putpixel((x, 0), round(225 * (1 - x / width) ** 1.4))
    mask = grad.resize((W, H))
    layer = Image.new("RGBA", (W, H), INDIGO + (0,))
    layer.putalpha(mask)
    return layer


def _sticker(cut: Image.Image, edge: int = 9) -> Image.Image:
    """The cutout with a cream outline and a soft shadow (also hides upscaled matte edges)."""
    pad = edge * 3
    a = ImageOps.expand(cut.getchannel("A"), pad, 0).point(lambda v: 255 if v > 40 else 0)
    ring = a.filter(ImageFilter.MaxFilter(2 * edge + 1))
    out = Image.new("RGBA", a.size, (0, 0, 0, 0))
    shadow = Image.new("RGBA", a.size, (0, 0, 0, 0))
    shadow.putalpha(ring.filter(ImageFilter.GaussianBlur(edge)).point(lambda v: int(v * 0.55)))
    out.alpha_composite(shadow, (edge // 2, edge // 2))
    cream = Image.new("RGBA", a.size, CREAM + (255,))
    cream.putalpha(ring)
    out.alpha_composite(cream)
    out.alpha_composite(ImageOps.expand(cut, pad, (0, 0, 0, 0)))
    return out


def _badge(label: str) -> Image.Image:
    font = _font(30)
    name, _, ep = label.partition(" · ")
    l, t, r, b = _measure(label, font)
    pad_x, pad_y = 22, 12
    img = Image.new("RGBA", (r - l + 2 * pad_x, b - t + 2 * pad_y), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=img.height // 2,
                        fill=INDIGO + (235,), outline=AMBER + (255,), width=3)
    d.text((pad_x - l, pad_y - t), name, font=font, fill=CREAM)
    if ep:
        x = pad_x - l + _measure(name + " · ", font)[2]
        d.text((x, pad_y - t), ep, font=font, fill=AMBER)
    return img


def _draw_word(canvas: Image.Image, text: ThumbText, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(canvas)
    max_w, max_h = x1 - x0, y1 - y0
    word_size = fit_size(text.text, max_w, int(max_h * 0.55), start=260)
    word_font, word_stroke = _font(word_size), max(4, word_size // 14)
    parts: list[tuple[str, object, int, tuple, int]] = []   # (text, font, stroke, fill, gap after)
    if text.reading:
        rs = min(64, fit_size(text.reading, max_w, 80, start=64, weight="Bold"))
        parts.append((text.reading, _font(rs, "Bold"), max(3, rs // 10), AMBER, 4))
    parts.append((text.text, word_font, word_stroke, CREAM, 18))
    sub_img = None
    if text.sub:
        ss = fit_size(text.sub, max_w - 44, 90, start=60)
        sf = _font(ss)
        l, t, r, b = _measure(text.sub, sf)
        sub_img = Image.new("RGBA", (r - l + 44, b - t + 26), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sub_img)
        sd.rounded_rectangle((0, 0, sub_img.width - 1, sub_img.height - 1), radius=14, fill=AMBER + (255,),
                             outline=INK + (255,), width=4)
        sd.text((22 - l, 13 - t), text.sub, font=sf, fill=INK)
    heights = [_measure(p[0], p[1], p[2]) for p in parts]
    total = sum(b - t + p[4] for p, (_, t, _, b) in zip(parts, heights)) + (sub_img.height if sub_img else 0)
    y = y0 + max(0, (max_h - total) // 2)
    for (s, f, st, fill, gap), (l, t, _, b) in zip(parts, heights):
        _outlined(d, (x0 - l, y - t), s, f, fill, st)
        y += b - t + gap
    if sub_img:
        canvas.alpha_composite(sub_img, (x0, min(y, y1 - sub_img.height)))


def _draw_hook(canvas: Image.Image, text: ThumbText, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    size, lines = wrap_hook(text.text.upper(), x1 - x0, y1 - y0)
    font, stroke = _font(size), max(4, size // 14)
    d = ImageDraw.Draw(canvas)
    line_h = round(size * 1.12)
    y = y0 + max(0, (y1 - y0 - line_h * len(lines)) // 2)
    for ln in lines:
        x = x0
        for piece in (re.split(r"(\*[^*]+\*)", ln)):
            if not piece:
                continue
            hot = piece.startswith("*") and piece.endswith("*")
            s = piece.strip("*") if hot else piece
            _outlined(d, (x, y - _measure("A", font, stroke)[1]), s, font, AMBER if hot else CREAM, stroke)
            x += round(font.getlength(s))
        y += line_h


def render_thumb(game: Image.Image, face: Optional[Image.Image], text: ThumbText, badge: str,
                 crop: Optional[tuple[int, int, int, int]] = None, hide=()) -> Image.Image:
    """The finished 1280x720 thumbnail. ``face`` is a tight RGBA cutout (or None); ``hide``
    boxes (source pixels) are blurred out of the game frame first."""
    canvas = _background(game, crop, hide).convert("RGBA")
    face_w = 0
    if face is not None:
        sticker = _sticker(face)
        scale = FACE_H * H / sticker.height
        if sticker.width * scale > 0.56 * W:   # wide and short when the cam sits at the frame's bottom edge
            scale = 0.56 * W / sticker.width
        sticker = sticker.resize((round(sticker.width * scale), round(sticker.height * scale)), Image.LANCZOS)
        face_w = sticker.width
        canvas.alpha_composite(_scrim(round(W * 0.68)))
        canvas.alpha_composite(sticker, (W - sticker.width + 6, H - sticker.height + round(0.06 * sticker.height)))
    else:
        canvas.alpha_composite(_scrim(round(W * 0.75)))
    b = _badge(badge) if badge else None
    if b:
        canvas.alpha_composite(b, (MARGIN - 8, 30))
    top = 30 + (b.height if b else 0) + 24
    right = W - MARGIN - (round(face_w * 0.78) if face_w else 0)
    right = max(right, round(W * 0.52))
    box = (MARGIN, top, right, H - 44)
    (_draw_word if text.mode == "word" else _draw_hook)(canvas, text, box)
    return canvas.convert("RGB")


def save_jpeg(img: Image.Image, target: Path) -> Path:
    for q in (90, 85, 78, 70):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=True)
        if buf.tell() <= MAX_BYTES:
            break
    target.write_bytes(buf.getvalue())
    return target


# ---------------------------------------------------------------------------
# Faces
# ---------------------------------------------------------------------------

def cam_region(layout) -> Optional[tuple[float, float, float, float]]:
    """Where to look for the streamer's face (frame fractions), from the layout's cam box."""
    if layout is None or layout.cam is None or not layout.thumb_face:
        return None
    if layout.cam.region:
        return layout.cam.region
    # Around the measured box rather than facecam.REGION: that covers half the frame, and
    # in a single still a face in the game art (or a chat avatar) passes for the streamer.
    x, y, w, h = layout.cam.crop
    m = 220
    return (max(0, x - m) / SRC_W, max(0, y - m) / SRC_H, min(SRC_W, x + w + m) / SRC_W, min(SRC_H, y + h + m) / SRC_H)


def find_face(frame: Image.Image, region) -> Optional[tuple[float, float, float]]:
    """(cx, cy, size) in the frame's pixels. Full resolution inside ``region``, strict first,
    then looser (a cap or a turned head loses the strict detector)."""
    import numpy as np

    from .facecam import largest_face

    x0, y0 = round(region[0] * frame.width), round(region[1] * frame.height)
    box = frame.convert("L").crop((x0, y0, round(region[2] * frame.width), round(region[3] * frame.height)))
    gray = np.asarray(box)
    for neighbours in (6, 3):
        found = largest_face(gray, (0, 0, 1, 1), 1.0, neighbours)
        if found and found[2] >= 50:
            return found[0] + x0, found[1] + y0, found[2]
    return None


def consistent_faces(faces: dict[float, tuple[float, float, float]]) -> dict[float, tuple[float, float, float]]:
    """Drop detections away from where most of them are: The streamer sits in one place per segment."""
    import statistics

    if len(faces) < 3:
        return faces
    mx = statistics.median(f[0] for f in faces.values())
    my = statistics.median(f[1] for f in faces.values())
    ms = statistics.median(f[2] for f in faces.values())
    return {t: f for t, f in faces.items() if abs(f[0] - mx) <= 1.5 * ms and abs(f[1] - my) <= 1.5 * ms}


def plausible_cutout(cut: Image.Image, crop_size: tuple[int, int]) -> bool:
    """A person-sized cutout: tall enough for head and shoulders, and mostly solid."""
    import numpy as np

    a = np.asarray(cut.getchannel("A")) > 40
    return (cut.height >= 0.45 * crop_size[1] and a.sum() >= 0.10 * crop_size[0] * crop_size[1]
            and a.mean() >= 0.35)


def cam_face(layout) -> tuple[float, float, float]:
    """A stand-in face in the middle of the layout's cam box, for when no face is detected."""
    x, y, w, h = layout.cam.crop
    return x + w / 2, y + 0.4 * h, w / 3.8


def face_crop(frame: Image.Image, face: tuple[float, float, float],
              box: Optional[tuple[int, int, int, int]] = None) -> Image.Image:
    """Head and chest around a detected face, inside ``box`` (source pixels) if given. It stops
    at the chest: lower down a pet on the streamer's lap joins the cutout as a blob,
    and the straight bottom edge sits below the canvas anyway."""
    cx, cy, size = face
    x0, x1 = max(0, cx - 2.2 * size), min(frame.width, cx + 2.2 * size)
    y0, y1 = max(0, cy - 1.25 * size), min(frame.height, cy + 1.8 * size)
    if box:
        k = frame.width / SRC_W
        bx, by, bw, bh = (v * k for v in box)
        x0, y0, x1, y1 = max(x0, bx), max(y0, by), min(x1, bx + bw), min(y1, by + bh)
    return frame.crop((round(x0), round(y0), round(x1), round(y1)))


def _largest_blob(cut: Image.Image) -> Image.Image:
    """Keep the biggest opaque region (the streamer), dropping stray bits rembg kept (a dog bed, a lamp)."""
    import cv2
    import numpy as np

    a = np.asarray(cut.getchannel("A"))
    n, labels, stats, _ = cv2.connectedComponentsWithStats((a > 40).astype(np.uint8), 8)
    if n <= 2:
        return cut
    keep = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    mask = np.where(labels == keep, a, 0).astype(np.uint8)
    out = cut.copy()
    out.putalpha(Image.fromarray(mask))
    bbox = out.getchannel("A").getbbox()
    return out.crop(bbox) if bbox else cut


# ---------------------------------------------------------------------------
# One episode
# ---------------------------------------------------------------------------

def _inputs_hash(*parts) -> str:
    return hashlib.sha256(json.dumps([VERSION, *parts], sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()[:16]


def build_episode_thumbs(session: Session, seg: Segment, session_dir: Path, *, force: bool = False,
                         say: Callable[[str], None] = print,
                         cutout: Optional[Callable[[Image.Image], Image.Image]] = None) -> list[Path]:
    """Render pick1-3.jpg for one episode; returns the files (empty if already up to date)."""
    from . import media, vods
    from .render import load_layouts
    from .media import extract_frame

    layouts = load_layouts()
    key = pick_layout(session_dir, seg, layouts)
    layout = layouts.get(key) if key else None
    tdir = vods.thumbs_dir(session_dir, seg.id)
    cand_dir = tdir / "candidates"
    if not any(cand_dir.glob("f_*.jpg")):
        say(f"  segment {seg.id}: no candidates yet, extracting frames")
        vods.extract_thumbs(session, seg, session_dir)
    meta_p = session_dir / "vods" / f"seg{seg.id}.meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    side = tdir / "text.json"   # sessions from before meta.json (2026-10-05) keep the block here
    if not meta.get("thumb") and side.exists():
        meta["thumb"] = json.loads(side.read_text(encoding="utf-8"))
    if (meta.get("thumb") or {}).get("layout") in layouts:   # a segment with no clips to take it from
        key = meta["thumb"]["layout"]
        layout = layouts[key]
    series = vods.load_series().get(seg.series or "", {})
    japanese = seg.type == "japanese" or series.get("type") == "japanese"
    text = thumb_text(meta, seg, session_dir, japanese)
    badge = badge_label(seg, series, session.session)
    block = meta.get("thumb") or {}
    pins = {}
    for k in ("at", "face_at"):
        if block.get(k) is not None:
            t = parse_tc(block[k]) if isinstance(block[k], str) else float(block[k])
            pins[k] = extract_frame(Path(session.recording), t, cand_dir / media.frame_name(t))
    cands = sorted(cand_dir.glob("f_*.jpg"))
    stamp = tdir / "thumb.json"
    digest = _inputs_hash(asdict(text), badge, key, [p.name for p in cands], {k: p.name for k, p in pins.items()})
    picks = [tdir / f"pick{i}.jpg" for i in range(1, PICKS + 1)]
    if not force and stamp.exists() and all(p.exists() for p in picks[:1]):
        if json.loads(stamp.read_text(encoding="utf-8")).get("inputs") == digest:
            return []

    scores = vods.thumb_scores(seg, session_dir)
    frames = {media.frame_time(p): p for p in cands}
    crop = layout.thumb if layout else None
    stats, faces = {}, {}
    region = cam_region(layout)
    for t, p in frames.items():
        with Image.open(p) as im:
            im.load()
            g = im
            if crop:
                sx = im.width / SRC_W
                x, y, w, h = crop
                g = im.crop((round(x * sx), round(y * sx), round((x + w) * sx), round((y + h) * sx)))
            stats[t] = game_stats(g)
            if region:
                f = find_face(im, region)
                if f:
                    faces[t] = f
    faces = consistent_faces(faces)
    guessed = False
    if region and not faces:
        # Nothing detected (a cap, a turned head): cut the streamer out of the cam box at the loudest moments.
        say(f"  [yellow]segment {seg.id}: no face detected in the cam box ({key}); using the box itself[/yellow]")
        faces = {t: cam_face(layout) for t in frames}
        guessed = True
    bonus = []
    if text.mode == "word":
        for w in _word_counts(session_dir, seg):
            if w["word"] == text.text:
                bonus = [h["t"] for h in w.get("heard") or []]
                break
    games = [media.frame_time(pins["at"])] if "at" in pins else []
    games += [t for t in rank_games(stats, scores, bonus, seg.parts[0][0]) if t not in games]
    face_ts = rank_faces(faces, scores)
    if "face_at" in pins:
        with Image.open(pins["face_at"]) as im:
            f = find_face(im, region) if region else None
        if not f and layout is not None and layout.cam is not None:
            say(f"  [yellow]segment {seg.id}: no face detected at face_at; cutting from the cam box there[/yellow]")
            f = cam_face(layout)
        if f:
            t = media.frame_time(pins["face_at"])
            faces[t] = f
            face_ts = [t] + [x for x in face_ts if x != t]
    if cutout is None:
        cutout = cutout_image

    cut_cache: dict[float, Optional[Image.Image]] = {}

    def cut_face(ft: float) -> Optional[Image.Image]:
        if ft not in cut_cache:
            src = pins["face_at"] if ("face_at" in pins and ft == media.frame_time(pins["face_at"])) else frames[ft]
            face_img = None
            with Image.open(src) as im:
                region_img = face_crop(im.convert("RGB"), faces[ft], layout.thumb_face_box)
                try:
                    face_img = _largest_blob(cutout(region_img))
                except RuntimeError as exc:
                    say(f"  [yellow]segment {seg.id}: cutout failed at {ft:.0f}s ({exc})[/yellow]")
            if face_img is not None and not plausible_cutout(face_img, region_img.size):
                say(f"  [yellow]segment {seg.id}: cutout at {ft:.0f}s doesn't look like {settings.streamer_name()}; trying another[/yellow]")
                face_img = None
            cut_cache[ft] = face_img
        return cut_cache[ft]

    written, record = [], []
    for i, target in enumerate(picks):
        if i >= len(games):
            break
        gt = games[i]
        ft, face_img = None, None
        for k in range(len(face_ts)):   # this pick's face first, then the next ones that cut out cleanly
            cand = face_ts[(i + k) % len(face_ts)]
            face_img = cut_face(cand)
            if face_img is not None:
                ft = cand
                break
        src = pins["at"] if (i == 0 and "at" in pins) else frames[gt]
        with Image.open(src) as im:
            img = render_thumb(im, face_img, text, badge, crop, layout.thumb_hide if layout else ())
        save_jpeg(img, target)
        written.append(target)
        record.append({"file": target.name, "game_t": gt, "face_t": ft})
    stamp.write_text(json.dumps({
        "inputs": digest, "built": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "layout": key, "face_guessed": guessed, "badge": badge, "text": asdict(text), "picks": record,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return written


# ---------------------------------------------------------------------------
# What's on YouTube
# ---------------------------------------------------------------------------

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def is_pushed(pick: Path, video_id: str) -> bool:
    """``pick`` is what YouTube has for ``video_id`` (per ``pushed.json`` next to it)."""
    p = pick.parent / "pushed.json"
    if not p.exists():
        return False
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("video_id") == video_id and data.get("sha") == _sha(pick)


def mark_pushed(pick: Path, video_id: str) -> None:
    (pick.parent / "pushed.json").write_text(json.dumps({
        "video_id": video_id, "file": pick.name, "sha": _sha(pick),
        "pushed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }), encoding="utf-8")
