"""Shorts rendering helpers (no ffmpeg)."""
from __future__ import annotations

import json

import pytest

from wabisanji_post.captions import CaptionStyle, build_ass
from wabisanji_post.models import Clip
from wabisanji_post.render import (
    BADGE_Y,
    CAPTION_STYLE,
    BadgeSpot,
    OUT_H,
    card_events,
    load_layouts,
    loudnorm_filter,
    video_filter,
    vocab_cards,
    write_ass,
    Card,
)


def test_layouts_file_is_valid():
    layouts = load_layouts()
    assert {"japanese", "gaming"} <= set(layouts)
    j = layouts["japanese"]
    assert j.cards_y is not None and layouts["gaming"].cards_y is None
    # stacked: game above cam, both inside the frame
    for lay in layouts.values():
        if lay.cam is None:     # Desk cam: game box only
            assert lay.game.y + lay.game.height <= OUT_H
            continue
        assert lay.game.y + lay.game.height <= lay.cam.y
        assert lay.cam.y + lay.cam.height <= OUT_H


def test_layout_crop_outside_source_rejected(tmp_path):
    bad = tmp_path / "layouts.yaml"
    bad.write_text("x:\n  game: {crop: [1800, 0, 400, 300], width: 1080, y: 0}\n"
                   "  cam: {crop: [0, 0, 100, 100], width: 500, y: 900}\n  captions: {bottom: 1500}\n")
    with pytest.raises(ValueError, match="outside 1920x1080"):
        load_layouts(bad)


def clip(**kw) -> Clip:
    base = dict(id="c01", segment=2, start="00:41:49.8", end="00:42:24.6", layout="japanese", title="t")
    base.update(kw)
    return Clip.model_validate(base)


def capture(t, sid, words, seg=2):
    return {"t": t, "screenshot_id": sid, "segment": seg, "vocab": [
        {"word": w, "reading": r, "meaning": m, "heard": [{"t": h} for h in heard]} for w, r, m, heard in words]}


def test_vocab_cards_timing(tmp_path):
    vj = tmp_path / "vocab.json"
    vj.write_text(json.dumps({"captures": [
        capture(2400.0, 1, [("古い", "ふるい", "old", [])]),             # older capture before the clip: ignored
        capture(2504.0, 2, [("竜", "りゅう", "dragon", [])]),            # last capture before the clip: opens it
        capture(2520.0, 3, [("聞く", "きく", "to hear; to listen; to ask", [2530.0])]),  # heard inside the clip
        capture(2522.0, 4, [("いる", "いる", "to be", [])]),             # captured inside the clip
        capture(2530.0, 5, [("砂", "すな", "sand", [])], seg=3),         # other segment
    ]}), encoding="utf-8")
    cards = vocab_cards(clip(), vj)
    assert [(c.word, c.t) for c in cards] == [("竜", 0.0), ("いる", 12.2), ("聞く", 20.2)]


def test_card_events_cover_until_next_and_skip_tiny():
    cards = [Card(0.0, "竜", "りゅう", "dragon"), Card(5.0, "いる", "いる", "to be"),
             Card(5.5, "聞く", "きく", "to hear"), Card(30.0, "来る", "くる", "to come")]
    ev = card_events(cards, clip_len=30.3, top=40)
    starts_ends = sorted({(e.split(",")[1], e.split(",")[2]) for e in ev})
    # 竜 0-5, いる 5-7 (min 2 s), 聞く 7-30, 来る 30.0-30.3 is under 0.5 s: dropped
    assert starts_ends == [("0:00:00.00", "0:00:05.00"), ("0:00:05.00", "0:00:07.00"), ("0:00:07.00", "0:00:30.00")]
    assert len(ev) == 9  # box, amber edge, text per card
    text = [e for e in ev if e.startswith("Dialogue: 3")]
    assert "きく" in text[2] and "\\N" in text[2]
    assert "いる}いる" not in text[1]  # reading line skipped when it equals the word


def test_styled_captions_highlight_each_word():
    words = [{"w": "Ashita.", "s": 0.0, "e": 0.5}, {"w": "That's", "s": 0.8, "e": 1.0}, {"w": "tomorrow.", "s": 1.0, "e": 1.5}]
    ass = build_ass(words, style=CAPTION_STYLE, margin_v=OUT_H - 430)
    assert "Style: CC, Noto Sans JP, 70, &H00DCECF2" in ass
    assert ", 3, 14, 0, 2, 60, 60, 1490, 1" in ass  # opaque box, bottom centre, margin from layout
    events = [ln for ln in ass.splitlines() if ln.startswith("Dialogue")]
    assert len(events) == 3  # "Ashita." ends a sentence: chunk 1 = 1 word, chunk 2 = 2 words
    assert events[1].endswith("{\\c&H004AA0E8}That's{\\c&H00DCECF2} tomorrow.")
    assert events[2].endswith("That's {\\c&H004AA0E8}tomorrow.{\\c&H00DCECF2}")


def test_styled_captions_break_on_long_pause():
    words = [{"w": "Oh", "s": 7.0, "e": 7.4}, {"w": "yeah,", "s": 15.1, "e": 15.4}]
    events = [ln for ln in build_ass(words, style=CAPTION_STYLE).splitlines() if ln.startswith("Dialogue")]
    # two separate chunks: "Oh" ends at 7.48, not held until "yeah," at 15.1
    assert events == [
        "Dialogue: 0,0:00:07.00,0:00:07.48,CC,,0,0,0,,{\\c&H004AA0E8}Oh{\\c&H00DCECF2}",
        "Dialogue: 0,0:00:15.10,0:00:15.48,CC,,0,0,0,,{\\c&H004AA0E8}yeah,{\\c&H00DCECF2}",
    ]


def test_styled_captions_never_overlap_next_chunk():
    # 2026-10-08_2 c06: "nice" ended at 10.33 but "hair" started at 10.25, so libass stacked
    # both chunks for a few frames
    words = [{"w": "mother", "s": 9.59, "e": 9.80}, {"w": "has", "s": 9.81, "e": 10.0},
             {"w": "nice", "s": 10.03, "e": 10.25}, {"w": "hair", "s": 10.25, "e": 10.58}]
    events = [ln for ln in build_ass(words, style=CAPTION_STYLE).splitlines() if ln.startswith("Dialogue")]
    assert events[2].startswith("Dialogue: 0,0:00:10.03,0:00:10.25,")
    assert events[3].startswith("Dialogue: 0,0:00:10.25,")


def test_styled_captions_escape_override_braces():
    ass = build_ass([{"w": "{x}", "s": 0, "e": 1}], style=CaptionStyle(highlight=None, uppercase=False))
    assert ass.rstrip().endswith(",(x)")


def test_legacy_captions_unchanged():
    words = [{"w": "hello", "s": 0.0, "e": 0.4}, {"w": "world.", "s": 0.5, "e": 0.9}]
    ass = build_ass(words, crop_mode="center")
    assert "Style: CC, DM Sans, 82, &H00FFFFFF, &H00FFFFFF, &H00141414, &H64000000, -1, 0, 0, 0, 100, 100, 0, 0, 1, 6, 4, 2, 70, 70, 470, 1" in ass
    assert ass.rstrip().endswith("Dialogue: 0,0:00:00.00,0:00:00.98,CC,,0,0,0,,HELLO WORLD.")


def test_loudness_gain_and_limiter():
    f = loudnorm_filter({"input_i": "-20.5"})
    assert f.startswith("volume=6.50dB,aresample=192000,alimiter=limit=0.8414")
    assert f.endswith("aresample=48000")


def test_badge_png(tmp_path):
    from PIL import Image

    from wabisanji_post.badge import render_badge

    img = Image.open(render_badge(tmp_path / "badge.png"))
    assert img.mode == "RGBA" and img.size == (496, 152)
    assert img.getpixel((0, 0))[3] == 0          # stepped corner is transparent
    assert img.getpixel((img.width // 2, 70))[3] == 255


def test_badge_spots_default_top_centre_off_for_japanese(tmp_path):
    layouts = load_layouts()
    assert layouts["gaming"].badge == BadgeSpot(BADGE_Y)
    assert layouts["japanese"].badge is None and layouts["japanese_43"].badge is None
    custom = tmp_path / "layouts.yaml"
    custom.write_text("x:\n  game: {crop: [0, 0, 400, 300], width: 1080, y: 300}\n"
                      "  captions: {bottom: 1500}\n  badge: {y: 900, x: 40}\n")
    assert load_layouts(custom)["x"].badge == BadgeSpot(900, 40)


def test_video_filter_badge_under_captions(tmp_path):
    lay = load_layouts()["gaming"]
    plain = video_filter(lay, tmp_path / "c.ass", tmp_path)
    with_badge = video_filter(lay, tmp_path / "c.ass", tmp_path, badge=True)
    assert "[1:v]" not in plain
    assert f"[b][1:v]overlay=(W-w)/2:{BADGE_Y}[bb];[bb]subtitles=" in with_badge
    assert clip().badge is True and clip(badge=False).badge is False


def test_hand_set_cards_show_without_a_card_band(tmp_path):
    lay = load_layouts()["gaming_chat_right"]
    assert lay.cards_y is None
    c = clip(layout="gaming_chat_right", cards=[
        {"at": "00:42:00.0", "word": "食べる", "reading": "たべる", "meaning": "to eat (dictionary form)"}])
    ass = tmp_path / "c01.ass"
    n = write_ass(c, [{"w": "Taberu.", "s": 2520.0, "e": 2520.5}], lay, tmp_path, ass)
    text = ass.read_text(encoding="utf-8")
    assert n == 1 and "食べる" in text and "0:00:10.20" in text


def test_hand_set_card_outside_clip_rejected():
    with pytest.raises(ValueError, match="outside the clip"):
        clip(cards=[{"at": "00:50:00", "word": "食べる", "meaning": "to eat"}])


def test_to_source_scales_reference_boxes_to_2k():
    from wabisanji_post.media import to_source

    assert to_source([600, 0, 820, 1080], (1920, 1080)) == (600, 0, 820, 1080)      # 1080p: unchanged
    assert to_source([600, 0, 820, 1080], (2560, 1440)) == (800, 0, 1094, 1440)     # even, full height
    assert to_source([1310, 975, 610, 100], (2560, 1440)) == (1746, 1300, 814, 134)  # a chat blur box
    x, y, w, h = to_source([1919, 1079, 1, 1], (2560, 1440))                        # kept inside the frame
    assert x + w <= 2560 and y + h <= 1440 and w >= 2 and h >= 2


def test_for_source_crops_2k_at_full_resolution(tmp_path):
    from wabisanji_post.render import for_source

    lay = load_layouts()["gaming"]
    assert for_source(lay, (1920, 1080)) is lay
    big = for_source(lay, (2560, 1440))
    gx, gy, gw, gh = lay.game.crop
    assert big.game.crop[2] == round(gw * 4 / 3 / 2) * 2
    assert (big.game.width, big.game.y, big.cam.width, big.cam.y) == (lay.game.width, lay.game.y, lay.cam.width, lay.cam.y)
    vf = video_filter(big, tmp_path / "c.ass", tmp_path)
    bx, by, bw, bh = big.game.crop
    assert f"crop={bw}:{bh}:{bx}:{by},scale={lay.game.width}:" in vf
    desk = for_source(load_layouts()["desk"], (2560, 1440))
    assert desk.cam is None and desk.game.crop == (800, 0, 1094, 1440)


# --- jump cuts (Clip.cuts, Shorts only) ---

CUT = {"start": "00:42:00.0", "end": "00:42:10.0", "reason": "ARIA looking it up"}


def test_clip_cuts_parts_and_clock():
    c = clip(cuts=[CUT])     # 00:41:49.8 to 00:42:24.6, minus 10 s
    assert c.short_parts == [(2509.8, 2520.0), (2530.0, 2544.6)]
    assert c.duration == pytest.approx(34.8) and c.short_duration == pytest.approx(24.8)
    assert c.to_short(2515.0) == pytest.approx(5.2)
    assert c.to_short(2525.0) == pytest.approx(10.2)    # inside the cut: snaps to the jump
    assert c.to_short(2531.0) == pytest.approx(11.2)


def test_clip_cuts_validated():
    with pytest.raises(ValueError, match="inside the clip"):
        clip(cuts=[{"start": "00:42:20", "end": "00:42:30", "reason": "x"}])       # runs past the end
    with pytest.raises(ValueError, match="in order"):
        clip(cuts=[CUT, {"start": "00:42:05", "end": "00:42:12", "reason": "x"}])  # overlaps
    with pytest.raises(ValueError, match="inside a cut"):
        clip(cuts=[CUT], cards=[{"at": "00:42:05", "word": "食べる", "meaning": "to eat"}])


def test_write_ass_jump_cut_drops_and_shifts_words(tmp_path):
    lay = load_layouts()["gaming_chat_right"]
    c = clip(layout="gaming_chat_right", cuts=[CUT],
             cards=[{"at": "00:42:12.0", "word": "食べる", "meaning": "to eat"}])
    words = [{"w": "Before.", "s": 2519.0, "e": 2519.5},
             {"w": "Gone.", "s": 2525.0, "e": 2525.5},     # inside the cut
             {"w": "After.", "s": 2531.0, "e": 2531.5}]
    write_ass(c, words, lay, tmp_path, tmp_path / "c.ass")
    text = (tmp_path / "c.ass").read_text(encoding="utf-8")
    assert "Before" in text and "After" in text and "Gone" not in text
    assert "0:00:11.20" in text      # "After." at 2531 → 11.2 s into the Short
    assert "0:00:12.20" in text      # the card at 00:42:12 → 12.2 s


def test_join_parts_filter(tmp_path):
    from wabisanji_post.render import join_parts

    f = join_parts([(0.0, 10.2), (20.2, 34.8)], 1)
    assert "[0:v]split=2[vi0][vi1]" in f and "[0:a:1]asplit=2[ai0][ai1]" in f
    assert "trim=start=20.200:end=34.800" in f and f.endswith("concat=n=2:v=0:a=1[asrc]")
    assert "[vsrc]" in f
    assert "[asrc]" not in join_parts([(0, 1), (2, 3)], 1, audio=False)
    assert "[vsrc]" not in join_parts([(0, 1), (2, 3)], 1, video=False)
    lay = load_layouts()["gaming"]
    assert "[vsrc]split=3" in video_filter(lay, tmp_path / "c.ass", tmp_path, src="[vsrc]")
