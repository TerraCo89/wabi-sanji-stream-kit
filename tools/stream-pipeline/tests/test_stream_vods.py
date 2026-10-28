"""Segment VOD helpers (no ffmpeg)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wabisanji_post.models import Segment
from wabisanji_post.vods import (
    episode_label,
    load_series,
    slugify,
    thumb_times,
    vod_path,
    youtube_chapters,
)


def seg2() -> Segment:
    return Segment.model_validate({
        "id": 2, "type": "japanese", "game": "Breath of Fire IV (JP)",
        "series": "Learning Japanese with Breath of Fire IV", "start": "00:15:00", "end": "00:51:50",
        "chapters": [{"at": "00:15:00", "title": "Setup"}, {"at": "00:22:20", "title": "Title screen"},
                     {"at": "00:32:00", "title": "Gameplay"}],
    })


def test_chapters_relative_to_keyframe_start():
    # file starts 0.383 s before the segment start; first chapter is always 0:00
    lines = youtube_chapters(seg2(), 899.617, [(2504.0, "A dragon attacks")])
    assert lines == ["0:00 Setup", "7:20 Title screen", "17:00 Gameplay", "26:44 A dragon attacks"]


def test_chapters_over_an_hour():
    s = Segment.model_validate({"id": 3, "type": "gaming", "game": "AF", "series": "AF",
                                "start": "00:52:00", "end": "02:26:20",
                                "chapters": [{"at": "00:52:00", "title": "Tour"}]})
    assert youtube_chapters(s, 3119.617, [(8667.0, "Raid")]) == ["0:00 Tour", "1:32:27 Raid"]


def test_episode_label_open_and_locked():
    open_ = {"numbering": "open", "episodes": [{"session": "2026-10-05", "segment": 2, "episode": 4}]}
    assert episode_label(open_, "2026-10-05", 2) == "#?"
    locked = {**open_, "numbering": "locked"}
    assert episode_label(locked, "2026-10-05", 2) == "#4"
    assert episode_label(locked, "2026-10-05", 3) == "#?"


def test_series_registry_has_the_2026_10_05_series():
    series = load_series()
    assert {"Learning Japanese with Breath of Fire IV", "Alchemy Factory"} <= set(series)


def test_vod_path_slug(tmp_path):
    assert slugify("Learning Japanese with Breath of Fire IV") == "learning-japanese-with-breath-of-fire-iv"
    assert vod_path(tmp_path, seg2()).name == "seg2_learning-japanese-with-breath-of-fire-iv.mp4"


def test_thumb_times_prefer_signals_and_stay_apart(tmp_path):
    (tmp_path / "signals").mkdir()
    (tmp_path / "signals" / "loudness.json").write_text(json.dumps({"segments": [
        {"segment": 2, "peaks": [{"t": 3019.9, "above_median": 15.2}, {"t": 3030.0, "above_median": 14.0},
                                 {"t": 2460.9, "above_median": 14.6}]},
        {"segment": 3, "peaks": [{"t": 6569.3, "above_median": 20.5}]},
    ]}), encoding="utf-8")
    (tmp_path / "signals" / "vocab.json").write_text(json.dumps({"captures": [
        {"segment": 2, "vocab": [{"heard": [{"t": 2103.5}]}]}]}), encoding="utf-8")
    times = thumb_times(seg2(), tmp_path, count=6, spacing=45)
    assert 3020.4 in times and 2461.4 in times and 2103.8 in times
    assert 3030.5 not in times  # within 45 s of a louder peak
    assert all(900 <= t <= 3110 for t in times) and len(times) == 6
    assert all(b - a >= 45 for a, b in zip(times, times[1:]))


def test_chapters_through_a_multi_part_file():
    from wabisanji_post.vods import file_time

    seg = Segment.model_validate({
        "id": 1, "type": "gaming", "game": "AF", "series": "AF", "start": "00:12:20", "end": "05:06:30",
        "cuts": [{"start": "03:29:20", "end": "03:47:30", "reason": "BRB"}],
        "chapters": [{"at": "00:12:20", "title": "Start"}, {"at": "04:00:00", "title": "After the break"}],
    })
    cut = {"start": 739.6, "parts": [
        {"rec_start": 739.6, "rec_end": 12560.0, "file_start": 0.0},
        {"rec_start": 13651.2, "rec_end": 18390.0, "file_start": 11820.4},
    ]}
    assert file_time(cut, 13000) is None  # in the BRB
    # 04:00:00 = 14400 s → 11820.4 + (14400 - 13651.2) = 12569.2 → 3:29:29
    assert youtube_chapters(seg, cut) == ["0:00 Start", "3:29:29 After the break"]


def test_thumb_times_skip_cuts(tmp_path):
    seg = Segment.model_validate({"id": 1, "type": "gaming", "game": "AF", "series": "AF",
                                  "start": "00:00:00", "end": "01:00:00",
                                  "cuts": [{"start": "00:10:00", "end": "00:50:00", "reason": "BRB"}]})
    times = thumb_times(seg, tmp_path, count=6, spacing=60)
    assert times and all(seg.contains(t) for t in times)


def test_cut_segment_joins_parts_on_real_video(tmp_path):
    """60 s test video, keyframe every 2 s; cut 0:20-0:40 out of a 0:05-0:55 segment."""
    import datetime as dt
    import json as _json
    import subprocess

    from wabisanji_post.media import probe_duration
    from wabisanji_post.models import Session
    from wabisanji_post.vods import cut_segment

    rec = tmp_path / "rec.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=64x36:r=10",
                    "-f", "lavfi", "-i", "sine=f=440:r=48000", "-t", "60", "-c:v", "libx264", "-g", "20",
                    "-c:a", "aac", str(rec)], check=True)
    session = Session.model_validate({
        "session": "2026-09-10", "recording": str(rec), "duration": 60,
        "recording_start": dt.datetime(2026, 9, 10, 8, 41, tzinfo=dt.timezone(dt.timedelta(hours=10))),
        "segments": [{"id": 1, "type": "gaming", "game": "AF", "series": "AF", "start": 5, "end": 55,
                      "cuts": [{"start": 20, "end": 40, "reason": "BRB"}]}],
    })
    info = cut_segment(session, session.segment(1), tmp_path)
    out = tmp_path / "vods" / info["file"]
    assert out.exists() and not (tmp_path / "vods" / "_parts_seg1").exists()
    assert [p["rec_start"] for p in info["parts"]] == [4.0, 40.0]  # keyframe before 5, keyframe at/after 40
    assert abs(probe_duration(out) - (16 + 15)) < 1.0  # 4-20 plus 40-55 (+ ~0.2 s container padding per part)
    # each part starts exactly on its keyframe (seeking to the keyframe itself lands one early)
    import numpy as np
    def frame(src, extra=()):
        raw = subprocess.run(["ffmpeg", "-v", "error", *extra, "-i", str(src), "-frames:v", "1", "-f", "rawvideo",
                              "-pix_fmt", "gray", "-"], capture_output=True).stdout
        return np.frombuffer(raw, np.uint8).astype(float)
    assert np.array_equal(frame(out), frame(rec, ["-ss", "4"]))
    # and cut.json maps recording time to file time correctly (what chapters rely on):
    # 1 s into part 2 by the mapping is the recording at 41 s
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(rec), "-vf", "fps=10", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True).stdout
    allf = np.frombuffer(raw, np.uint8).reshape(-1, 36 * 64).astype(float)
    at = info["parts"][1]["file_start"] + 1.0
    got = frame(out, ["-ss", f"{at:.3f}"])
    best = int(((allf - got) ** 2).mean(axis=1).argmin()) / 10
    assert abs(best - 41.0) <= 0.15, best
    assert _json.loads((tmp_path / "vods" / "seg1.cut.json").read_text())["cuts"][0]["reason"] == "BRB"


def test_build_youtube_md(tmp_path, monkeypatch):
    import json as _json

    import yaml as _yaml

    from wabisanji_post import vods
    from wabisanji_post.models import Session

    from test_stream_models import APPROVED

    data = _yaml.safe_load(APPROVED)
    data["segments"][3]["cuts"] = [{"start": "01:00:00", "end": "01:10:00", "reason": "BRB"}]
    session = Session.model_validate(data)
    registry = {
        "Alchemy Factory": {"name": "Alchemy Factory", "playlist_id": "PLU_VXeoYFHXE", "numbering": "locked",
                            "episodes": [{"session": "2026-10-05", "segment": 3, "episode": 16}]},
        "Learning Japanese with Breath of Fire IV": {"name": "x", "playlist_id": None, "numbering": "open", "episodes": []},
    }
    monkeypatch.setattr(vods, "load_series", lambda *a: registry)
    (tmp_path / "vods").mkdir()
    (tmp_path / "vods" / "seg3.cut.json").write_text(_json.dumps({"file": "seg3_alchemy-factory.mp4", "duration": 5060.0,
        "start": 3119.6, "cuts": [{"start": 3600, "end": 4200, "reason": "BRB"}],
        "parts": [{"rec_start": 3119.6, "rec_end": 3600, "file_start": 0}, {"rec_start": 4200.4, "rec_end": 8780, "file_start": 480.6}]}))
    (tmp_path / "vods" / "seg3.meta.json").write_text(_json.dumps({"hook": "Touring a viewer's factory",
        "about": ["I tour a factory."], "tags_extra": ["factory tour"], "check": "",
        "chapters": [{"at": "00:52:00", "title": "Tour"}, {"at": "00:58:00", "title": "Back home"},
                     {"at": "01:15:00", "title": "Steel"}]}))
    md = vods.build_youtube_md(session, session.segment(3), tmp_path).read_text(encoding="utf-8")
    assert "Alchemy Factory #16 | Touring a viewer's factory" in md
    assert "0:00 Tour\n6:00 Back home\n13:00 Steel" in md  # 01:15:00 = 4500 s → 480.6 + 299.6 = 780.2
    assert "More episodes: https://www.youtube.com/playlist?list=PLU_VXeoYFHXE" in md
    assert "#wabisanji #alchemyfactory" in md and "factory tour" in md
    assert "Cut out of this episode: 1:00:00-1:10:00 BRB." in md
    # Japanese segment: open numbering, word list from signals/vocab.json, no playlist yet
    (tmp_path / "signals").mkdir()
    (tmp_path / "signals" / "vocab.json").write_text(_json.dumps({"captures": [{"t": 2487.0, "vocab": [
        {"word": "明日", "reading": "あした", "meaning": "tomorrow; the next day", "category": "speech"}]}]}), encoding="utf-8")
    (tmp_path / "vods" / "seg2.cut.json").write_text(_json.dumps({"file": "seg2.mp4", "duration": 2212.0, "start": 899.6}))
    (tmp_path / "vods" / "seg2.meta.json").write_text(_json.dumps({"hook": "A dragon in the desert", "about": ["x"], "chapters": []}))
    md2 = vods.build_youtube_md(session, session.segment(2), tmp_path).read_text(encoding="utf-8")
    assert "Learning Japanese with Breath of Fire IV #? | A dragon in the desert" in md2
    assert "don't publish yet" in md2 and "Words from this episode (1)\n明日 (あした) tomorrow" in md2
    assert "#learnjapanese" in md2 and "no playlist yet" in md2
    # session chapters are used when meta has none (2026-10-05 seg2 has 4) → shown
    assert "0:00 Setting up the wabisanji parser" in md2
    long = {"hook": "x" * 80, "about": [], "chapters": []}
    (tmp_path / "vods" / "seg2.meta.json").write_text(_json.dumps(long))
    with pytest.raises(ValueError, match="over 100"):
        vods.build_youtube_md(session, session.segment(2), tmp_path)


def test_episode_times_in_check_text():
    from wabisanji_post.vods import episode_times

    seg = Segment.model_validate({"id": 1, "type": "gaming", "game": "AF", "series": "AF", "start": "00:12:20", "end": "05:06:30",
                                  "cuts": [{"start": "03:29:20", "end": "03:47:30", "reason": "BRB"}]})
    cut = {"start": 740.0, "parts": [{"rec_start": 740.0, "rec_end": 12560.0, "file_start": 0.0},
                                     {"rec_start": 13650.0, "rec_end": 18390.0, "file_start": 11820.0}]}
    text = "swearing 02:41-02:44, quiet 03:47-04:30, intro 00:05, cue at 04:34:30, in BRB 03:35"
    assert episode_times(text, seg=seg, cut=cut) == \
        "swearing 2:28-2:31, quiet 3:17-3:59, intro 00:05, cue at 4:04:00, in BRB 03:35"


def test_keyframe_at_or_before_falls_back_to_first_keyframe_at_file_start(monkeypatch):
    from wabisanji_post import vods as v
    monkeypatch.setattr(v, "_keyframes", lambda rec, a, b: [0.048, 2.048] if b >= 2.0 else [])
    assert v.keyframe_at_or_before(Path("x.mp4"), 0.0) == 0.048


def test_episode_times_maps_a_cut_start_to_the_end_of_the_part_before():
    from wabisanji_post import vods as v
    cut = {"start": 6256.867, "parts": [
        {"rec_start": 6256.867, "rec_end": 8950.0, "file_start": 0.0},
        {"rec_start": 9602.7, "rec_end": 16160.0, "file_start": 2693.184}]}
    seg = Segment(id=2, type="gaming", game="Alchemy Factory", series="Alchemy Factory", start="01:44:20", end="04:29:20")
    assert v.episode_times("break 02:29:10-02:40:00", cut, seg) == "break 0:44:53-0:44:53"


def test_banner_must_sit_inside_one_kept_part():
    from pydantic import ValidationError

    base = {"id": 4, "type": "gaming", "game": "Half-Life", "series": "Half-Life", "start": 100, "end": 1000,
            "cuts": [{"start": 400, "end": 500, "reason": "BRB"}]}
    seg = Segment.model_validate({**base, "banners": [{"start": 200, "end": 300, "text": "Mic muted"}]})
    assert seg.banners[0].text == "Mic muted"
    with pytest.raises(ValidationError, match="inside one kept part"):
        Segment.model_validate({**base, "banners": [{"start": 350, "end": 450, "text": "x"}]})  # across the cut
    with pytest.raises(ValidationError, match="inside one kept part"):
        Segment.model_validate({**base, "banners": [{"start": 50, "end": 150, "text": "x"}]})  # before the start
    with pytest.raises(ValidationError, match="time order"):
        Segment.model_validate({**base, "banners": [{"start": 250, "end": 300, "text": "x"},
                                                    {"start": 200, "end": 260, "text": "y"}]})


def test_cut_segment_with_banner_on_real_video(tmp_path):
    """60 s test video with a cut; banner over 0:21-0:33 of a 0:05-0:55 segment.

    The banner shows inside its window only, and the timeline (cut.json, length) is unchanged.
    """
    import datetime as dt
    import subprocess

    import numpy as np

    from wabisanji_post.media import probe_duration
    from wabisanji_post.models import Session
    from wabisanji_post.vods import cut_segment

    rec = tmp_path / "rec.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=10",
                    "-f", "lavfi", "-i", "sine=f=440:r=48000", "-t", "60", "-c:v", "libx264", "-g", "20",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(rec)], check=True)
    session = Session.model_validate({
        "session": "2026-10-10", "recording": str(rec), "duration": 60,
        "recording_start": dt.datetime(2026, 10, 10, 9, 54, tzinfo=dt.timezone(dt.timedelta(hours=11))),
        "segments": [{"id": 4, "type": "gaming", "game": "HL", "series": "HL", "start": 5, "end": 55,
                      "cuts": [{"start": 40, "end": 44, "reason": "BRB"}],
                      "banners": [{"start": 21, "end": 33, "text": "Mic muted for a few minutes"}]}],
    })
    info = cut_segment(session, session.segment(4), tmp_path, encoder="libx264")
    out = tmp_path / "vods" / info["file"]
    assert out.exists() and not (tmp_path / "vods" / "_banners_seg4").exists()
    assert not list((tmp_path / "vods").glob("*.banner.mp4"))
    b = info["banners"][0]
    assert abs(b["file_start"] - 17.0) < 0.3 and abs(b["file_end"] - 29.0) < 0.3  # file 0:00 = recording 4.0
    assert abs(probe_duration(out) - info["duration"]) < 0.5

    def frame(src, at):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", str(src), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(180, 320).astype(float)

    top, rest = slice(0, 40), slice(60, None)
    def diff(file_t, rec_t, rows):  # against the closest recording frame (seeks can land a frame apart)
        got = frame(out, file_t)[rows]
        return min(np.abs(got - frame(rec, rec_t + k / 10)[rows]).mean() for k in (-2, -1, 0, 1, 2))

    assert diff(21.0, 25.0, top) > 10      # banner on (small at 180p, so a modest mean over the top rows)
    assert diff(21.0, 25.0, rest) < 6      # the rest of the picture is the recording
    assert diff(10.0, 14.0, top) < 6       # banner off before the window
    assert diff(33.0, 37.0, top) < 6       # and after it


def test_blur_must_sit_inside_one_kept_part():
    from pydantic import ValidationError

    base = {"id": 1, "type": "gaming", "game": "Enshrouded", "series": "Enshrouded", "start": 100, "end": 1000,
            "cuts": [{"start": 400, "end": 500, "reason": "BRB"}]}
    blur = {"box": [1310, 975, 610, 100], "reason": "viewer's personal news in chat"}
    seg = Segment.model_validate({**base, "blurs": [{"start": 500, "end": 900, **blur}]})
    assert seg.blurs[0].box == (1310, 975, 610, 100)
    with pytest.raises(ValidationError, match="inside one kept part"):
        Segment.model_validate({**base, "blurs": [{"start": 350, "end": 450, **blur}]})  # across the cut
    with pytest.raises(ValidationError, match="w, h >= 4"):
        Segment.model_validate({**base, "blurs": [{"start": 200, "end": 300, "box": [0, 0, 0, 10], "reason": "x"}]})


def test_cut_segment_with_blur_on_real_video(tmp_path):
    """A box blurred over 0:46-0:52 of a 0:05-0:55 segment with a cut at 0:40-0:44: blurred inside
    its window only, the rest of the picture untouched, the timeline unchanged."""
    import datetime as dt
    import subprocess

    import numpy as np

    from wabisanji_post.media import probe_duration
    from wabisanji_post.models import Session
    from wabisanji_post.vods import cut_segment

    rec = tmp_path / "rec.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=10",
                    "-f", "lavfi", "-i", "sine=f=440:r=48000", "-t", "60", "-c:v", "libx264", "-g", "20",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(rec)], check=True)
    session = Session.model_validate({
        "session": "2026-10-17_2", "recording": str(rec), "duration": 60,
        "recording_start": dt.datetime(2026, 10, 17, 20, 24, tzinfo=dt.timezone(dt.timedelta(hours=11))),
        "segments": [{"id": 1, "type": "gaming", "game": "E", "series": "E", "start": 5, "end": 55,
                      "cuts": [{"start": 40, "end": 44, "reason": "personal"}],
                      # 1920x1080 reference pixels: (200, 120, 100, 50) on this 320x180 file
                      "blurs": [{"start": 46, "end": 52, "box": [1200, 720, 600, 300], "reason": "chat"}]}],
    })
    info = cut_segment(session, session.segment(1), tmp_path, encoder="libx264")
    out = tmp_path / "vods" / info["file"]
    assert out.exists() and not list((tmp_path / "vods").glob("*.banner.mp4"))
    assert "banners" not in info
    bl = info["blurs"][0]
    assert bl["box"] == [1200, 720, 600, 300] and abs(bl["file_start"] - 38.0) < 0.6  # 4 s before, 4 s cut (part lengths are probed)
    assert abs(probe_duration(out) - info["duration"]) < 0.5

    def frame(src, at):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", str(src), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(180, 320).astype(float)

    def rec_time(file_t):  # through cut.json's parts, like chapters
        p = [p for p in info["parts"] if p["file_start"] <= file_t][-1]
        return p["rec_start"] + file_t - p["file_start"]

    box, rest = (slice(120, 170), slice(200, 300)), (slice(0, 110), slice(0, 320))
    def diff(file_t, area):
        got = frame(out, file_t)[area]
        return min(np.abs(got - frame(rec, rec_time(file_t) + k / 10)[area]).mean() for k in (-2, -1, 0, 1, 2))

    assert diff(40.5, box) > 10      # blurred inside the window (recording 0:48)
    assert diff(40.5, rest) < 6      # the rest of the picture is the recording
    assert diff(30.0, box) < 6       # sharp before the window
    assert diff(46.0, box) < 6       # and after it
