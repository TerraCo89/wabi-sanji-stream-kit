"""AnkiConnect integration: push captured vocabulary into Anki.

Requires the AnkiConnect add-on (code 2055492159) and Anki running.
Anki owns the SRS; this module only creates/updates notes.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import requests

from .config import AnkiConfig
from .db import Database

logger = logging.getLogger(__name__)

# Decks: <base>::<game>::<section>, sections numbered in play order
# ("01 Pallet Town") plus fixed Menus / Battles decks. Pokémon species and
# character names get their own deck so the town decks stay about the language.
NAMES_SECTION = "Pokémon & Names"
GENERIC_NAMES_SECTION = "Names"  # same deck for games that aren't Pokémon
UNSORTED_SECTION = "Unsorted"


def is_pokemon_game(source: str | None) -> bool:
    return "pokemon" in (source or "").lower().replace("é", "e")

NOTE_FIELDS = [
    "Word",
    "Reading",
    "Meaning",
    "PartOfSpeech",
    "JLPT",
    "Sentence",
    "SentenceTranslation",
    "Context",
    "Screenshot",
    "Source",
]

# Reading sits in a hover-only <rt> on the front: peek at furigana when stuck
# without it giving the card away. The Context field holds the surface form
# the word appeared as on screen (e.g. "あがった (past tense)").
CARD_FRONT = """<div class="word"><ruby>{{Word}}<rt>{{Reading}}</rt></ruby></div>
{{#Sentence}}<div class="sentence">{{Sentence}}</div>{{/Sentence}}
{{#Screenshot}}<div class="screenshot">{{Screenshot}}</div>{{/Screenshot}}"""

# {{tts}} uses the OS's Japanese voice (Windows: Settings > Speech > Add
# voices > Japanese); no audio files or add-ons needed.
CARD_BACK = """{{FrontSide}}
<hr id="answer">
<div class="reading">{{Reading}} {{#Reading}}{{tts ja_JP:Reading}}{{/Reading}}</div>
{{#Context}}<div class="form">on screen: {{Context}}</div>{{/Context}}
<div class="meaning">{{Meaning}}</div>
{{#PartOfSpeech}}<div class="pos">{{PartOfSpeech}}{{#JLPT}} &middot; {{JLPT}}{{/JLPT}}</div>{{/PartOfSpeech}}
{{#SentenceTranslation}}<div class="translation">{{SentenceTranslation}}</div>{{/SentenceTranslation}}
{{#Source}}<div class="source">{{Source}}</div>{{/Source}}"""

CARD_CSS = """.card { font-family: "Noto Sans JP", sans-serif; font-size: 22px; text-align: center; }
.word { font-size: 42px; }
.word rt { font-size: 18px; color: #1a7a6d; visibility: hidden; }
.word:hover rt, .word:active rt { visibility: visible; }
.form { font-size: 18px; color: #b5651d; margin-top: 4px; }
.sentence { font-size: 24px; color: #555; margin-top: 12px; }
.screenshot img { max-width: 90%; max-height: 320px; margin-top: 12px; }
.reading { color: #1a7a6d; font-size: 28px; }
.meaning { margin-top: 8px; }
.pos, .source { font-size: 14px; color: #999; margin-top: 8px; }
.translation { font-size: 18px; color: #777; margin-top: 8px; }"""


class AnkiConnectError(RuntimeError):
    pass


class AnkiClient:
    def __init__(self, config: AnkiConfig):
        self.config = config
        self._decks: set[str] = set()

    def invoke(self, action: str, **params: Any) -> Any:
        """Call an AnkiConnect action; raises AnkiConnectError on failure."""
        try:
            response = requests.post(
                self.config.url,
                json={"action": action, "version": 6, "params": params},
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as e:
            raise AnkiConnectError(
                f"Cannot reach AnkiConnect at {self.config.url}. "
                f"Is Anki running with the AnkiConnect add-on installed? ({e})"
            )
        data = response.json()
        if data.get("error"):
            raise AnkiConnectError(f"AnkiConnect {action} failed: {data['error']}")
        return data.get("result")

    def is_available(self) -> bool:
        try:
            self.invoke("version")
            return True
        except AnkiConnectError:
            return False

    # -- setup -----------------------------------------------------------------

    def deck_for(self, category: str | None, source: str | None, section: str | None) -> str:
        if (category or "").lower() == "pokemon":
            section = NAMES_SECTION if is_pokemon_game(source) else GENERIC_NAMES_SECTION
        game = (source or "Unsorted").replace("::", " ")
        return f"{self.config.deck}::{game}::{section or UNSORTED_SECTION}"

    def ensure_deck(self, deck: str) -> None:
        if deck not in self._decks:
            self.invoke("createDeck", deck=deck)
            self._decks.add(deck)

    def ensure_note_type(self) -> None:
        """Create the note type, or bring an existing one's templates up to date."""
        existing = self.invoke("modelNames")
        if self.config.note_type in existing:
            self.invoke(
                "updateModelTemplates",
                model={
                    "name": self.config.note_type,
                    "templates": {"Recognition": {"Front": CARD_FRONT, "Back": CARD_BACK}},
                },
            )
            self.invoke("updateModelStyling", model={"name": self.config.note_type, "css": CARD_CSS})
            return
        logger.info("Creating Anki note type %r", self.config.note_type)
        self.invoke(
            "createModel",
            modelName=self.config.note_type,
            inOrderFields=NOTE_FIELDS,
            css=CARD_CSS,
            cardTemplates=[
                {"Name": "Recognition", "Front": CARD_FRONT, "Back": CARD_BACK}
            ],
        )

    def setup(self) -> None:
        self.ensure_note_type()

    # -- notes -----------------------------------------------------------------

    def store_screenshot(self, image_path: str | Path) -> str | None:
        """Copy a screenshot into Anki's media folder. Returns the media filename."""
        path = Path(image_path)
        if not path.exists():
            return None
        filename = f"wabisanji-{path.stem}{path.suffix}"
        self.invoke("storeMediaFile", filename=filename, path=str(path.resolve()))
        return filename

    def find_note(self, word: str) -> int | None:
        """Find an existing note for this word in our deck, if any."""
        escaped = word.replace('"', '\\"')
        ids = self.invoke(
            "findNotes",
            query=f'"deck:{self.config.deck}" "Word:{escaped}"',
        )
        return ids[0] if ids else None

    def add_or_update_note(self, fields: dict[str, str], deck: str, tags: list[str]) -> int:
        """Create a note in `deck`, or update the existing note in place.

        Existing notes keep their deck (where the word was first seen) and
        gain any new section tags.
        """
        tags = [self.config.tag, *tags]
        existing_id = self.find_note(fields["Word"])
        if existing_id:
            self.invoke("updateNoteFields", note={"id": existing_id, "fields": fields})
            self.invoke("addTags", notes=[existing_id], tags=" ".join(tags))
            return existing_id
        self.ensure_deck(deck)
        return self.invoke(
            "addNote",
            note={
                "deckName": deck,
                "modelName": self.config.note_type,
                "fields": fields,
                "tags": tags,
                "options": {"allowDuplicate": False},
            },
        )


def section_tag(source: str | None, section: str) -> str:
    """Anki tag for a section, e.g. wabisanji::pokemon_leafgreen::01_pallet_town."""
    slug = lambda t: re.sub(r"[^\w]+", "_", t.lower()).strip("_")
    return f"wabisanji::{slug(source or 'unsorted')}::{slug(section)}"


def sync_vocabulary(db: Database, client: AnkiClient) -> tuple[int, int]:
    """Push all unsynced vocabulary to Anki. Returns (synced, failed)."""
    client.setup()
    rows = db.unsynced_vocabulary()
    synced = failed = 0

    for row in rows:
        try:
            screenshot_field = ""
            if row["screenshot_path"]:
                media_name = client.store_screenshot(row["screenshot_path"])
                if media_name:
                    screenshot_field = f'<img src="{media_name}">'

            fields = {
                "Word": row["word"] or "",
                "Reading": row["reading"] or "",
                "Meaning": row["meaning"] or "",
                "PartOfSpeech": row["part_of_speech"] or "",
                "JLPT": row["jlpt_level"] or "",
                "Sentence": row["sentence"] or "",
                "SentenceTranslation": row["sentence_translation"] or "",
                "Context": row["form"] or "",
                "Screenshot": screenshot_field,
                "Source": " · ".join(
                    p for p in (row["source_name"], row["section"], row["speaker"]) if p
                ),
            }
            deck = client.deck_for(row["category"], row["source_name"], row["section"])
            tags = [
                section_tag(row["source_name"], sec)
                for sec in (row["all_sections"] or "").split(",") if sec
            ]
            note_id = client.add_or_update_note(fields, deck=deck, tags=tags)
            db.mark_synced(row["id"], note_id)
            synced += 1
            logger.info("Synced %s -> Anki note %s", row["word"], note_id)
        except AnkiConnectError as e:
            failed += 1
            logger.warning("Failed to sync %r: %s", row["word"], e)

    return synced, failed
