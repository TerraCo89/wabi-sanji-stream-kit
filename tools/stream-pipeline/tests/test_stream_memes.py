"""Episode memes: the rules in plan() and the ffmpeg filter graph (no ffmpeg run)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from wabisanji_post import memes as m
from wabisanji_post.models import Segment, Session
from wabisanji_post.vods import meme_audio, meme_filters

START = dt.datetime(2026, 10, 19, 8, 35, 4, tzinfo=dt.timezone(dt.timedelta(hours=11)))


def session(seg: Segment) -> Session:
    return Session.model_validate({
        "session": "2026-10-19", "status": "transcribed", "recording": "C:/x.mkv",
        "recording_start": START.isoformat(), "duration": "02:00:00", "segments": [seg.model_dump()],
    })


def seg(**kw) -> Segment:
    return Segment.model_validate({"id": 3, "type": "gaming", "game": "Half-Life", "series": "Half-Life",
                                   "start": "00:10:00", "end": "00:40:00",
                                   "cuts": [{"start": "00:20:00", "end": "00:22:00", "reason": "BRB"}], **kw})


@pytest.fixture
def lib(tmp_path):
    folder = tmp_path / "memes"
    folder.mkdir()
    for f in ("oh-no-anyway.mp4", "vine-boom.mp3", "twss.mp4", "you-died.png"):
        (folder / f).write_bytes(b"x")
    library = {
        "oh-no-anyway": m.LibraryMeme("oh-no-anyway", "oh-no-anyway.mp4", risk="med"),
        "vine-boom": m.LibraryMeme("vine-boom", "vine-boom.mp3", risk="low"),
        "twss": m.LibraryMeme("twss", "twss.mp4", family=False),
        "you-died": m.LibraryMeme("you-died", "you-died.png", risk="low"),
        "gone": m.LibraryMeme("gone", "gone.mp4"),
    }
    return library, folder


def run_plan(s, tmp_path, lib, inserts, live=()):
    library, folder = lib
    return m.plan(session(s), s, tmp_path, library=library, folder=folder, inserts=inserts,
                  live=list(live), duration=lambda path, kind: 2.0)


def ins(at, mid):
    return m.Insert(segment=3, at=at, id=mid)


def test_plan_rules(tmp_path, lib):
    s = seg()
    kept, dropped = run_plan(s, tmp_path, lib, [
        ins(700, "oh-no-anyway"),       # kept
        ins(720, "vine-boom"),          # within 90 s of the one before
        ins(1260, "you-died"),          # inside the BRB cut
        ins(900, "nope"),               # not in the library
        ins(1000, "gone"),              # file missing
        ins(1500, "vine-boom"),         # ARIA popped one live at 1480
        ins(2399, "you-died"),          # would run past the segment end (2 s long)
        ins(1700, "twss"),              # kept: no kids in this segment
    ], live=[(1480.0, "noice", "phrase")])
    assert [(p.insert.at, p.meme.id) for p in kept] == [(700, "oh-no-anyway"), (1700, "twss")]
    reasons = {i.at: why for i, why in dropped}
    assert "within 90s" in reasons[720]
    assert "cut" in reasons[1260]
    assert "no meme" in reasons[900]
    assert "file missing" in reasons[1000]
    assert "live" in reasons[1500]
    assert "past the end" in reasons[2399]


def test_family_rule_uses_the_transcript(tmp_path, lib, monkeypatch):
    monkeypatch.setenv("WS_FAMILY_PATTERN", r"\b(junior|kiddo)\b")
    s = seg()
    (tmp_path / "transcript").mkdir()
    (tmp_path / "transcript" / "seg3.txt").write_text("[00:12:00] Junior, pass me the pickaxe", encoding="utf-8")
    kept, dropped = run_plan(s, tmp_path, lib, [ins(700, "twss"), ins(1000, "oh-no-anyway")])
    assert [p.meme.id for p in kept] == ["oh-no-anyway"]
    assert "family is in this segment" in dropped[0][1]


def test_density_cap(tmp_path, lib):
    s = seg(start="00:10:00", end="00:15:00", cuts=[])  # 5 min: cap 2
    kept, dropped = run_plan(s, tmp_path, lib, [ins(610, "vine-boom"), ins(710, "oh-no-anyway"),
                                                ins(810, "vine-boom")])
    assert len(kept) == 2
    assert "cap" in dropped[0][1]


def test_live_memes_from_events(tmp_path):
    s = seg()
    events = tmp_path / "events.jsonl"
    rows = [{"type": "meme", "t": (START + dt.timedelta(seconds=650)).isoformat(), "id": "noice", "via": "phrase"},
            {"type": "question", "t": (START + dt.timedelta(seconds=700)).isoformat(), "id": "q1"},
            {"type": "meme", "t": (START - dt.timedelta(hours=2)).isoformat(), "id": "bruh"}]  # an older stream
    events.write_text("\n".join(json.dumps(r) for r in rows) + "\n{half a line", encoding="utf-8")
    assert m.live_memes(session(s), events) == [(650.0, "noice", "phrase")]


def test_load_inserts(tmp_path):
    (tmp_path / "memes.yaml").write_text(
        "memes:\n  - {segment: 3, at: '00:11:40.5', id: vine-boom, line: 'wait what', why: reveal}\n",
        encoding="utf-8")
    [i] = m.load_inserts(tmp_path)
    assert (i.segment, i.at, i.id, i.line) == (3, 700.5, "vine-boom", "wait what")


def placed(lib, mid, at, dur=2.0):
    library, folder = lib
    meme = library[mid]
    return m.Placed(m.Insert(3, at, mid), meme, folder / meme.file, dur, file_at=at)


def test_filter_graph(tmp_path, lib, monkeypatch):
    monkeypatch.setattr("wabisanji_post.vods._has_audio", lambda p: p.suffix == ".mp4")
    monkeypatch.setattr("wabisanji_post.memes.caption_png",
                        lambda text, h, target: target)
    library, _ = lib
    library["vine-boom"].caption = ""
    items = [placed(lib, "oh-no-anyway", 10), placed(lib, "you-died", 30, 3.0), placed(lib, "vine-boom", 50, 1.0)]
    inputs, chain, last, sounds = meme_filters(items, first_input=2, height=1080, work=tmp_path, last="v1")
    # clip -> input 2 (with audio), image -> input 3 (looped), sound -> input 4 (audio only, no caption)
    assert inputs.count("-i") == 3
    assert inputs[inputs.index("-loop") + 1] == "1"
    assert [s[2] for s in sounds] == ["2:a", "4:a"]
    assert last == "mv2"  # two pictures overlaid; the uncaptioned sound adds none
    assert "enable='between(t,10.000,12.000)'" in chain[1]
    audio = meme_audio(sounds)
    assert "between(t,10.000,12.000)+between(t,50.000,51.000)" in audio[0]
    assert "adelay=50000:all=1" in audio[2]
    assert audio[-1].endswith("amix=inputs=3:normalize=0:duration=first[aout]")


def test_captioned_sound_gets_a_picture(tmp_path, lib, monkeypatch):
    monkeypatch.setattr("wabisanji_post.memes.caption_png", lambda text, h, target: target)
    library, _ = lib
    library["vine-boom"].caption = "Oh no."
    inputs, chain, last, sounds = meme_filters([placed(lib, "vine-boom", 5, 1.5)], 1, 1080, tmp_path, "0:v")
    assert inputs.count("-i") == 2  # caption png, then the sound
    assert sounds == [(5, 6.5, "2:a")]
    assert last == "mv1"
