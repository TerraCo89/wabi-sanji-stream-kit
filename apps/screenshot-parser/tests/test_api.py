"""Stream API tests against a temp capture log built from schema.sql."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from wabisanji import session
from wabisanji.db import SCHEMA_PATH, connect_readonly
from wabisanji.stream.server import create_app
from wabisanji.stream.state import StreamState

GAME = "Pokemon LeafGreen"
OTHER = "Dragon Quest"


def _utc(dt: datetime) -> str:
    """SQLite CURRENT_TIMESTAMP format."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "capture.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def screenshot(sid, source_id, processed_at, section="01 Pallet Town", speaker=None):
        conn.execute(
            """INSERT INTO screenshots (id, file_path, source_id, extracted_text_json,
                                        section, speaker, processed_at)
               VALUES (?, ?, ?, '[]', ?, ?, ?)""",
            (sid, f"shot{sid}.png", source_id, section, speaker, processed_at),
        )

    def word(vid, text, reading, meaning, count, first_seen, last_seen):
        conn.execute(
            """INSERT INTO vocabulary (id, word, reading, meaning, encounter_count,
                                       first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (vid, text, reading, meaning, count, first_seen, last_seen),
        )

    def link(sid, vid, form=None):
        conn.execute(
            "INSERT INTO screenshot_vocabulary (screenshot_id, vocabulary_id, form) VALUES (?, ?, ?)",
            (sid, vid, form),
        )

    now = datetime.now(timezone.utc)
    yesterday = datetime.now().astimezone().replace(hour=0, minute=0, second=0) - timedelta(hours=1)

    conn.execute("INSERT INTO sources (id, name, type) VALUES (1, ?, 'game'), (2, ?, 'game')",
                 (GAME, OTHER))
    # Timestamps deliberately disagree with id order: screenshot 2 is newer by
    # id but has the older processed_at, and last_seen_at is Python ISO format.
    # Ordering must follow screenshot id.
    screenshot(1, 1, _utc(now))
    screenshot(2, 1, _utc(yesterday), speaker="オーキド")
    screenshot(3, 2, _utc(now))  # other game, newest of all

    word(1, "家", "いえ", "house", 2, _utc(yesterday), now.isoformat())  # seen in 1 and 2
    word(2, "母", "はは", "mother", 1, _utc(now), _utc(now))             # seen in 1 only
    word(3, "博士", "はかせ", "professor", 1, _utc(yesterday), _utc(yesterday))
    word(4, "勇者", "ゆうしゃ", "hero", 1, _utc(now), _utc(now))         # other game

    link(1, 1)
    link(1, 2)
    link(2, 3)
    link(2, 1, form="家に (location)")
    link(3, 4)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def client(db_path, monkeypatch):
    # Never read the real session file from tests
    monkeypatch.setattr(session, "load", lambda: {"source": GAME})
    return TestClient(create_app(db_path, StreamState()))


def test_health(client):
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["source"] == GAME
    assert body["overlay_hidden"] is False
    assert {"obs_connected", "capture_enabled", "live_ocr"} <= body.keys()


def test_health_reports_missing_db(tmp_path):
    client = TestClient(create_app(tmp_path / "missing.db", StreamState(source=GAME)))
    assert client.get("/health").json()["ok"] is False
    assert client.get("/api/words").status_code == 503


def test_words_newest_screenshot_first_and_deduplicated(client):
    words = client.get("/api/words").json()["words"]
    # Screenshot 2 (博士, 家) before screenshot 1 (母); 家 appears once, from
    # its newest screenshot, with that screenshot's form and speaker.
    assert [w["word"] for w in words] == ["博士", "家", "母"]
    house = words[1]
    assert house["form"] == "家に (location)"
    assert house["speaker"] == "オーキド"
    assert house["section"] == "01 Pallet Town"


def test_words_fields_and_is_new(client):
    words = {w["word"]: w for w in client.get("/api/words").json()["words"]}
    assert set(words["家"]) >= {
        "word", "reading", "meaning", "form", "jlpt_level", "category",
        "section", "speaker", "encounter_count", "is_new",
    }
    assert words["家"]["encounter_count"] == 2 and words["家"]["is_new"] is False
    assert words["母"]["is_new"] is True
    assert words["博士"]["is_new"] is True


def test_words_limit(client):
    assert [w["word"] for w in client.get("/api/words?limit=2").json()["words"]] == ["博士", "家"]
    assert client.get("/api/words?limit=0").status_code == 422


def test_words_source_filter(client):
    # Default (session) source excludes the other game's newer screenshot
    assert "勇者" not in [w["word"] for w in client.get("/api/words").json()["words"]]
    body = client.get("/api/words", params={"source": OTHER}).json()
    assert body["source"] == OTHER
    assert [w["word"] for w in body["words"]] == ["勇者"]


def test_cli_source_overrides_session(db_path, monkeypatch):
    monkeypatch.setattr(session, "load", lambda: {"source": GAME})
    client = TestClient(create_app(db_path, StreamState(source=OTHER)))
    assert [w["word"] for w in client.get("/api/words").json()["words"]] == ["勇者"]


def test_no_source_anywhere_shows_all(db_path, monkeypatch):
    monkeypatch.setattr(session, "load", lambda: {"source": None})
    client = TestClient(create_app(db_path, StreamState()))
    assert [w["word"] for w in client.get("/api/words").json()["words"]] == [
        "勇者", "博士", "家", "母",
    ]


def test_today_counts_local_day(client):
    # Today: 母 is new (家 and 博士 were first seen yesterday); screenshot 1.
    assert client.get("/api/today").json() == {"new_words": 1, "screenshots": 1}
    assert client.get("/api/today", params={"source": OTHER}).json() == {
        "new_words": 1, "screenshots": 1,
    }


def test_overlay_state_and_page(client):
    assert client.get("/api/state").json() == {"overlay_hidden": False}
    client.app.state.stream.toggle_overlay()
    assert client.get("/api/state").json() == {"overlay_hidden": True}

    page = client.get("/overlay")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "/api/words" in page.text


def test_readonly_connection_cannot_write(db_path):
    conn = connect_readonly(db_path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO sources (name) VALUES ('nope')")
    conn.close()
