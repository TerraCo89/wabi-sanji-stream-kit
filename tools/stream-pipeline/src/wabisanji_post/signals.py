"""Moment signals for /stream-moments (spec: "Moment signals").

  signals/vocab.json      vocab captures from the parser DB (read-only), as recording offsets,
                          each with where the word was heard in the transcript (romaji match)
  signals/loudness.json   short-term loudness peaks per segment (ebur128 on audio_track)
  signals/markers.json    Twitch stream markers, or why they were skipped
  signals/chat.json       chat from the recorder's archive: messages, subs/raids, bursts and
                          chatters per segment (moderator-deleted messages left out)

All times are recording offsets in seconds, plus an ``hms`` string for reading.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import statistics
import subprocess
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz

from . import settings
from .models import Segment, Session, fmt_tc


DEFAULT_DB = settings.parser_dir() / "data" / "wabisanji.db"


def _hms(t: float) -> str:
    return fmt_tc(round(t, 1))


# ---------------------------------------------------------------------------
# Screenshot capture times
# ---------------------------------------------------------------------------

# OBS: "Screenshot 2026-10-05 11-28-16.png"; RetroArch: "<game>-261004-121323.png"
_OBS_SHOT = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})")
_RA_SHOT = re.compile(r"-(\d{2})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$")


def capture_time(file_path: str, processed_at: Optional[str], tz: dt.tzinfo) -> tuple[dt.datetime, str]:
    """When a screenshot was taken, and how we know (``filename`` or ``processed_at``).

    Filenames carry local time to the second. ``processed_at`` (UTC) is a
    fallback only: it is when the parser ingested the shot, which can lag.
    """
    stem = Path(file_path.replace("\\", "/")).stem
    m = _OBS_SHOT.search(stem)
    if m:
        return dt.datetime(*map(int, m.groups()), tzinfo=tz), "filename"
    m = _RA_SHOT.search(stem)
    if m:
        y, mo, d, h, mi, s = map(int, m.groups())
        return dt.datetime(2000 + y, mo, d, h, mi, s, tzinfo=tz), "filename"
    if processed_at:
        utc = dt.datetime.fromisoformat(processed_at).replace(tzinfo=dt.timezone.utc)
        return utc.astimezone(tz), "processed_at"
    raise ValueError(f"no capture time for {file_path}")


# ---------------------------------------------------------------------------
# Kana → romaji (Hepburn-ish, enough to match Whisper's romaji)
# ---------------------------------------------------------------------------

_KANA = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "ゐ": "i", "ゑ": "e", "を": "o", "ん": "n",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o", "ゔ": "vu",
}
_YOON = {"ゃ": "a", "ゅ": "u", "ょ": "o"}


def to_romaji(kana: str) -> str:
    """Hiragana/katakana → lowercase romaji. Unknown characters are dropped."""
    hira = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in kana)
    out: list[str] = []
    double_next = False
    for c in hira:
        if c == "っ":
            double_next = True
            continue
        if c == "ー":
            if out and out[-1]:
                out.append(out[-1][-1])
            continue
        if c in _YOON and out:
            prev = out.pop()
            stem = prev[:-1]  # き → k, し → sh, ち → ch, じ → j (っち → tch)
            if stem.endswith(("sh", "ch", "j")):
                out.append(stem + _YOON[c])
            else:
                out.append(stem + "y" + _YOON[c])
            continue
        r = _KANA.get(c)
        if r is None:
            continue
        if double_next:
            r = ("t" if r.startswith("ch") else r[0]) + r
            double_next = False
        out.append(r)
    return "".join(out)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def find_heard(romaji: str, words: list[dict], t: float, window: float, min_score: int = 85) -> list[dict]:
    """Transcript spots within ±window s of t where the romaji reading was said.

    Compares against single words and runs of 2-3 words joined, since Whisper
    splits readings like "Koko Made" or "Deijoubu desu".
    """
    target = _norm(romaji)
    if len(target) < 3:
        return []
    if len(target) <= 4:  # short readings fuzzy-match anything ("nai" ~ "nari"): exact only
        min_score = 100
    near = [w for w in words if abs(w["s"] - t) <= window]
    hits: list[dict] = []
    for i in range(len(near)):
        for n in (1, 2, 3):
            chunk = near[i:i + n]
            if len(chunk) < n:
                break
            said = _norm("".join(w["w"] for w in chunk))
            if not said or abs(len(said) - len(target)) > max(2, len(target) // 3):
                continue
            score = fuzz.ratio(said, target)
            if score >= min_score:
                hits.append({"t": chunk[0]["s"], "hms": _hms(chunk[0]["s"]),
                             "said": " ".join(w["w"] for w in chunk), "score": round(score)})
    best: dict[float, dict] = {}
    for h in hits:  # one hit per spot, best score
        if h["t"] not in best or h["score"] > best[h["t"]]["score"]:
            best[h["t"]] = h
    return sorted(best.values(), key=lambda h: h["t"])


# ---------------------------------------------------------------------------
# Vocab captures
# ---------------------------------------------------------------------------

def vocab_signals(session: Session, transcripts: dict[int, list[dict]], db: Path = DEFAULT_DB,
                  heard_window: float = 120.0) -> dict:
    """Captures from the parser DB that fall inside this recording, grouped per screenshot."""
    if not db.exists():
        return {"status": "skipped", "reason": f"no parser DB at {db}", "captures": []}
    tz = session.recording_start.tzinfo
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)  # read-only: never write the capture log
    con.row_factory = sqlite3.Row
    shots = con.execute(
        """SELECT s.id, s.file_path, s.processed_at, s.section, s.speaker, s.translation,
                  s.extracted_text_json, so.name AS source
           FROM screenshots s LEFT JOIN sources so ON so.id = s.source_id
           ORDER BY s.id"""
    ).fetchall()
    captures = []
    for s in shots:
        try:
            taken, how = capture_time(s["file_path"] or "", s["processed_at"], tz)
        except ValueError:
            continue
        t = (taken - session.recording_start).total_seconds()
        if not (0 <= t < session.duration):
            continue
        seg = session.segment_at(t)
        vocab = con.execute(
            """SELECT v.word, v.reading, v.meaning, v.jlpt_level, v.category, v.first_seen_at,
                      sv.form, sv.sentence
               FROM screenshot_vocabulary sv JOIN vocabulary v ON v.id = sv.vocabulary_id
               WHERE sv.screenshot_id = ? ORDER BY v.id""",
            (s["id"],),
        ).fetchall()
        words = transcripts.get(seg.id, []) if seg else []
        text = " / ".join(x.get("text", "") for x in json.loads(s["extracted_text_json"] or "[]")
                          if isinstance(x, dict)) or None
        items = []
        for v in vocab:
            romaji = to_romaji(v["reading"] or v["word"])
            items.append({
                "word": v["word"], "reading": v["reading"], "romaji": romaji, "meaning": v["meaning"],
                "form": v["form"] or None, "jlpt": v["jlpt_level"], "category": v["category"],
                "heard": find_heard(romaji, words, t, heard_window),
            })
        captures.append({
            "t": round(t, 1), "hms": _hms(t), "time_from": how,
            "segment": seg.id if seg else None,
            "screenshot_id": s["id"], "file": s["file_path"], "source": s["source"],
            "section": s["section"], "speaker": s["speaker"],
            "text": text, "translation": s["translation"], "vocab": items,
        })
    con.close()
    return {"status": "ok", "db": str(db), "count": len(captures), "captures": captures}


# ---------------------------------------------------------------------------
# Loudness peaks
# ---------------------------------------------------------------------------

_EBUR = re.compile(r"t:\s*([\d.]+)\s+TARGET:.*?\bS:\s*(-?[\d.]+|-inf)")


def short_term_loudness(rec: Path, start: float, duration: float, audio_track: int) -> list[tuple[float, float]]:
    """(recording offset, short-term LUFS) once per second across ``start`` .. ``start + duration``."""
    proc = subprocess.run(
        ["ffmpeg", "-nostats", "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
         "-i", str(rec), "-map", f"0:a:{audio_track}", "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg ebur128 failed: {proc.stderr[-500:]}")
    out: list[tuple[float, float]] = []
    next_t = 0.0
    for m in _EBUR.finditer(proc.stderr):
        t = float(m.group(1))
        if t + 1e-6 < next_t or m.group(2) == "-inf":
            continue
        out.append((round(start + t, 1), float(m.group(2))))
        next_t = t + 1.0
    return out


def loudness_peaks(series: list[tuple[float, float]], *, above: float = 6.0, spacing: float = 20.0,
                   top: int = 40) -> tuple[float, list[dict]]:
    """Local maxima at least ``above`` LU over the segment median, ``spacing`` s apart."""
    values = [v for _, v in series if v > -70]
    if not values:
        return float("nan"), []
    median = statistics.median(values)
    cands = sorted((p for p in series if p[1] >= median + above), key=lambda p: -p[1])
    kept: list[tuple[float, float]] = []
    for t, v in cands:
        if all(abs(t - k) >= spacing for k, _ in kept):
            kept.append((t, v))
        if len(kept) >= top:
            break
    peaks = [{"t": t, "hms": _hms(t), "lufs": round(v, 1), "above_median": round(v - median, 1)}
             for t, v in sorted(kept)]
    return round(median, 1), peaks


def loudness_signals(session: Session, rec: Path, segments: list[Segment], **kw) -> dict:
    out = {"status": "ok", "audio_track": session.audio_track,
           "note": "audio_track 0 is the full mix, so game sound effects and music spikes count too",
           "segments": []}
    for seg in segments:
        series = [x for a, b in seg.parts for x in short_term_loudness(rec, a, b - a, session.audio_track)]
        median, peaks = loudness_peaks(series, **kw)
        out["segments"].append({"segment": seg.id, "median_lufs": median, "peaks": peaks})
    return out


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

def marker_signals(session: Session) -> dict:
    """Twitch stream markers as recording offsets. Needs the VOD (14 days) and `stream-auth twitch`."""
    if not session.twitch_vod_id:
        return {"status": "skipped", "reason": "no twitch_vod_id in session.yaml", "markers": []}
    from . import twitch

    try:
        helix = twitch.Helix()
        vod = helix.video(session.twitch_vod_id)
        if vod is None:
            return {"status": "skipped", "reason": f"VOD {session.twitch_vod_id} is gone", "markers": []}
        vod_start = (twitch.vod_created_at(vod) - session.recording_start).total_seconds()
        markers = helix.markers(session.twitch_vod_id)
    except (twitch.TwitchError, RuntimeError) as exc:
        return {"status": "skipped", "reason": str(exc), "markers": []}
    out = []
    for m in markers:
        t = float(m.get("position_seconds", 0)) + vod_start
        seg = session.segment_at(t)
        out.append({"t": round(t, 1), "hms": _hms(t), "segment": seg.id if seg else None,
                    "description": m.get("description") or ""})
    return {"status": "ok", "vod_start": round(vod_start, 1), "markers": sorted(out, key=lambda m: m["t"])}


# ---------------------------------------------------------------------------
# Chat (the recorder's archive)
# ---------------------------------------------------------------------------

_TAG_ESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}


def parse_irc(line: str) -> Optional[dict]:
    """``{tags, prefix, command, params}`` for one raw Twitch IRC line (same rules as the recorder)."""
    tags: dict[str, str] = {}
    rest = line
    if rest.startswith("@"):
        head, _, rest = rest.partition(" ")
        for pair in head[1:].split(";"):
            k, eq, v = pair.partition("=")
            tags[k] = re.sub(r"\\(.?)", lambda m: _TAG_ESCAPES.get(m.group(1), m.group(1)), v) if eq else ""
    rest = rest.lstrip(" ")
    prefix = ""
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")
    trailing = None
    if " :" in rest:
        rest, _, trailing = rest.partition(" :")
    parts = rest.split()
    if not parts:
        return None
    params = parts[1:] + ([trailing] if trailing is not None else [])
    return {"tags": tags, "prefix": prefix, "command": parts[0], "params": params}


def chat_signals(session: Session, lines: list[dict], *, bucket: float = 60.0) -> dict:
    """Chat as recording offsets. ``lines`` are ``[{t: datetime, line: raw IRC}]`` from
    :func:`recorder.chat_lines`. A message's time is Twitch's ``tmi-sent-ts`` when present."""
    t0 = session.recording_start
    messages: dict[str, dict] = {}
    events: list[dict] = []
    for row in lines:
        msg = parse_irc(row["line"])
        if not msg:
            continue
        tags, cmd = msg["tags"], msg["command"]
        sent = tags.get("tmi-sent-ts")
        when = (dt.datetime.fromtimestamp(int(sent) / 1000, dt.timezone.utc) if sent and sent.isdigit()
                else row["t"])
        t = (when - t0).total_seconds()
        if cmd == "CLEARMSG":
            messages.pop(tags.get("target-msg-id", ""), None)
            continue
        if cmd == "CLEARCHAT":
            uid = tags.get("target-user-id")
            for k in [k for k, m in messages.items() if not uid or m["user_id"] == uid]:
                messages.pop(k)
            continue
        if not 0 <= t <= session.duration:
            continue
        seg = session.segment_at(t)
        base = {"t": round(t, 1), "hms": _hms(t), "segment": seg.id if seg else None,
                "user": tags.get("display-name") or msg["prefix"].split("!")[0]}
        if cmd == "PRIVMSG":
            messages[tags.get("id") or f"{len(messages)}"] = {
                **base, "user_id": tags.get("user-id", ""), "text": msg["params"][-1] if msg["params"] else "",
                "broadcaster": "broadcaster/" in tags.get("badges", "")}
        elif cmd == "USERNOTICE":
            events.append({**base, "type": tags.get("msg-id", ""), "text": tags.get("system-msg", "")})
    msgs = sorted(messages.values(), key=lambda m: m["t"])

    per_segment, bursts = [], []
    for seg in session.segments:
        if seg.type == "skip":
            continue
        mine = [m for m in msgs if m["segment"] == seg.id]
        viewers_msgs = [m for m in mine if not m["broadcaster"]]
        counts: dict[int, int] = {}
        for m in viewers_msgs:
            k = int(m["t"] // bucket)
            counts[k] = counts.get(k, 0) + 1
        hours = seg.duration / 3600 or 1e-9
        per_segment.append({"segment": seg.id, "game": seg.game or seg.label, "messages": len(viewers_msgs),
                            "chatters": len({m["user_id"] or m["user"] for m in viewers_msgs}),
                            "messages_per_hour": round(len(viewers_msgs) / hours, 1)})
        if counts:  # baseline: every minute of the segment, quiet ones included
            span = range(int(seg.start // bucket), int(seg.end // bucket) + 1)
            floor = max(3, 2 * statistics.median(counts.get(k, 0) for k in span))
            for k, n in sorted(counts.items()):
                if n >= floor:
                    bursts.append({"t": k * bucket, "hms": _hms(k * bucket), "segment": seg.id, "count": n})
    return {"status": "ok", "count": len(msgs), "per_segment": per_segment, "bursts": bursts,
            "events": events, "messages": [{k: v for k, v in m.items() if k != "user_id"} for m in msgs]}
