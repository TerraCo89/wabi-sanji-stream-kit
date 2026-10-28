"""Spot "Hey ARIA, ..." in a Whisper utterance and work out what was asked.

Whisper spells the name a few ways ("Aria", "Arya", "Area"), so the wake word
list is configurable. "area" is a normal English word: it only counts after
"hey"/"okay"/"hi". The other spellings also count at the very start of an
utterance ("Aria, more."), which is how the follow-up commands get said.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

COMMON_WORDS = {"area"}  # real words: need the "hey" prefix

# Follow-up commands handled locally, without a Claude round-trip. Matched
# against the whole remainder after the wake word, so "how do I get more
# ammo" is still a question.
COMMANDS = {
    "more": ["more", "tell me more", "full answer", "go on", "keep going", "next", "and then"],
    "clear": ["never mind", "nevermind", "clear", "cancel", "thanks", "thank you", "cheers",
              "got it", "dismiss", "close", "stop", "stop that", "stop it", "stop searching",
              "stop looking", "forget it", "forget that"],
    "repeat": ["repeat", "repeat that", "say that again", "again", "what was that"],
    "memes_off": ["memes off", "no memes", "stop memes", "stop the memes", "turn memes off"],
    "memes_on": ["memes on", "turn memes on", "start memes", "memes back on"],
    "family_on": ["family mode", "family mode on", "kids are here", "kid mode", "kid mode on"],
    "family_off": ["family mode off", "kid mode off", "kids are gone", "adults only"],
}


# "Hey ARIA, we're playing Enshrouded" / "switch game to ..." / "the game is ..."
GAME = re.compile(
    r"^(?:(?:we're|we are|i'm|i am|now|we're now)\s+)?playing\s+(?P<a>.+)$"
    r"|^(?:switch|change|set)\s+(?:the\s+)?game\s+to\s+(?P<b>.+)$"
    r"|^(?:the\s+)?game\s+is\s+(?P<c>.+)$",
    re.IGNORECASE,
)
QUESTION_WORDS = re.compile(r"\b(how|what|where|why|when|which|who)\b", re.IGNORECASE)

# Said without "Hey ARIA", these only cancel while a question is being looked up
# (the listener keeps that window), so "stop that" to the dog an hour later is ignored.
BARE_STOP = {"stop that", "stop searching", "stop looking"}


@dataclass
class Wake:
    command: str  # question | listen | more | clear | repeat | memes_on/off | family_on/off | game
    text: str = ""  # the question, or the game for command == "game"


def build_pattern(words: list[str]) -> re.Pattern:
    names = sorted({w.lower() for w in words}, key=len, reverse=True)
    proper = [n for n in names if n not in COMMON_WORDS]
    any_name = "|".join(map(re.escape, names))
    start_name = "|".join(map(re.escape, proper)) or "(?!)"
    return re.compile(
        rf"(?:(?P<hey>\b(?:hey|hi|ok(?:ay)?|yo)[\s,.!-]+(?:{any_name})\b)|^\W*(?:{start_name})\b)"
        r"[\s,.!?:;-]*",
        re.IGNORECASE,
    )


def parse(text: str, pattern: re.Pattern) -> Wake | None:
    """None if the utterance doesn't address ARIA."""
    m = pattern.search(text)
    if not m:
        return None
    rest = text[m.end():].strip()
    if not m.group("hey") and not rest:
        # A lone "Aria." is what Whisper invents from a mumble; only "Hey ARIA" waits
        return None
    return classify(rest)


def classify(rest: str) -> Wake:
    """What the words after the wake word ask for."""
    bare = _normalize(rest)
    if not bare:
        return Wake("listen")
    for command, phrases in COMMANDS.items():
        if bare in phrases or bare.removeprefix("please ").removesuffix(" please") in phrases:
            return Wake(command)
    m = GAME.match(rest.strip(" ,.!?-"))
    if m:
        game = next(g for g in m.groups() if g).strip(" ,.!?-")
        game = re.sub(r"\s+now$", "", game, flags=re.IGNORECASE)
        # "we're playing X, how do I ..." is still a question
        if game and not QUESTION_WORDS.search(game):
            return Wake("game", game)
    return Wake("question", rest.strip(" ,.-"))


def is_bare_stop(text: str) -> bool:
    """A whole utterance that cancels a search without the wake word ("Stop that.")."""
    bare = _normalize(text)
    return bare in BARE_STOP or bare.removesuffix(" please") in BARE_STOP


def _normalize(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s']", " ", text.lower()).split())
