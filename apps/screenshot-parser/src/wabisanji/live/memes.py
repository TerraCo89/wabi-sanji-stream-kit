"""The meme library and the instant phrase matcher.

The index (content/stream/memes.yaml) lists each meme with:
- id: what `wabisanji live meme <id>` and the overlay use
- file: clip (.mp4), sound (.mp3) or image/GIF in the memes folder
- caption: optional text shown on screen with a sound (the quote, for sound bites)
- phrases: things the streamer says that fire it instantly from `wabisanji listen`
- about: what it means and when it fits, for Claude's slower, looser picks
- category: fail | shock | suspicion | win | awkward | chaos | numbers | japanese | ...
- risk: low | med | high: Content ID risk of the audio (episodes and Shorts use it)
- family: false for innuendo and the like; skipped in family mode and kids' segments
- cooldown_s: optional; the same meme won't repeat sooner (default 300)
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import yaml

DEFAULT_COOLDOWN_S = 300
FUZZY_MIN_WORDS = 3  # shorter phrases must match exactly; fuzzy 2-word matches misfire
FUZZY_RATIO = 0.86
RISKS = ("low", "med", "high")
CLIP_EXT = {".mp4", ".webm", ".mov", ".m4v"}
SOUND_EXT = {".mp3", ".m4a", ".wav", ".ogg"}


@dataclass
class Meme:
    id: str
    file: str
    phrases: list[str] = field(default_factory=list)
    about: str = ""
    cooldown_s: int = DEFAULT_COOLDOWN_S
    caption: str = ""
    category: str = ""
    risk: str = "high"  # unknown provenance counts as risky
    family: bool = True

    @property
    def kind(self) -> str:
        ext = Path(self.file).suffix.lower()
        return "clip" if ext in CLIP_EXT else "sound" if ext in SOUND_EXT else "image"


def load_index(path: str | Path) -> dict[str, Meme]:
    path = Path(path)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    memes = {}
    for entry in data.get("memes") or []:
        risk = str(entry.get("risk") or "high")
        if risk not in RISKS:
            raise ValueError(f"meme {entry.get('id')}: risk must be one of {', '.join(RISKS)}")
        meme = Meme(
            id=str(entry["id"]),
            file=str(entry["file"]),
            phrases=[str(p) for p in entry.get("phrases") or []],
            about=str(entry.get("about") or ""),
            cooldown_s=int(entry.get("cooldown_s") or DEFAULT_COOLDOWN_S),
            caption=str(entry.get("caption") or ""),
            category=str(entry.get("category") or ""),
            risk=risk,
            family=bool(entry.get("family", True)),
        )
        memes[meme.id] = meme
    return memes


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower().replace("'", "")).split())


def phrase_in(phrase: str, text: str) -> bool:
    """Whole-word match of `phrase` in `text`, fuzzy for longer phrases."""
    p, t = normalize(phrase), normalize(text)
    if not p:
        return False
    if f" {p} " in f" {t} ":
        return True
    p_words, t_words = p.split(), t.split()
    if len(p_words) < FUZZY_MIN_WORDS or len(t_words) < len(p_words):
        return False
    n = len(p_words)
    for i in range(len(t_words) - n + 1):
        window = " ".join(t_words[i:i + n])
        if SequenceMatcher(None, p, window).ratio() >= FUZZY_RATIO:
            return True
    return False


class MemeMatcher:
    """Finds the meme an utterance quotes, honouring cooldowns."""

    def __init__(self, memes: dict[str, Meme], gap_s: float = 20, clock=time.monotonic,
                 rng: random.Random | None = None):
        self.memes = memes
        self.gap_s = gap_s
        self.clock = clock
        self.rng = rng or random.Random()
        self._fired: dict[str, float] = {}
        self._last = float("-inf")

    def match(self, text: str, family_mode: bool = False) -> Meme | None:
        now = self.clock()
        if now - self._last < self.gap_s:
            return None
        # Longest phrase wins, so "it's a trap" beats a meme keyed on "trap". Memes tied
        # on it (the "that's what she said" clips) are picked at random.
        best: dict[str, tuple[int, Meme]] = {}
        for m in self.memes.values():
            if not (m.family or not family_mode):
                continue
            if now - self._fired.get(m.id, float("-inf")) < m.cooldown_s:
                continue
            for p in m.phrases:
                n = len(normalize(p))
                if n > best.get(m.id, (0, m))[0] and phrase_in(p, text):
                    best[m.id] = (n, m)
        if not best:
            return None
        top = max(n for n, _ in best.values())
        return self.rng.choice([m for n, m in best.values() if n == top])

    def fired(self, meme_id: str) -> None:
        now = self.clock()
        self._fired[meme_id] = now
        self._last = now
