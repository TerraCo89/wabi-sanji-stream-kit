"""Episode thumbnails (stream/thumbnail.py): text choice, frame scoring, layout pick, rendering."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from PIL import Image, ImageDraw

from wabisanji_post import thumbnail as th
from wabisanji_post.models import Segment, Session
from wabisanji_post.render import load_layouts


def seg(type_="japanese", **kw) -> Segment:
    base = {"id": 2, "type": type_, "game": "Breath of Fire IV (JP)",
            "series": "Learning Japanese with Breath of Fire IV", "start": "00:10:00", "end": "01:00:00"}
    return Segment.model_validate({**base, **kw})


def write_vocab(session_dir: Path, captures: list[dict]) -> None:
    (session_dir / "signals").mkdir(parents=True, exist_ok=True)
    (session_dir / "signals" / "vocab.json").write_text(json.dumps({"captures": captures}), encoding="utf-8")


def test_thumb_block_wins(tmp_path):
    meta = {"hook": "Fou-lu's general wants him asleep",
            "thumb": {"mode": "word", "text": "殺気", "reading": "さっき", "sub": "KILLING INTENT"}}
    t = th.thumb_text(meta, seg(), tmp_path, japanese=True)
    assert (t.mode, t.text, t.reading, t.sub) == ("word", "殺気", "さっき", "KILLING INTENT")


def test_japanese_fallback_is_the_most_heard_word(tmp_path):
    write_vocab(tmp_path, [
        {"t": 700, "segment": 2, "vocab": [
            {"word": "時代", "reading": "じだい", "meaning": "era; age", "heard": [{"t": 710}]},
            {"word": "フォウル", "reading": "フォウル", "meaning": "Fou-lu", "category": "pokemon",
             "heard": [{"t": 1}, {"t": 2}, {"t": 3}]},
            {"word": "殺気", "reading": "さっき", "meaning": "killing intent; bloodlust",
             "heard": [{"t": 720}, {"t": 730}]}]},
        {"t": 9000, "segment": 5, "vocab": [{"word": "外", "reading": "そと", "meaning": "outside",
                                             "heard": [{"t": 1}] * 9}]},   # outside the segment
    ])
    t = th.thumb_text({}, seg(), tmp_path, japanese=True)
    assert (t.mode, t.text, t.reading, t.sub) == ("word", "殺気", "さっき", "KILLING INTENT")


def test_hook_fallback_drops_a_dangling_word(tmp_path):
    s = seg("gaming", game="Alchemy Factory", series="Alchemy Factory")
    assert th.thumb_text({"hook": "Found 20,000 silver I didn't know"}, s, tmp_path, False).text == "FOUND 20,000 SILVER"
    assert th.thumb_text({"hook": "Podracer tests and Kestar Bool"}, s, tmp_path, False).text == "PODRACER TESTS AND KESTAR"
    assert th.thumb_text({"hook": "Tests and more"}, s, tmp_path, False).text == "TESTS AND MORE"
    assert th.thumb_text({}, s, tmp_path, False).text == "ALCHEMY FACTORY"


def test_badge_label():
    series = {"numbering": "locked", "episodes": [{"session": "2026-10-14", "segment": 2, "episode": 3}]}
    assert th.badge_label(seg(), series, "2026-10-14") == "BREATH OF FIRE IV · EP 3"
    assert th.badge_label(seg(), {**series, "numbering": "open"}, "2026-10-14") == "BREATH OF FIRE IV"
    assert th.badge_label(seg(), {**series, "thumb_label": "BoF IV"}, "2026-10-14") == "BOF IV · EP 3"
    assert th.badge_label(seg("coding", game=None, series="Rough Edges"), {}, "2026-10-14") == "ROUGH EDGES"


def test_game_ranking_skips_black_and_flat_frames():
    black = Image.new("RGB", (640, 360), (3, 3, 3))
    flat = Image.new("RGB", (640, 360), (120, 120, 120))
    busy = Image.new("RGB", (640, 360))
    d = ImageDraw.Draw(busy)
    for i in range(0, 640, 20):
        d.rectangle((i, 0, i + 10, 360), fill=(230, 60 + i % 150, 40))
    stats = {10.0: th.game_stats(black), 20.0: th.game_stats(flat), 30.0: th.game_stats(busy)}
    assert not th.usable_game(stats[10.0]) and not th.usable_game(stats[20.0])
    assert th.rank_games(stats, []) == [30.0]


def test_face_ranking_prefers_loud_moments_and_drops_outliers():
    faces = {100.0: (1550, 830, 100), 200.0: (1560, 835, 110), 300.0: (1545, 840, 105), 400.0: (900, 430, 140)}
    kept = th.consistent_faces(faces)
    assert 400.0 not in kept
    assert th.rank_faces(kept, [(15.0, 200.5), (5.0, 100.0)])[0] == 200.0


def test_trim_bars():
    img = Image.new("RGB", (1920, 1080), (0, 0, 0))
    img.paste(Image.new("RGB", (1920, 800), (90, 140, 200)), (0, 140))
    assert th.trim_bars(img).size == (1920, 800)


def test_layout_from_clips_then_type(tmp_path):
    layouts = load_layouts()
    assert th.pick_layout(tmp_path, seg(), layouts) == "japanese_43"
    assert th.pick_layout(tmp_path, seg("talk", game=None), layouts) is None
    (tmp_path / "clips.yaml").write_text(
        "session: '2026-10-14'\nclips:\n"
        + "".join(f"  - {{id: c0{i}, segment: 2, start: {700 + i * 100}, end: {730 + i * 100}, layout: {lay}, title: t}}\n"
                  for i, lay in enumerate(["japanese", "japanese", "japanese_43", "anki_center"])),
        encoding="utf-8")
    assert th.pick_layout(tmp_path, seg(), layouts) == "japanese"


def test_layouts_have_thumb_crops_inside_the_frame():
    for key, lay in load_layouts().items():
        if lay.thumb:
            x, y, w, h = lay.thumb
            assert x >= 0 and y >= 0 and x + w <= 1920 and y + h <= 1080, key


def _person(w: int = 300, h: int = 420) -> Image.Image:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((w * 0.3, 0, w * 0.7, h * 0.35), fill=(220, 180, 150, 255))
    d.rectangle((0, h * 0.35, w, h), fill=(230, 120, 40, 255))
    return img


def test_render_word_and_hook():
    game = Image.new("RGB", (1920, 1080), (40, 90, 60))
    for mode, text in (("word", "まみえる"), ("hook", "20,000 *SILVER*?!")):
        img = th.render_thumb(game, _person(), th.ThumbText(mode, text, "まみえる" if mode == "word" else None,
                                                             "TO MEET" if mode == "word" else None),
                              "BREATH OF FIRE IV · EP 3", crop=(395, 87, 1102, 826))
        assert img.size == (1280, 720) and img.mode == "RGB"
    img = th.render_thumb(game, None, th.ThumbText("hook", "A VERY LONG HOOK THAT HAS TO WRAP SOMEHOW"), "")
    assert img.size == (1280, 720)


def test_build_episode_thumbs_end_to_end(tmp_path):
    """Synthetic candidates, a stubbed cutout (no rembg): pick1-3 under 2 MB, then up to date."""
    session = Session.model_validate({
        "session": "2026-10-14", "recording": str(tmp_path / "missing.mkv"), "duration": 4000,
        "recording_start": dt.datetime(2026, 10, 14, 19, tzinfo=dt.timezone(dt.timedelta(hours=11))),
        "segments": [seg().model_dump(mode="json")],
    })
    cand = tmp_path / "vods" / "seg2_thumbs" / "candidates"
    cand.mkdir(parents=True)
    for i, t in enumerate((700.0, 1200.5, 2400.0, 3000.0)):
        img = Image.new("RGB", (1920, 1080), (20 + 50 * i, 120, 200 - 40 * i))
        ImageDraw.Draw(img).rectangle((500, 200, 900 + 100 * i, 600), fill=(240, 220, 60))
        img.save(cand / f"f_{int(t):05d}{'.5' if t % 1 else ''}.jpg")
    (tmp_path / "vods" / "seg2.meta.json").write_text(json.dumps(
        {"hook": "x", "thumb": {"mode": "word", "text": "殺気", "reading": "さっき", "sub": "KILLING INTENT"}}),
        encoding="utf-8")
    calls = []

    def fake_cutout(img: Image.Image) -> Image.Image:
        calls.append(img.size)
        return _person(img.width, img.height)

    out = th.build_episode_thumbs(session, session.segment(2), tmp_path, cutout=fake_cutout, say=lambda m: None)
    assert [p.name for p in out] == ["pick1.jpg", "pick2.jpg", "pick3.jpg"]
    assert all(p.stat().st_size < th.MAX_BYTES for p in out)
    with Image.open(out[0]) as im:
        assert im.size == (1280, 720)
    stamp = json.loads((tmp_path / "vods" / "seg2_thumbs" / "thumb.json").read_text(encoding="utf-8"))
    assert stamp["layout"] == "japanese_43" and stamp["face_guessed"] and calls   # no face in the fakes: cam box used
    assert th.build_episode_thumbs(session, session.segment(2), tmp_path, cutout=fake_cutout, say=lambda m: None) == []


def test_pushed_marker(tmp_path):
    pick = tmp_path / "pick1.jpg"
    Image.new("RGB", (64, 36), (1, 2, 3)).save(pick)
    assert not th.is_pushed(pick, "abc")
    th.mark_pushed(pick, "abc")
    assert th.is_pushed(pick, "abc") and not th.is_pushed(pick, "other")
    Image.new("RGB", (64, 36), (200, 2, 3)).save(pick)
    assert not th.is_pushed(pick, "abc")


def test_hide_boxes_blur_the_chat():
    frame = Image.new("RGB", (1920, 1080), (30, 30, 30))
    d = ImageDraw.Draw(frame)
    for y in range(500, 1000, 24):
        d.text((20, y), "viewer: some chat line", fill=(255, 255, 255))
    out = th.hide_boxes(frame, [(0, 420, 640, 660)])
    before = th.game_stats(frame.crop((0, 420, 640, 1080)))[3]
    after = th.game_stats(out.crop((0, 420, 640, 1080)))[3]
    assert after < before / 20   # sharpness gone: the text can't be read
    assert out.getpixel((1500, 200)) == frame.getpixel((1500, 200))
    assert load_layouts()["gaming"].thumb_hide
