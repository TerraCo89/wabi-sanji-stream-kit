"""Burned-in caption text for Shorts: slice a transcript, apply fixes, build ASS.

A word is a plain dict so it serialises straight to JSON: ``{"w", "s", "e"}`` (text, start,
end in seconds). Words keep their original casing and trailing punctuation so captions can
break on sentence ends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

Word = dict


def slice_words(words: list[Word], start: float, end: float) -> list[Word]:
    """Words whose start falls in [start, end], rebased so the clip begins at 0."""
    return [
        {"w": w["w"], "s": round(w["s"] - start, 2), "e": round(w["e"] - start, 2)}
        for w in words
        if start <= w["s"] <= end
    ]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def apply_caption_fixes(words: list[Word], fixes: list[tuple[str, str]]) -> list[Word]:
    """Replace word subsequences (find→replace), redistributing timing evenly.

    Corrects Whisper mishears without touching per-word timestamps. Matching is
    punctuation/case-insensitive on the joined token text; the replacement tokens split the
    matched span's time evenly.
    """
    for old, new in fixes:
        ow = old.split()
        n = len(ow)
        i = 0
        while n and i <= len(words) - n:
            seg = words[i:i + n]
            if _norm(" ".join(x["w"] for x in seg)) == _norm(old):
                s, e = seg[0]["s"], seg[-1]["e"]
                nw = new.split()
                step = (e - s) / len(nw)
                repl = [
                    {"w": nw[k], "s": round(s + k * step, 2), "e": round(s + (k + 1) * step, 2)}
                    for k in range(len(nw))
                ]
                tail = "".join(c for c in seg[-1]["w"] if c in ".,!?")
                if tail and not repl[-1]["w"].endswith(tuple(".,!?")):
                    repl[-1]["w"] += tail
                words = words[:i] + repl + words[i + n:]
                i += len(nw)
            else:
                i += 1
    return words


def _tidy(t: str) -> str:
    t = re.sub(r"\s+([.,!?%])", r"\1", t)
    t = re.sub(r"([.,!?])[.,!?]+", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


def _chunk(words: list[Word], max_words: int = 3, max_dur: float = 1.5,
           max_gap: Optional[float] = None) -> list[list[Word]]:
    """2-3 word caption chunks. ``max_gap`` also breaks before a pause that long."""
    out: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if max_gap is not None and cur and w["s"] - cur[-1]["e"] > max_gap:
            out.append(cur)
            cur = []
        cur.append(w)
        dur = cur[-1]["e"] - cur[0]["s"]
        ends_sentence = bool(re.search(r"[.!?]$", w["w"]))
        if len(cur) >= max_words or dur >= max_dur or ends_sentence:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def ffmpeg_escape_text(text: str) -> str:
    """Escape text for ffmpeg drawtext's single-quoted text= argument.

    Apostrophes inside single-quoted filter values break the parser, so they become U+2019.
    Colons end the drawtext option even inside the quoted value, and backslashes must be
    doubled ahead of both.
    """
    return text.replace("\\", "\\\\").replace("'", "’").replace(":", "\\:")


# MarginV by crop mode (used when no CaptionStyle is given).
_MARGIN_V = {"center": 470, "letterbox": 270}

_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CC, DM Sans, 82, &H00FFFFFF, &H00FFFFFF, &H00141414, &H64000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 6, 4, 2, 70, 70, {margin_v}, 1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ts(sec: float) -> str:
    cs = int(round(max(0.0, sec) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


@dataclass(frozen=True)
class CaptionStyle:
    """Caption look for :func:`build_ass`. Colours are ASS ``&HAABBGGRR``.

    ``border_style`` 1 = outline + shadow, 3 = opaque box in ``back``.
    ``highlight`` colours the word being spoken (one event per word).
    """

    font: str = "DM Sans"
    size: int = 82
    primary: str = "&H00FFFFFF"
    outline_colour: str = "&H00141414"
    back: str = "&H64000000"
    bold: bool = True
    border_style: int = 1
    outline: float = 6
    shadow: float = 4
    margin_lr: int = 70
    highlight: Optional[str] = None
    uppercase: bool = True
    max_words: int = 3


def _ass_text(text: str) -> str:
    """Make plain text safe inside an ASS Dialogue (no override blocks, no escapes)."""
    return text.replace("\\", "/").replace("{", "(").replace("}", ")")


def build_ass(
    words: list[Word],
    *,
    crop_mode: str = "letterbox",
    uppercase: bool = True,
    style: Optional[CaptionStyle] = None,
    margin_v: Optional[int] = None,
    extra_styles: tuple[str, ...] = (),
    extra_events: tuple[str, ...] = (),
) -> str:
    """Karaoke-style ASS: 2-3 word chunks in the lower band.

    With ``style``, the look, ``margin_v`` and per-word highlighting come from the caller,
    and ``extra_styles`` / ``extra_events`` add further ASS lines (e.g. vocab word cards).
    Without it: white text on a black outline.
    """
    if style is None:
        mv = _MARGIN_V.get(crop_mode, _MARGIN_V["letterbox"]) if margin_v is None else margin_v
        lines = [_ASS_HEADER.format(margin_v=mv)]
        for group in _chunk(words):
            text = _tidy(" ".join(x["w"] for x in group))
            text = ffmpeg_escape_text(text).replace("{", "(").replace("}", ")")
            if uppercase:
                text = text.upper()
            start, end = group[0]["s"], group[-1]["e"] + 0.08
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},CC,,0,0,0,,{text}")
        return "\n".join(lines) + "\n"

    s = style
    mv = 270 if margin_v is None else margin_v
    header = _ASS_HEADER.format(margin_v=mv).replace(
        "Style: CC, DM Sans, 82, &H00FFFFFF, &H00FFFFFF, &H00141414, &H64000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 6, 4, 2, 70, 70,",
        f"Style: CC, {s.font}, {s.size}, {s.primary}, {s.primary}, {s.outline_colour}, {s.back}, "
        f"{-1 if s.bold else 0}, 0, 0, 0, 100, 100, 0, 0, {s.border_style}, {s.outline:g}, {s.shadow:g}, 2, "
        f"{s.margin_lr}, {s.margin_lr},",
    )
    if extra_styles:
        header = header.replace("\n\n[Events]", "\n" + "\n".join(extra_styles) + "\n\n[Events]")
    lines = [header]
    # Break on pauses over 1 s: otherwise a word before a long gap stays highlighted
    # until the next one.
    groups = list(_chunk(words, max_words=s.max_words, max_gap=1.0))
    for gi, group in enumerate(groups):
        texts = [_ass_text(x["w"].upper() if s.uppercase else x["w"]) for x in group]
        end = group[-1]["e"] + 0.08
        # Whisper word times can overlap the next chunk's first word: stop there, or libass
        # stacks both chunks for a few frames (two lines, newest on top).
        if gi + 1 < len(groups):
            end = min(end, groups[gi + 1][0]["s"])
        if not s.highlight:
            lines.append(f"Dialogue: 0,{_ts(group[0]['s'])},{_ts(end)},CC,,0,0,0,,{_tidy(' '.join(texts))}")
            continue
        for i, w in enumerate(group):
            seg_end = group[i + 1]["s"] if i + 1 < len(group) else end
            parts = [
                f"{{\\c{s.highlight}}}{t}{{\\c{s.primary}}}" if j == i else t
                for j, t in enumerate(texts)
            ]
            start = group[0]["s"] if i == 0 else w["s"]
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(seg_end)},CC,,0,0,0,,{' '.join(parts)}")
    lines.extend(extra_events)
    return "\n".join(lines) + "\n"
