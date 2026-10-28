"""Moment-signal helpers (no ffmpeg, no DB writes)."""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from wabisanji_post.models import Session
from wabisanji_post.signals import (
    capture_time,
    find_heard,
    loudness_peaks,
    to_romaji,
    vocab_signals,
)

AEDT = dt.timezone(dt.timedelta(hours=11))


@pytest.mark.parametrize("kana,romaji", [
    ("あした", "ashita"),
    ("だいじょうぶ", "daijoubu"),
    ("ここまで", "kokomade"),
    ("いわかげ", "iwakage"),
    ("しんじる", "shinjiru"),
    ("ちょっと", "chotto"),
    ("きゃく", "kyaku"),
    ("まっちゃ", "matcha"),
    ("ニーナ", "niina"),
    ("サライ", "sarai"),
    ("岩カゲ", "kage"),  # kanji dropped; readings are kana anyway
])
def test_to_romaji(kana, romaji):
    assert to_romaji(kana) == romaji


def test_capture_time_from_filenames():
    obs = capture_time(r"C:\Videos\Screenshot 2026-10-05 11-28-16.png", "2026-10-05 00:28:40", AEDT)
    assert obs == (dt.datetime(2026, 10, 5, 11, 28, 16, tzinfo=AEDT), "filename")
    ra = capture_time(r"C:\RetroArch\screenshots\Pocket Monsters - LeafGreen (Japan)-261004-121323.png", None, AEDT)
    assert ra == (dt.datetime(2026, 10, 4, 12, 13, 23, tzinfo=AEDT), "filename")


def test_capture_time_falls_back_to_processed_at_utc():
    t, how = capture_time("shot.png", "2026-10-05 00:28:40", AEDT)
    assert how == "processed_at"
    assert t == dt.datetime(2026, 10, 5, 11, 28, 40, tzinfo=AEDT)


def words(*pairs):
    return [{"w": w, "s": s, "e": s + 0.4} for w, s in pairs]


def test_find_heard_joins_split_readings():
    ws = words(("Koko", 2403.6), ("Made.", 2404.0), ("Waiteru.", 2411.5), ("Ashita.", 2487.4))
    hits = find_heard("kokomade", ws, 2405, window=120)
    assert [h["said"] for h in hits] == ["Koko Made."]
    assert hits[0]["score"] == 100


def test_find_heard_fuzzy_and_window():
    ws = words(("Deijoubu", 2135.6), ("desu.", 2136.0), ("Deijoubu.", 4000.0))
    hits = find_heard("daijoubu", ws, 2127, window=120)
    assert [h["hms"] for h in hits] == ["00:35:35.6"]  # the far one is outside the window


def test_find_heard_short_words_need_exact_match():
    ws = words(("Nari.", 2761.5), ("Ano.", 2657.9))
    assert find_heard("nai", ws, 2704, window=120) == []
    assert [h["said"] for h in find_heard("ano", ws, 2656, window=120)] == ["Ano."]


def test_loudness_peaks_spacing_and_threshold():
    series = [(float(t), -40.0) for t in range(100)]
    series[10] = (10.0, -25.0)
    series[12] = (12.0, -28.0)  # within 20 s of the bigger peak: dropped
    series[60] = (60.0, -30.0)
    series[80] = (80.0, -36.0)  # only 4 LU above median: not a peak
    median, peaks = loudness_peaks(series, above=6, spacing=20)
    assert median == -40.0
    assert [(p["t"], p["above_median"]) for p in peaks] == [(10.0, 15.0), (60.0, 10.0)]


def test_vocab_signals_reads_db_readonly(tmp_path):
    db = tmp_path / "wabisanji.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE screenshots (id INTEGER PRIMARY KEY, file_path TEXT, source_id INTEGER,
            extracted_text_json TEXT, translation TEXT, processed_at TEXT, section TEXT, speaker TEXT);
        CREATE TABLE vocabulary (id INTEGER PRIMARY KEY, word TEXT, reading TEXT, meaning TEXT,
            jlpt_level TEXT, category TEXT, first_seen_at TEXT);
        CREATE TABLE screenshot_vocabulary (screenshot_id INTEGER, vocabulary_id INTEGER, sentence TEXT, form TEXT);
        INSERT INTO sources VALUES (2, 'Breath of Fire IV');
        INSERT INTO screenshots VALUES (1, 'C:\\Videos\\Screenshot 2026-10-05 11-18-15.png', 2,
            '[{"text": "休んでも、明日には‥"}]', 'Even if we rest, by tomorrow...', '2026-10-05 00:18:30', '02 Great Desert', 'Cray');
        INSERT INTO screenshots VALUES (2, 'C:\\Videos\\Screenshot 2026-10-05 09-00-00.png', 2, '[]', NULL, NULL, NULL, NULL);
        INSERT INTO vocabulary VALUES (1, '明日', 'あした', 'tomorrow', 'N5', 'speech', NULL);
        INSERT INTO screenshot_vocabulary VALUES (1, 1, '休んでも、明日には‥', NULL);
    """)
    con.commit()
    con.close()
    session = Session(session="2026-10-05", status="transcribed", recording="x.mkv",
                      recording_start=dt.datetime(2026, 10, 5, 10, 37, 11, tzinfo=AEDT), duration=8798,
                      segments=[{"id": 2, "type": "japanese", "game": "BoF", "series": "BoF", "start": 900, "end": 3110}])
    out = vocab_signals(session, {2: words(("Ashita.", 2487.4))}, db)
    assert out["count"] == 1  # the 09:00 shot is before the recording
    cap = out["captures"][0]
    assert cap["hms"] == "00:41:04" and cap["segment"] == 2 and cap["text"] == "休んでも、明日には‥"
    assert cap["vocab"][0]["romaji"] == "ashita"
    assert cap["vocab"][0]["heard"][0]["hms"] == "00:41:27.4"
