"""Pixel-art "twitch.tv/WabiSanji" badge overlaid on stream Shorts.

The panel and lettering are drawn on a coarse grid (one cell = ``UNIT`` output px)
and scaled up nearest-neighbour, so the edges stay hard. The Twitch Glitch is NOT
pixelated: Twitch's brand rules don't allow altering the mark, so it's drawn at
output resolution from the official SVG geometry, in the one-colour white variant
that Twitch provides for use on a purple background.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

UNIT = 4  # output px per pixel-art cell

TWITCH_PURPLE = (145, 70, 255, 255)   # #9146FF
HIGHLIGHT = (172, 122, 255, 255)
SHADE = (112, 48, 214, 255)
OUTLINE = (36, 18, 79, 255)
DROP = (16, 10, 36, 150)
TEXT_SHADOW = (43, 26, 92, 255)
WHITE = (255, 255, 255, 255)
LAVENDER = (230, 220, 255, 255)

# 9-row glyphs: rows 0-6 sit on the baseline, rows 7-8 are the descender (only j uses them).
_GLYPHS = {
    "W": ["#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"],
    "S": [".###.", "#...#", "#....", ".###.", "....#", "#...#", ".###."],
    "a": [".....", ".....", ".###.", "....#", ".####", "#...#", ".####"],
    "b": ["#....", "#....", "####.", "#...#", "#...#", "#...#", "####."],
    "c": ["....", "....", ".###", "#...", "#...", "#...", ".###"],
    "h": ["#....", "#....", "####.", "#...#", "#...#", "#...#", "#...#"],
    "i": ["#", ".", "#", "#", "#", "#", "#"],
    "j": ["..#", "...", "..#", "..#", "..#", "..#", "..#", "#.#", ".#."],
    "n": [".....", ".....", "####.", "#...#", "#...#", "#...#", "#...#"],
    "t": [".#..", ".#..", "####", ".#..", ".#..", ".#..", "..##"],
    "v": [".....", ".....", "#...#", "#...#", "#...#", ".#.#.", "..#.."],
    "w": [".....", ".....", "#...#", "#...#", "#.#.#", "#.#.#", ".#.#."],
    ".": [".", ".", ".", ".", ".", ".", "#"],
    "/": ["..#", "..#", ".#.", ".#.", ".#.", "#..", "#.."],
}
_ROWS = 9

# Official Glitch (viewBox 0 0 2400 2800): outer bubble with the inner one cut out, and two eyes.
_GLITCH_W, _GLITCH_H = 2400, 2800
_GLITCH_OUTER = [(500, 0), (0, 500), (0, 2300), (600, 2300), (600, 2800), (1100, 2300), (1500, 2300),
                 (2400, 1400), (2400, 0)]
_GLITCH_INNER = [(2200, 1300), (1800, 1700), (1400, 1700), (1050, 2050), (1050, 1700), (600, 1700),
                 (600, 200), (2200, 200)]
_GLITCH_EYES = [(1150, 550, 1350, 1150), (1700, 550, 1900, 1150)]


def _text_width(text: str) -> int:
    return sum(len(_GLYPHS[ch][0]) for ch in text) + len(text) - 1


def _draw_text(img: Image.Image, text: str, x: int, y: int, scale: int, colour, shadow) -> None:
    """Cells on the grid; ``scale`` doubles the glyphs (still on the same grid)."""
    px = img.load()
    for dx, dy, col in ((1, 1, shadow), (0, 0, colour)):
        cx = x
        for ch in text:
            g = _GLYPHS[ch]
            for r, row in enumerate(g):
                for c, bit in enumerate(row):
                    if bit != "#":
                        continue
                    for sy in range(scale):
                        for sx in range(scale):
                            px[cx + c * scale + sx + dx, y + r * scale + sy + dy] = col
            cx += (len(g[0]) + 1) * scale


def _panel(w: int, h: int) -> Image.Image:
    """Bevelled panel with stepped corners, plus a drop shadow 2 cells down-right."""
    def inside(x: int, y: int) -> bool:
        if not (0 <= x < w and 0 <= y < h):
            return False
        ex, ey = min(x, w - 1 - x), min(y, h - 1 - y)
        return ex + ey >= 2   # trims a 3-cell staircase off each corner

    img = Image.new("RGBA", (w + 2, h + 2), (0, 0, 0, 0))
    px = img.load()
    for y in range(h):
        for x in range(w):
            if inside(x, y):
                px[x + 2, y + 2] = DROP
    for y in range(h):
        for x in range(w):
            if not inside(x, y):
                continue
            edge = not all(inside(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
            if edge:
                px[x, y] = OUTLINE
            elif not inside(x, y - 2) or not inside(x - 2, y):
                px[x, y] = HIGHLIGHT
            elif not inside(x, y + 2) or not inside(x + 2, y):
                px[x, y] = SHADE
            else:
                px[x, y] = TWITCH_PURPLE
    return img


def _glitch(height: int) -> Image.Image:
    """White one-colour Glitch, ``height`` px tall, supersampled for clean 45° edges."""
    ss = 4
    k = height * ss / _GLITCH_H
    big = Image.new("RGBA", (round(_GLITCH_W * k), height * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.polygon([(x * k, y * k) for x, y in _GLITCH_OUTER], fill=WHITE)
    d.polygon([(x * k, y * k) for x, y in _GLITCH_INNER], fill=(0, 0, 0, 0))
    for x0, y0, x1, y1 in _GLITCH_EYES:
        d.rectangle([x0 * k, y0 * k, x1 * k - 1, y1 * k - 1], fill=WHITE)
    return big.resize((round(_GLITCH_W * height / _GLITCH_H), height), Image.LANCZOS)


def render_badge(target: Path, *, line1: str = "twitch.tv/", line2: str = "WabiSanji") -> Path:
    """Write the badge PNG (RGBA) to ``target`` and return it. About 490x150 px at UNIT 4."""
    pad, gap, glitch_cells = 4, 4, 26
    text_w = max(_text_width(line1), 2 * _text_width(line2))
    text_h = 7 + 3 + 2 * _ROWS
    glitch_px = glitch_cells * UNIT
    glitch_w_cells = -(-round(_GLITCH_W * glitch_px / _GLITCH_H) // UNIT)
    w = pad + glitch_w_cells + gap + text_w + pad + 1
    h = pad + max(text_h, glitch_cells) + pad

    grid = _panel(w, h)
    tx = pad + glitch_w_cells + gap
    ty = pad
    _draw_text(grid, line1, tx, ty, 1, LAVENDER, TEXT_SHADOW)
    _draw_text(grid, line2, tx, ty + 7 + 3, 2, WHITE, TEXT_SHADOW)

    img = grid.resize((grid.width * UNIT, grid.height * UNIT), Image.NEAREST)
    glitch = _glitch(glitch_px)
    gy = (pad + (h - 2 * pad - glitch_cells) // 2) * UNIT
    img.alpha_composite(glitch, (pad * UNIT, gy))
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target)
    return target
