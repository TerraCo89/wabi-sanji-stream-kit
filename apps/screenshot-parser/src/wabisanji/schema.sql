-- Wabi Sanji screenshot parser: capture log schema.
-- Trimmed from japanese-tutor's 13-table schema. SRS lives in Anki now, so
-- flashcards/review_sessions/study_goals are gone. This DB records what was
-- captured and what has been pushed to Anki.

PRAGMA foreign_keys = ON;

-- Games / learning sources
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT CHECK(type IN ('game', 'manga', 'anime', 'visual_novel', 'other') OR type IS NULL),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- OCR-processed screenshots
CREATE TABLE IF NOT EXISTS screenshots (
    id INTEGER PRIMARY KEY,
    file_path TEXT,
    checksum TEXT UNIQUE,               -- SHA-256, duplicate detection
    source_id INTEGER,
    ocr_method TEXT,                    -- claude | manga-ocr | hybrid
    extracted_text_json TEXT NOT NULL,  -- JSON array of text segments
    translation TEXT,
    context TEXT,
    section TEXT,                       -- play-order section / Anki subdeck, e.g. "01 Pallet Town"
    speaker TEXT,                       -- who's talking (NPC), for the card's source line
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE SET NULL
);

-- Vocabulary encountered across screenshots
CREATE TABLE IF NOT EXISTS vocabulary (
    id INTEGER PRIMARY KEY,
    word TEXT NOT NULL,
    reading TEXT,
    meaning TEXT,
    part_of_speech TEXT,
    jlpt_level TEXT CHECK(jlpt_level IN ('N5', 'N4', 'N3', 'N2', 'N1') OR jlpt_level IS NULL),
    category TEXT DEFAULT 'speech' CHECK(category IN ('speech', 'menu', 'pokemon')),
    encounter_count INTEGER DEFAULT 1 CHECK(encounter_count >= 1),
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    anki_note_id INTEGER,               -- set once pushed to Anki
    anki_synced_at TIMESTAMP,
    UNIQUE(word, reading)
);

-- Which screenshot each word was seen in (with the sentence it appeared in)
CREATE TABLE IF NOT EXISTS screenshot_vocabulary (
    screenshot_id INTEGER NOT NULL,
    vocabulary_id INTEGER NOT NULL,
    sentence TEXT,
    form TEXT,                          -- how it appeared on screen, e.g. "あがった (past tense)"
    PRIMARY KEY (screenshot_id, vocabulary_id),
    FOREIGN KEY (screenshot_id) REFERENCES screenshots(id) ON DELETE CASCADE,
    FOREIGN KEY (vocabulary_id) REFERENCES vocabulary(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_screenshots_checksum ON screenshots(checksum);
CREATE INDEX IF NOT EXISTS idx_screenshots_processed_at ON screenshots(processed_at);
CREATE INDEX IF NOT EXISTS idx_vocabulary_word ON vocabulary(word);
CREATE INDEX IF NOT EXISTS idx_vocabulary_unsynced ON vocabulary(anki_note_id) WHERE anki_note_id IS NULL;
