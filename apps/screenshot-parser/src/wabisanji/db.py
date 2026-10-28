"""SQLite capture log: screenshots seen, vocabulary encountered, Anki sync state."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def file_checksum(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the watcher creates the DB on the main
        # thread but uses it from watchdog's dispatch thread. Access is
        # serialized (single dispatch thread), so this is safe.
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # WAL unsupported on some filesystems (network mounts); default journal is fine
            logger.warning("WAL mode unavailable for %s; using default journal", self.db_path)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created."""
        for table, column in (
            ("screenshot_vocabulary", "form"),
            ("screenshots", "section"),
            ("screenshots", "speaker"),
        ):
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- screenshots ----------------------------------------------------------

    def screenshot_seen(self, image_path: str | Path) -> bool:
        """True if this exact image (by checksum) was already processed."""
        checksum = file_checksum(image_path)
        row = self.conn.execute(
            "SELECT id FROM screenshots WHERE checksum = ?", (checksum,)
        ).fetchone()
        return row is not None

    def save_analysis(self, result: dict[str, Any], source_name: str | None = None) -> int:
        """Persist an analyzer result. Returns the screenshot row id.

        Upserts vocabulary (bumping encounter_count on repeats) and links
        each word to this screenshot with its source sentence.
        """
        source_id = None
        if source_name:
            self.conn.execute(
                "INSERT OR IGNORE INTO sources (name, type) VALUES (?, 'game')",
                (source_name,),
            )
            source_id = self.conn.execute(
                "SELECT id FROM sources WHERE name = ?", (source_name,)
            ).fetchone()["id"]

        file_path = result.get("file_path")
        checksum = None
        if file_path and Path(file_path).exists():
            checksum = file_checksum(file_path)

        cur = self.conn.execute(
            """INSERT INTO screenshots
               (file_path, checksum, source_id, ocr_method, extracted_text_json, translation,
                context, section, speaker)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(checksum) DO UPDATE SET
                 ocr_method = excluded.ocr_method,
                 extracted_text_json = excluded.extracted_text_json,
                 translation = excluded.translation,
                 context = excluded.context,
                 section = COALESCE(excluded.section, screenshots.section),
                 speaker = COALESCE(excluded.speaker, screenshots.speaker)""",
            (
                file_path,
                checksum,
                source_id,
                result.get("ocr_method"),
                json.dumps(result.get("extracted_text", []), ensure_ascii=False),
                result.get("translation", ""),
                result.get("context", ""),
                result.get("section") or None,
                result.get("speaker") or None,
            ),
        )
        if cur.lastrowid:
            screenshot_id = cur.lastrowid
        else:
            screenshot_id = self.conn.execute(
                "SELECT id FROM screenshots WHERE checksum = ?", (checksum,)
            ).fetchone()["id"]

        # Join all on-screen segments (dialogue screens have one; menu/battle
        # screens have many) so the card's example matches the translation.
        segments = result.get("extracted_text", [])
        sentence = "　".join(s for s in (seg.get("text", "") for seg in segments) if s)

        for vocab in result.get("vocabulary", []):
            word = (vocab.get("word") or "").strip()
            if not word:
                continue
            reading = (vocab.get("reading") or "").strip() or None
            now = datetime.now(timezone.utc).isoformat()
            self.conn.execute(
                """INSERT INTO vocabulary (word, reading, meaning, part_of_speech, jlpt_level, category)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(word, reading) DO UPDATE SET
                     encounter_count = encounter_count + 1,
                     last_seen_at = ?,
                     meaning = COALESCE(vocabulary.meaning, excluded.meaning),
                     -- re-push so the note picks up this section's tag
                     anki_note_id = NULL""",
                (
                    word,
                    reading,
                    vocab.get("meaning"),
                    vocab.get("part_of_speech"),
                    vocab.get("jlpt_level"),
                    vocab.get("category") or "speech",
                    now,
                ),
            )
            vocab_id = self.conn.execute(
                "SELECT id FROM vocabulary WHERE word = ? AND reading IS ?", (word, reading)
            ).fetchone()["id"]
            self.conn.execute(
                """INSERT OR IGNORE INTO screenshot_vocabulary
                   (screenshot_id, vocabulary_id, sentence, form) VALUES (?, ?, ?, ?)""",
                (screenshot_id, vocab_id, sentence, (vocab.get("form") or "").strip() or None),
            )

        self.conn.commit()
        return screenshot_id

    # -- vocabulary / anki sync -------------------------------------------------

    def unsynced_vocabulary(self) -> list[sqlite3.Row]:
        """Vocabulary rows not yet pushed to Anki.

        Example sentence, screenshot, speaker and section come from where the
        word was first seen (that section is the card's deck); all_sections
        lists every section it has turned up in, for tags.
        """
        return self.conn.execute(
            """SELECT v.*,
                      sv.sentence, sv.form,
                      s.file_path AS screenshot_path,
                      s.translation AS sentence_translation,
                      s.speaker, s.section,
                      so.name AS source_name,
                      (SELECT GROUP_CONCAT(DISTINCT s2.section) FROM screenshot_vocabulary sv2
                        JOIN screenshots s2 ON s2.id = sv2.screenshot_id
                        WHERE sv2.vocabulary_id = v.id) AS all_sections
               FROM vocabulary v
               LEFT JOIN screenshot_vocabulary sv
                 ON sv.vocabulary_id = v.id
                AND sv.screenshot_id = (SELECT MIN(screenshot_id) FROM screenshot_vocabulary
                                         WHERE vocabulary_id = v.id)
               LEFT JOIN screenshots s ON s.id = sv.screenshot_id
               LEFT JOIN sources so ON so.id = s.source_id
               WHERE v.anki_note_id IS NULL
               ORDER BY v.first_seen_at, v.id""",
        ).fetchall()

    def mark_synced(self, vocabulary_id: int, anki_note_id: int) -> None:
        self.conn.execute(
            "UPDATE vocabulary SET anki_note_id = ?, anki_synced_at = ? WHERE id = ?",
            (anki_note_id, datetime.now(timezone.utc).isoformat(), vocabulary_id),
        )
        self.conn.commit()

    # -- stats ------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]
        return {
            "screenshots": q("SELECT COUNT(*) FROM screenshots"),
            "vocabulary": q("SELECT COUNT(*) FROM vocabulary"),
            "synced_to_anki": q("SELECT COUNT(*) FROM vocabulary WHERE anki_note_id IS NOT NULL"),
            "pending_sync": q("SELECT COUNT(*) FROM vocabulary WHERE anki_note_id IS NULL"),
            "sources": q("SELECT COUNT(*) FROM sources"),
        }


# -- read-only queries (stream server) -------------------------------------------
#
# The stream server reads while `ingest` writes from another process. These
# helpers open a fresh read-only connection instead of using Database(), whose
# constructor runs the schema script and migrations (both write).


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open the capture log read-only. Raises sqlite3.OperationalError if it's missing."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def recent_words(
    conn: sqlite3.Connection, limit: int = 8, source: str | None = None
) -> list[dict[str, Any]]:
    """Newest vocabulary, by the screenshot it was most recently seen in.

    Ordered by screenshot id (newest first), not by timestamp: processed_at is
    SQLite's UTC "YYYY-MM-DD HH:MM:SS" but last_seen_at is Python ISO format,
    so string ordering across them is wrong. Each word appears once, taken from
    its newest screenshot; words from the same screenshot keep sentence order.
    `source` filters by game name; None means all sources.
    """
    rows = conn.execute(
        """WITH hits AS (
             SELECT s.id AS screenshot_id, sv.rowid AS link_order, sv.vocabulary_id,
                    sv.form, s.section, s.speaker,
                    ROW_NUMBER() OVER (
                      PARTITION BY sv.vocabulary_id ORDER BY s.id DESC, sv.rowid
                    ) AS rn
             FROM screenshots s
             JOIN screenshot_vocabulary sv ON sv.screenshot_id = s.id
             LEFT JOIN sources so ON so.id = s.source_id
             WHERE ?1 IS NULL OR so.name = ?1
           )
           SELECT v.id, v.word, v.reading, v.meaning, h.form, v.jlpt_level, v.category,
                  h.section, h.speaker, v.encounter_count
           FROM hits h
           JOIN vocabulary v ON v.id = h.vocabulary_id
           WHERE h.rn = 1
           ORDER BY h.screenshot_id DESC, h.link_order
           LIMIT ?2""",
        (source, limit),
    ).fetchall()
    return [{**dict(r), "is_new": r["encounter_count"] == 1} for r in rows]


def today_counts(
    conn: sqlite3.Connection, source: str | None = None, now: datetime | None = None
) -> dict[str, int]:
    """New words and screenshots for the local calendar day containing `now`.

    The local day's bounds are converted to UTC, since the DB stores UTC.
    datetime() normalises both SQLite and ISO timestamps before comparing.
    """
    local_now = (now or datetime.now()).astimezone()
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    # Re-attach the local zone at each bound so a DST change mid-day is respected
    fmt = lambda d: d.replace(tzinfo=None).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lo, hi = fmt(start), fmt(end)

    new_words = conn.execute(
        """SELECT COUNT(*) FROM vocabulary v
           WHERE datetime(v.first_seen_at) >= ?1 AND datetime(v.first_seen_at) < ?2
             AND (?3 IS NULL OR EXISTS (
               SELECT 1 FROM screenshot_vocabulary sv
               JOIN screenshots s ON s.id = sv.screenshot_id
               JOIN sources so ON so.id = s.source_id
               WHERE sv.vocabulary_id = v.id AND so.name = ?3))""",
        (lo, hi, source),
    ).fetchone()[0]
    screenshots = conn.execute(
        """SELECT COUNT(*) FROM screenshots s
           LEFT JOIN sources so ON so.id = s.source_id
           WHERE datetime(s.processed_at) >= ?1 AND datetime(s.processed_at) < ?2
             AND (?3 IS NULL OR so.name = ?3)""",
        (lo, hi, source),
    ).fetchone()[0]
    return {"new_words": new_words, "screenshots": screenshots}
