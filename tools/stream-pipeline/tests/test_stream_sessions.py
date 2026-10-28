"""OBS filename parsing and session-folder helpers."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from wabisanji_post import sessions
from wabisanji_post.media import fine_times, frame_name, frame_time, per_minute_times

AEDT = dt.timezone(dt.timedelta(hours=11))


@pytest.mark.parametrize("name,expected", [
    ("2026-10-05 10-37-11.mkv", dt.datetime(2026, 10, 5, 10, 37, 11, tzinfo=AEDT)),
    (r"C:\Videos\2026-10-05 10-16-02.mkv", dt.datetime(2026, 10, 5, 10, 16, 2, tzinfo=AEDT)),
    ("2026-09-15 19-02-44.mp4", dt.datetime(2026, 9, 15, 19, 2, 44, tzinfo=AEDT)),
    ("2026-09-15_19-02-44.mkv", dt.datetime(2026, 9, 15, 19, 2, 44, tzinfo=AEDT)),
    (Path("C:/Videos/2026-10-05 15-13-06.mkv"), dt.datetime(2026, 10, 5, 15, 13, 6, tzinfo=AEDT)),
])
def test_parse_obs_filename(name, expected):
    assert sessions.parse_obs_filename(name, tz=AEDT) == expected


@pytest.mark.parametrize("name", [
    "Alchemy Factory 1.mp4",
    "2026-10-05.mkv",
    "2026-10-05 10-37.mkv",
    "Replay 2026-10-05 10-37-11.mkv",
    "2026-13-05 10-37-11.mkv",  # month 13
    "2026-10-05 25-00-00.mkv",  # hour 25
])
def test_parse_obs_filename_rejects(name):
    with pytest.raises(ValueError):
        sessions.parse_obs_filename(name, tz=AEDT)


def test_parse_obs_filename_local_zone_has_offset():
    got = sessions.parse_obs_filename("2026-10-05 10-37-11.mkv")
    assert got.tzinfo is not None
    assert (got.year, got.month, got.day, got.hour, got.minute, got.second) == (2026, 10, 5, 10, 37, 11)


def test_next_session_id(tmp_path):
    day = dt.date(2026, 10, 5)
    assert sessions.next_session_id(day, tmp_path) == "2026-10-05"
    (tmp_path / "2026-10-05").mkdir()
    assert sessions.next_session_id(day, tmp_path) == "2026-10-05_2"
    (tmp_path / "2026-10-05_2").mkdir()
    assert sessions.next_session_id(day, tmp_path) == "2026-10-05_3"


def test_resolve_and_find_session(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_SESSIONS_ROOT", str(tmp_path))
    rec = tmp_path / "2026-10-05 10-37-11.mkv"
    rec.write_bytes(b"")
    d = tmp_path / "2026-10-05"
    d.mkdir()
    (d / "session.yaml").write_text(f"session: 2026-10-05\nrecording: {rec}\n", encoding="utf-8")
    assert sessions.resolve_session_dir("2026-10-05") == d
    assert sessions.resolve_session_dir(d) == d
    assert sessions.resolve_session_dir(d / "session.yaml") == d
    assert sessions.find_session_for_recording(rec, tmp_path) == d
    assert sessions.find_session_for_recording(tmp_path / "other.mkv", tmp_path) is None
    with pytest.raises(FileNotFoundError):
        sessions.resolve_session_dir("2026-01-01")


def test_frame_times_and_names():
    assert per_minute_times(8798)[:3] == [30, 90, 150]
    assert len(per_minute_times(8798)) == 147  # matches the Cowork run
    assert fine_times(455, 515) == [455, 465, 475, 485, 495, 505, 515]
    assert frame_name(90) == "f_00090.jpg"
    assert frame_name(2472.5) == "f_02472.5.jpg"
    assert frame_time(Path(frame_name(2472.5))) == 2472.5
    assert frame_time(Path("f_03095.jpg")) == 3095
