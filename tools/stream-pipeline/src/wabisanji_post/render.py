"""Render approved clips as 9:16 Shorts (spec: "Rendering Shorts").

One ffmpeg pass per clip: blurred game fills 1080x1920, the game crop sits on
top, the facecam below (rects from ``layouts.yaml``), the pixel-art
twitch.tv/WabiSanji badge (``badge.py``) where the layout has a spot for it,
captions burned in with libass, audio loudness-normalised to -14 LUFS (two-pass
loudnorm), NVENC.

Captions reuse ``shorts.py`` (``slice_words`` → ``apply_caption_fixes`` →
``build_ass``) with the Wabi Sanji style. Japanese clips also get vocab word
cards (word, reading, meaning) in the top band, timed to when the streamer says the
word (or when it was captured), like the stream overlay.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

import yaml

from . import badge as badge_png
from . import settings
from .captions import CaptionStyle, _ts, apply_caption_fixes, build_ass, slice_words
from .media import _escape_filter_path, _run_ffmpeg, extract_frame
from .media import REF_H, REF_W, probe_video_size, to_source
from .models import Clip, Session


logger = logging.getLogger(__name__)

OUT_W, OUT_H = 1080, 1920
LAYOUTS_YAML = settings.layouts_path()
FONTS_DIR = settings.fonts_dir()
NOTO_URL = "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/SubsetOTF/JP/NotoSansJP-{w}.otf"
NOTO_WEIGHTS = ("Regular", "Bold", "Black")

# Wabi Sanji palette (ASS colours are &HAABBGGRR; alpha 00 = opaque, 33 = 80 % opaque)
INDIGO = "&H552F2B&"      # #2b2f55
AMBER = "&H4AA0E8&"       # #e8a04a
CREAM = "&HDCECF2&"       # #f2ecdc
CAPTION_STYLE = CaptionStyle(
    font="Noto Sans JP",
    size=70,
    primary="&H00DCECF2",
    outline_colour="&H33552F2B",  # BorderStyle 3 draws the box in OutlineColour
    back="&H00000000",
    bold=True,
    border_style=3,
    outline=14,
    shadow=0,
    margin_lr=60,
    highlight="&H004AA0E8",
    uppercase=False,
    max_words=3,
)

LOUDNESS = {"I": -14.0, "TP": -1.5, "LRA": 11.0}


# ---------------------------------------------------------------------------
# Layouts and fonts
# ---------------------------------------------------------------------------

@dataclass
class Box:
    crop: tuple[int, int, int, int]
    width: int
    y: int
    auto: bool = False          # cam only: follow the detected face (facecam.py), crop is the fallback
    region: Optional[tuple[float, float, float, float]] = None   # auto: where to look (frame fractions)

    @property
    def height(self) -> int:
        _, _, w, h = self.crop
        return round(self.width * h / w / 2) * 2


BADGE_Y = 24   # badge top edge: in the band above the game, clear of the Shorts UI's corner icons


@dataclass
class BadgeSpot:
    y: int
    x: Optional[int] = None     # None: centred


@dataclass
class Layout:
    key: str
    game: Box
    cam: Optional[Box]          # None: no facecam in the scene (Desk cam), game box only
    captions_bottom: int
    cards_y: Optional[int] = None
    badge: Optional[BadgeSpot] = None   # twitch.tv/WabiSanji badge; None: no badge in this layout
    thumb: Optional[tuple[int, int, int, int]] = None   # episode thumbnail background; None: the whole frame
    thumb_hide: tuple[tuple[int, int, int, int], ...] = ()   # blurred out of the thumbnail (chat overlays)
    thumb_face: bool = True     # False: the cam box isn't the streamer (irl: the phone screen)
    thumb_face_box: Optional[tuple[int, int, int, int]] = None   # cut the streamer out of this box only


def load_layouts(path: Path = LAYOUTS_YAML) -> dict[str, Layout]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = {}
    for key, d in raw.items():
        boxes = {k: Box(tuple(d[k]["crop"]), int(d[k]["width"]), int(d[k]["y"]), bool(d[k].get("auto", False)),
                        tuple(d[k]["region"]) if d[k].get("region") else None)
                 for k in ("game", "cam") if d.get(k)}
        b = d.get("badge", {})   # default: top centre; `badge: false` turns it off for the layout
        lay = Layout(key, boxes["game"], boxes.get("cam"), int(d["captions"]["bottom"]),
                     (d.get("cards") or {}).get("y"),
                     BadgeSpot(int(b.get("y", BADGE_Y)), b.get("x")) if b not in (False, None) else None,
                     tuple((d.get("thumb") or {})["crop"]) if (d.get("thumb") or {}).get("crop") else None,
                     tuple(tuple(b) for b in (d.get("thumb") or {}).get("hide", [])),
                     bool((d.get("thumb") or {}).get("face", True)),
                     tuple((d.get("thumb") or {})["face_box"]) if (d.get("thumb") or {}).get("face_box") else None)
        for name, b in boxes.items():
            x, y, w, h = b.crop
            if x < 0 or y < 0 or x + w > REF_W or y + h > REF_H:
                raise ValueError(f"layout {key}.{name}: crop {b.crop} is outside {REF_W}x{REF_H}")
            if b.width > OUT_W or b.y + b.height > OUT_H:
                raise ValueError(f"layout {key}.{name}: placed box runs off the 1080x1920 frame")
        out[key] = lay
    return out


def for_source(layout: Layout, size: tuple[int, int]) -> Layout:
    """The layout with its game and cam crops in the recording's own pixels.

    Crops are measured in 1920x1080 reference pixels; a 2560x1440 recording is cropped
    at its full resolution rather than scaled down first. Placement in the Short is unchanged.
    """
    if size == (REF_W, REF_H):
        return layout
    cam = replace(layout.cam, crop=to_source(layout.cam.crop, size)) if layout.cam else None
    return replace(layout, game=replace(layout.game, crop=to_source(layout.game.crop, size)), cam=cam)


def ensure_fonts(fonts_dir: Path = FONTS_DIR) -> Path:
    """Noto Sans JP (OFL) next to the other fonts; downloaded once, not committed."""
    for w in NOTO_WEIGHTS:
        target = fonts_dir / f"NotoSansJP-{w}.otf"
        if not target.exists():
            logger.info("Downloading NotoSansJP-%s.otf …", w)
            urllib.request.urlretrieve(NOTO_URL.format(w=w), target)
    return fonts_dir


# ---------------------------------------------------------------------------
# Vocab word cards
# ---------------------------------------------------------------------------

@dataclass
class Card:
    t: float  # clip-relative
    word: str
    reading: Optional[str]
    meaning: str


def _short_meaning(meaning: str, limit: int = 40) -> str:
    parts = [p.strip() for p in re.split(r";", meaning or "") if p.strip()]
    out = ""
    for p in parts:
        cand = f"{out}; {p}" if out else p
        if len(cand) > limit and out:
            break
        out = cand
    return out[: limit + 10]


def vocab_cards(clip: Clip, vocab_json: Path, *, lookback: float = 60.0) -> list[Card]:
    """Cards for words captured shortly before or during the clip.

    A card appears when the streamer says the word inside the clip (``heard``),
    otherwise at the capture time if that's inside the clip. One card per word.
    """
    if not vocab_json.exists():
        return []
    data = json.loads(vocab_json.read_text(encoding="utf-8"))
    caps = [c for c in data.get("captures", [])
            if c.get("segment") == clip.segment and clip.start - lookback <= c["t"] <= clip.end and c["vocab"]]
    # The overlay is already showing the last capture before the clip: open on it.
    before = [c for c in caps if c["t"] < clip.start]
    opening = before[-1]["screenshot_id"] if before else None
    cards: dict[str, Card] = {}
    for cap in caps:
        for v in cap["vocab"]:
            heard = [h["t"] for h in v.get("heard", []) if clip.start <= h["t"] <= clip.end]
            if heard:
                t = min(heard)
            elif clip.start <= cap["t"] <= clip.end:
                t = cap["t"]
            elif cap["screenshot_id"] == opening:
                t = clip.start
            else:
                continue
            key = v["word"]
            if key not in cards or t - clip.start < cards[key].t:
                cards[key] = Card(round(t - clip.start, 2), v["word"], v.get("reading"), _short_meaning(v.get("meaning", "")))
    return sorted(cards.values(), key=lambda c: c.t)


CARD_W, CARD_H, CARD_H_NO_READING = 900, 195, 150  # card height with / without a reading line
CARD_STYLE = (
    "Style: Card, Noto Sans JP, 44, &H00DCECF2, &H00DCECF2, &H00000000, &H00000000, "
    "0, 0, 0, 0, 100, 100, 0, 0, 1, 0, 0, 8, 110, 110, 0, 1"
)


def card_events(cards: list[Card], clip_len: float, top: int, *, min_show: float = 2.0) -> list[str]:
    """ASS events: an indigo box with an amber edge, word / reading / meaning in it.

    Each card stays up until the next one (at least ``min_show`` s) or the end.
    """
    x0 = (OUT_W - CARD_W) // 2
    events: list[str] = []
    events_end = 0.0
    for i, c in enumerate(cards):
        start = max(c.t, events_end)
        nxt = cards[i + 1].t if i + 1 < len(cards) else clip_len
        end = max(nxt, start + min_show) if i + 1 < len(cards) else clip_len
        end = min(end, clip_len)
        if end - start < 0.5:
            continue
        events_end = end
        has_reading = bool(c.reading and c.reading != c.word)
        h = CARD_H if has_reading else CARD_H_NO_READING
        box = (f"{{\\an7\\pos({x0},{top})\\p1\\bord0\\shad0\\1c{INDIGO}\\1a&H10&}}"
               f"m 0 0 l {CARD_W} 0 {CARD_W} {h} 0 {h}{{\\p0}}")
        edge = (f"{{\\an7\\pos({x0},{top})\\p1\\bord0\\shad0\\1c{AMBER}}}"
                f"m 0 0 l 12 0 12 {h} 0 {h}{{\\p0}}")
        reading = f"\\N{{\\fnNoto Sans JP\\b1\\fs40\\1c{AMBER}}}{c.reading}" if has_reading else ""
        text = (f"{{\\an8\\pos({OUT_W // 2},{top + 8})\\fnNoto Sans JP\\b900\\fs84\\1c{CREAM}}}{c.word}"
                f"{reading}\\N{{\\fnNoto Sans JP\\b0\\fs36\\1c{CREAM}}}{c.meaning.replace('{', '(').replace('}', ')')}")
        for layer, body in ((1, box), (2, edge), (3, text)):
            events.append(f"Dialogue: {layer},{_ts(start)},{_ts(end)},Card,,0,0,0,,{body}")
    return events


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def measure_loudness(rec: Path, start: float, dur: float, audio_track: int,
                     parts: Optional[list[tuple[float, float]]] = None) -> dict:
    """``parts`` (seconds from ``start``): measure only these kept ranges, joined (a jump-cut Short)."""
    measure = f"loudnorm=I={LOUDNESS['I']}:TP={LOUDNESS['TP']}:LRA={LOUDNESS['LRA']}:print_format=json"
    if parts and len(parts) > 1:
        audio = ["-filter_complex", join_parts(parts, audio_track, video=False) + f";[asrc]{measure}[a]", "-map", "[a]"]
    else:
        audio = ["-map", f"0:a:{audio_track}", "-af", measure]
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(rec),
         *audio, "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S)
    if proc.returncode != 0 or not m:
        raise RuntimeError(f"loudnorm measure failed: {proc.stderr[-400:]}")
    return json.loads(m.group(0))


def loudnorm_filter(measured: dict) -> str:
    """Exact gain to -14 LUFS, then a true-peak-ish limiter at -1.5 dBFS.

    Two-pass linear ``loudnorm`` stops short when the gain would break the
    peak ceiling (c05 on 2026-10-05 came out at -16.8 LUFS), so measure with
    loudnorm, apply the gain ourselves and catch peaks with ``alimiter``,
    oversampled 4x so inter-sample peaks are limited too.
    """
    gain = LOUDNESS["I"] - float(measured["input_i"])
    limit = 10 ** (LOUDNESS["TP"] / 20)
    return (
        f"volume={gain:.2f}dB,aresample=192000,"
        f"alimiter=limit={limit:.4f}:attack=5:release=80:level=false:latency=true,"
        f"aresample=48000"
    )


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def join_parts(parts: list[tuple[float, float]], audio_track: int, *, video: bool = True,
               audio: bool = True) -> str:
    """Filter that trims ``parts`` (seconds into input 0) and joins them into ``[vsrc]`` / ``[asrc]``.

    Jump-cut Shorts (``Clip.cuts``): ``video=False`` for measuring loudness, ``audio=False`` for a still.
    """
    n = len(parts)
    chains, vs, as_ = [], "", ""
    if video:
        chains.append(f"[0:v]split={n}" + "".join(f"[vi{i}]" for i in range(n)))
    if audio:
        chains.append(f"[0:a:{audio_track}]asplit={n}" + "".join(f"[ai{i}]" for i in range(n)))
    for i, (a, b) in enumerate(parts):
        if video:
            chains.append(f"[vi{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[vp{i}]")
            vs += f"[vp{i}]"
        if audio:
            chains.append(f"[ai{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[ap{i}]")
            as_ += f"[ap{i}]"
    if video:
        chains.append(f"{vs}concat=n={n}:v=1:a=0[vsrc]")
    if audio:
        chains.append(f"{as_}concat=n={n}:v=0:a=1[asrc]")
    return ";".join(chains)


def video_filter(layout: Layout, ass_path: Path, fonts_dir: Path, *, badge: bool = False,
                 src: str = "[0:v]") -> str:
    """``badge``: input 1 is the badge PNG, placed at ``layout.badge`` under the captions.
    ``src``: the video to lay out (``[vsrc]`` after ``join_parts`` for a jump-cut Short)."""
    g, c = layout.game, layout.cam
    gx, gy, gw, gh = g.crop
    subs = f"subtitles='{_escape_filter_path(ass_path)}':fontsdir='{_escape_filter_path(fonts_dir)}'"
    if badge:
        spot = layout.badge
        bx = "(W-w)/2" if spot.x is None else str(spot.x)
        subs = f"[1:v]overlay={bx}:{spot.y}[bb];[bb]{subs}"
    if c is None:
        return (
            f"{src}split=2[bsrc][gsrc];"
            f"[bsrc]crop={gw}:{gh}:{gx}:{gy},scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            f"gblur=sigma=10,scale={OUT_W}:{OUT_H},eq=brightness=-0.22:saturation=0.75[bg];"
            f"[gsrc]crop={gw}:{gh}:{gx}:{gy},scale={g.width}:{g.height}[game];"
            f"[bg][game]overlay=({OUT_W}-w)/2:{g.y}[b];"
            f"[b]{subs},format=yuv420p[v]"
        )
    cx, cy, cw, ch = c.crop
    return (
        f"{src}split=3[bsrc][gsrc][csrc];"
        f"[bsrc]crop={gw}:{gh}:{gx}:{gy},scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
        f"gblur=sigma=10,scale={OUT_W}:{OUT_H},eq=brightness=-0.22:saturation=0.75[bg];"
        f"[gsrc]crop={gw}:{gh}:{gx}:{gy},scale={g.width}:{g.height}[game];"
        f"[csrc]crop={cw}:{ch}:{cx}:{cy},scale={c.width}:{c.height}[cam];"
        f"[bg][game]overlay=({OUT_W}-w)/2:{g.y}[a];"
        f"[a][cam]overlay=({OUT_W}-w)/2:{c.y}[b];"
        f"[b]{subs},format=yuv420p[v]"
    )


def short_words(words: list[dict], clip: Clip) -> list[dict]:
    """The clip's words on the Short's clock: words starting inside a cut are dropped."""
    if not clip.cuts:
        return slice_words(words, clip.start, clip.end)
    out = []
    for w in words:
        part = next(((a, b) for a, b in clip.short_parts if a <= w["s"] <= b), None)
        if part:
            out.append({"w": w["w"], "s": round(clip.to_short(w["s"]), 2),
                        "e": round(clip.to_short(min(w["e"], part[1])), 2)})
    return out


def write_ass(clip: Clip, words: list[dict], layout: Layout, session_dir: Path, ass_path: Path) -> int:
    clip_words = apply_caption_fixes(short_words(words, clip), clip.caption_fixes)
    cards = vocab_cards(clip, session_dir / "signals" / "vocab.json") if layout.cards_y is not None else []
    cards = sorted(cards + [Card(c.at - clip.start, c.word, c.reading, c.meaning) for c in clip.cards],
                   key=lambda c: c.t)
    # Card times are seconds from clip.start; move them onto the Short's clock (a card inside a cut
    # shows from the jump).
    cards = [replace(c, t=round(clip.to_short(clip.start + c.t), 2)) for c in cards]
    # Hand-set cards also show in layouts with no card band: in the gap above the game.
    cards_y = layout.cards_y if layout.cards_y is not None else max(0, layout.game.y - CARD_H)
    ass = build_ass(
        clip_words,
        style=CAPTION_STYLE,
        margin_v=OUT_H - layout.captions_bottom,
        extra_styles=(CARD_STYLE,) if cards else (),
        extra_events=tuple(card_events(cards, clip.short_duration, cards_y)) if cards else (),
    )
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(ass, encoding="utf-8")
    return len(cards)


NVENC = ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "20", "-b:v", "0",
         "-maxrate", "24M", "-bufsize", "48M", "-profile:v", "high"]


def render_clip(session: Session, clip: Clip, layout: Layout, words: list[dict], session_dir: Path,
                *, fonts_dir: Path, still_at: Optional[float] = None) -> tuple[Path, int]:
    """Render ``shorts/<id>.mp4`` (or, with ``still_at`` seconds into the clip, a JPEG preview)."""
    out_dir = session_dir / "shorts"
    rec = Path(session.recording)
    # Not next to <id>.mp4: players (VLC, MPC) auto-load a same-name .ass as a subtitle
    # track and draw the captions a second time over the burned-in ones.
    ass_path = out_dir / "_captions" / f"{clip.id}.ass"
    if layout.cam is not None and layout.cam.auto:
        from . import facecam
        _, _, cw, ch = layout.cam.crop
        crop = facecam.auto_cam(rec, clip.start, clip.duration, ch / cw, out_dir / "_captions" / f"{clip.id}.cam.json",
                                region=layout.cam.region or facecam.REGION)
        if crop:
            layout = replace(layout, cam=replace(layout.cam, crop=crop))
    n_cards = write_ass(clip, words, layout, session_dir, ass_path)
    badge_in: list[str] = []
    if clip.badge and layout.badge is not None:
        badge_in = ["-i", str(badge_png.render_badge(out_dir / "_captions" / "badge.png"))]
    # Jump cuts: trim the kept parts out of the input and join them before the layout.
    parts = [(a - clip.start, b - clip.start) for a, b in clip.short_parts] if clip.cuts else None
    vf = video_filter(for_source(layout, probe_video_size(rec)), ass_path, fonts_dir, badge=bool(badge_in),
                      src="[vsrc]" if parts else "[0:v]")
    if parts:
        vf = join_parts(parts, session.audio_track, audio=still_at is None) + ";" + vf

    if still_at is not None:
        target = out_dir / "_preview" / f"{clip.id}_still.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        _run_ffmpeg(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{clip.start:.3f}", "-t", f"{clip.duration:.3f}",
             "-i", str(rec), *badge_in, "-filter_complex", vf, "-map", "[v]", "-ss", f"{still_at:.3f}",
             "-frames:v", "1", "-q:v", "2", "-update", "1", str(target)],
            f"still {clip.id}",
        )
        return target, n_cards

    measured = measure_loudness(rec, clip.start, clip.duration, session.audio_track, parts)
    if parts:
        vf += f";[asrc]{loudnorm_filter(measured)}[aout]"
        audio = ["-map", "[aout]"]
    else:
        audio = ["-map", f"0:a:{session.audio_track}", "-af", loudnorm_filter(measured)]
    target = out_dir / f"{clip.id}.mp4"
    _run_ffmpeg(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{clip.start:.3f}", "-t", f"{clip.duration:.3f}",
         "-i", str(rec), *badge_in, "-filter_complex", vf, "-map", "[v]", *audio, *NVENC,
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(target)],
        f"render {clip.id}",
    )
    return target, n_cards


def preview_frames(short: Path, duration: float, out_dir: Path) -> list[Path]:
    """Frames at 25 % and 75 % of a rendered Short, 540 px wide, for a quick look."""
    return [
        extract_frame(short, duration * f, out_dir / f"{short.stem}_{int(f * 100)}.jpg", width=540)
        for f in (0.25, 0.75)
    ]
