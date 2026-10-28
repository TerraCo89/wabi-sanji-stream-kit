"""Transcript helpers (no GPU needed)."""
from __future__ import annotations

from wabisanji_post.models import Segment
from wabisanji_post.transcribe import default_prompt, readable_lines


def w(text, s, e=None):
    return {"w": text, "s": s, "e": s + 0.3 if e is None else e}


def test_lines_break_on_long_pause_and_use_hms():
    words = [w("Ashita.", 2487.0), w("That's", 2488.0), w("tomorrow.", 2488.4), w("Ah,", 2491.5), w("sweet.", 2492.0)]
    assert readable_lines(words) == [
        "[00:41:27] Ashita. That's tomorrow.",
        "[00:41:31] Ah, sweet.",
    ]


def test_short_sentences_are_merged_until_min_words():
    words = [w(t, 10 + i * 0.5) for i, t in enumerate("So. We could do. Right click. Properties. Refresh browser now.".split())]
    lines = readable_lines(words, min_words=8)
    assert lines == ["[00:00:10] So. We could do. Right click. Properties. Refresh browser now."]


def test_max_words():
    words = [w(f"w{i}", i * 0.4) for i in range(30)]
    lines = readable_lines(words, max_words=25)
    assert len(lines) == 2 and lines[0].count(" ") == 25


def test_japanese_sentence_end():
    words = [w(t, i * 0.4) for i, t in enumerate(["この", "砂漠に", "竜が", "いる", "とは", "聞いて", "いた", "が。", "next"])]
    assert readable_lines(words)[0].endswith("が。")


def test_default_prompt():
    seg = Segment(id=2, type="japanese", game="Breath of Fire IV (JP)",
                  series="Learning Japanese with Breath of Fire IV", start=900, end=3110)
    assert default_prompt(seg) == (
        "Breath of Fire IV (JP), Learning Japanese with Breath of Fire IV. Wabi Sanji stream, Twitch chat."
    )
    same = Segment(id=3, type="gaming", game="Alchemy Factory", series="Alchemy Factory", start=3120, end=8780)
    assert default_prompt(same) == "Alchemy Factory. Wabi Sanji stream, Twitch chat."
