"""Current play session: which game and section (town/route) captures belong to.

Sections become Anki subdecks numbered in play order ("01 Pallet Town",
"02 Route 1", ...) so the deck list reads like the playthrough. Menu and
battle screens are routed to fixed sections instead of the current one.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path

from .config import APP_ROOT

SESSION_PATH = APP_ROOT / "data" / "session.json"

MENU_SECTION = "Menus"
BATTLE_SECTION = "Battles"
_NUMBERED = re.compile(r"^(\d+) (.+)$")


def load() -> dict:
    if SESSION_PATH.exists():
        return json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    return {"source": None, "section": None, "sections": {}}


def save(state: dict) -> None:
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def set_section(name: str, source: str | None = None) -> dict:
    """Make `name` the current section, numbering it if it's new for this source.

    Re-entering a section you've already visited reuses its number.
    """
    state = load()
    source = source or state.get("source")
    if not source:
        raise ValueError("No game set yet: pass --source the first time")
    known: list[str] = state.setdefault("sections", {}).setdefault(source, [])

    bare = _NUMBERED.match(name).group(2) if _NUMBERED.match(name) else name
    existing = next((s for s in known if _NUMBERED.match(s).group(2).lower() == bare.lower()), None)
    if existing:
        section = existing
    else:
        section = f"{len(known) + 1:02d} {bare}"
        known.append(section)

    state.update(source=source, section=section)
    save(state)
    return state


def set_source(name: str, known: list[str] = ()) -> dict:
    """Switch game ("Hey ARIA, we're playing Enshrouded"), back to that game's last section.

    `name` is what Whisper heard, so it snaps to a game we already know (this
    session's games, plus `known`, e.g. series.yaml) when it's close.
    """
    state = load()
    source = match_source(name, [*state.get("sections", {}), *known]) or name
    sections = state.get("sections", {}).get(source) or []
    state.update(source=source, section=sections[-1] if sections else None)
    save(state)
    return state


def match_source(name: str, known: list[str]) -> str | None:
    """The known game `name` most likely means ("in shrouded" -> "Enshrouded"), or None."""
    def key(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    scored = [(SequenceMatcher(None, key(name), key(k)).ratio(), k) for k in known if key(k)]
    ratio, best = max(scored, default=(0.0, None))
    return best if ratio >= 0.8 else None


def section_for(screen: str | None, state: dict) -> str | None:
    """Pick the section for a screenshot from its screen type."""
    if screen == "menu":
        return MENU_SECTION
    if screen == "battle":
        return BATTLE_SECTION
    return state.get("section")
