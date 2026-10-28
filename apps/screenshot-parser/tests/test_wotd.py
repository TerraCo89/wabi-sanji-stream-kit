"""Word of the Day picker tests against a temp capture log."""

from __future__ import annotations

import random
import sqlite3
from datetime import date

import pytest

from wabisanji import wotd
from wabisanji.db import SCHEMA_PATH, connect_readonly

TODAY = date(2026, 10, 15)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "capture.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    db.execute("INSERT INTO sources (id, name, type) VALUES (1, 'Game', 'game')")
    words = [
        # id, word, reading, meaning, category
        (1, "町", "まち", "town", "speech"),
        (2, "炎", "ほのお", "flame", "speech"),
        (3, "どうぐ", "どうぐ", "items", "menu"),
        (4, "謎", None, "mystery", "speech"),
        (5, "将軍", "しょうぐん", "", "speech"),
    ]
    for vid, word, reading, meaning, category in words:
        db.execute("INSERT INTO screenshots (id, file_path, source_id, extracted_text_json) "
                   "VALUES (?, ?, 1, '[]')", (vid, f"shot{vid}.png"))
        db.execute("INSERT INTO vocabulary (id, word, reading, meaning, category) "
                   "VALUES (?, ?, ?, ?, ?)", (vid, word, reading, meaning, category))
        db.execute("INSERT INTO screenshot_vocabulary (screenshot_id, vocabulary_id) "
                   "VALUES (?, ?)", (vid, vid))
    db.commit()
    db.close()
    c = connect_readonly(path)
    yield c
    c.close()


def test_pick_skips_menu_and_incomplete_words(conn):
    picks = {wotd.pick(conn, {}, TODAY, random.Random(i))["word"] for i in range(30)}
    assert picks == {"町", "炎"}


def test_pick_avoids_recent_picks(conn):
    history = {"炎": "2026-10-10"}
    picks = {wotd.pick(conn, history, TODAY, random.Random(i))["word"] for i in range(30)}
    assert picks == {"町"}


def test_pick_reuses_words_when_all_were_recent(conn):
    history = {"町": "2026-10-14", "炎": "2026-10-14"}
    assert wotd.pick(conn, history, TODAY, random.Random(0))["word"] in {"町", "炎"}


def test_old_picks_come_back(conn):
    history = {"町": "2026-09-01", "炎": "2026-10-14"}
    assert wotd.pick(conn, history, TODAY, random.Random(0))["word"] == "町"


def test_save_writes_three_lines_and_history(tmp_path):
    out, hist = tmp_path / "wotd.txt", tmp_path / "hist.json"
    wotd.save("炎", "ほのお", "flame;\n blaze", TODAY, out=out, history_path=hist)
    assert out.read_text(encoding="utf-8").splitlines() == ["炎", "ほのお", "flame; blaze"]
    assert wotd.load_history(hist) == {"炎": "2026-10-15"}
