"""Backlog inventory: VOD matching, classification, split streams, session ids (synthetic files)."""
from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

import pytest
import yaml

from wabisanji_post import backlog, sessions


def make_video(path: Path, seconds: int) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s=16x16:r=1",
                    "-t", str(seconds), "-c:v", "libx264", str(path)], check=True)
    return path


def vod(vid: str, start: dt.datetime, seconds: int, title: str) -> dict:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return {"id": vid, "type": "archive", "title": title,
            "created_at": start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration": f"{h}h{m}m{s}s" if h else f"{m}m{s}s"}


def obs_start(name: str) -> dt.datetime:
    return sessions.parse_obs_filename(name)  # local zone, as the inventory reads it


@pytest.mark.parametrize("text,seconds", [("2h27m59s", 8879), ("1m20s", 80), ("37m41s", 2261), ("45s", 45)])
def test_parse_duration(text, seconds):
    assert backlog.parse_duration(text) == seconds


def test_inventory_classifies_and_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(backlog, "FALSE_START_BYTES", 0)  # tiny synthetic files; use length only
    rec = tmp_path / "Videos"
    (rec / "WabiSanji").mkdir(parents=True)
    (rec / "other-project").mkdir()
    make_video(rec / "other-project" / "not ours.mp4", 400)  # unrelated folder: never listed
    a = make_video(rec / "2026-09-18 08-57-19.mkv", 360)
    b = make_video(rec / "2026-09-18 09-04-00.mkv", 360)  # 21 s after a ends, same VOD title: split?
    make_video(rec / "2026-09-19 10-00-00.mkv", 360)  # no VOD: local test
    make_video(rec / "2026-09-19 11-00-00.mkv", 30)  # false start
    make_video(rec / "WabiSanji" / "Game 1.mp4", 360)  # downloaded VOD: matched by length
    root = tmp_path / "sessions"
    (root / "2026-09-18").mkdir(parents=True)  # id taken
    vods = [
        vod("v1", obs_start(a.name), 360, "THIS IS MY WOW"),
        vod("v2", obs_start(b.name), 360, "THIS IS MY WOW"),
        vod("v0", dt.datetime(2026, 9, 10, 8, 41, tzinfo=dt.timezone(dt.timedelta(hours=10))), 361, "Makin' Magic"),
    ]
    items = {Path(i["file"]).name: i for i in backlog.inventory(rec, root, vods, db=tmp_path / "none.db")}
    assert "not ours.mp4" not in items
    assert items[a.name]["class"] == "stream" and items[a.name]["vod"]["id"] == "v1"
    assert items[b.name]["split_from"] == a.name
    assert items["2026-09-19 10-00-00.mkv"]["class"] == "local_test"
    assert items["2026-09-19 11-00-00.mkv"]["class"] == "false_start"
    g = items["Game 1.mp4"]
    assert g["class"] == "stream" and g["start_from"] == "vod (duration match)" and g["start"].startswith("2026-09-10T08:41")
    assert g["session_id"] == "2026-09-10"
    assert items[a.name]["session_id"] == "2026-09-18_2" and items[b.name]["session_id"] == "2026-09-18_3"


def test_inventory_marks_session_recordings(tmp_path, monkeypatch):
    monkeypatch.setattr(backlog, "FALSE_START_BYTES", 0)
    rec = tmp_path / "Videos"
    rec.mkdir()
    f = make_video(rec / "2026-10-05 10-37-11.mkv", 360)
    t = make_video(rec / "2026-10-05 10-16-02.mkv", 360)
    root = tmp_path / "sessions"
    (root / "2026-10-05").mkdir(parents=True)
    (root / "2026-10-05" / "session.yaml").write_text(
        f"session: 2026-10-05\nrecording: {f}\nexcluded_files:\n  - file: {t}\n    reason: local test\n", encoding="utf-8")
    items = {Path(i["file"]).name: i for i in backlog.inventory(rec, root, [], db=tmp_path / "none.db")}
    assert items[f.name]["class"] == "in_session" and "session_id" not in items[f.name]
    assert items[t.name]["class"] == "excluded"


def test_merge_keeps_confirmed_edits(tmp_path):
    path = tmp_path / "_backlog.yaml"
    backlog.write_backlog(path, [
        {"file": "A.mkv", "class": "local_test", "confirmed": True, "notes": "Sam: was a test"},
        {"file": "B.mkv", "class": "stream", "confirmed": False, "notes": "check game"},
    ])
    fresh = [{"file": "A.mkv", "class": "stream", "session_id": "2026-09-01", "confirmed": False},
             {"file": "B.mkv", "class": "stream", "confirmed": False}]
    merged = {i["file"]: i for i in backlog.merge_previous(fresh, path)}
    assert merged["A.mkv"]["class"] == "local_test" and merged["A.mkv"]["confirmed"] is True
    assert merged["B.mkv"]["notes"] == "check game" and merged["B.mkv"]["confirmed"] is False
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["recordings"][0]["file"] == "A.mkv"
