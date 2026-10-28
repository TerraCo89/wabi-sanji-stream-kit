"""Backlog inventory (spec: "Backlog", Phase 7): every recording, classified, in ``sessions/_backlog.yaml``.

Sources of truth, best first:

1. **Twitch VODs** (Helix Get Videos). A recording that overlaps a VOD was
   streamed; the VOD title is a hint for segmenting. Files that aren't OBS
   recordings (e.g. ``WabiSanji/Alchemy Factory 1.mp4``, VODs downloaded and
   remuxed) are matched by duration instead and take the VOD's start time.
2. **Size / length.** Under ~0.2 GB or 5 min: a false start.
3. **No VOD** for a real-length recording: a local test (VODs reach back past
   the whole backlog, checked 2026-10-05).

Recordings already in a session (``recording`` or ``excluded_files``) are listed
as such. The streamer confirms by editing ``class`` / setting ``confirmed: true``; a rerun
keeps those edits.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional

import yaml

from . import sessions, settings
from .models import fmt_short
from .signals import DEFAULT_DB, capture_time

BACKLOG_YAML = "_backlog.yaml"
FALSE_START_BYTES = 200_000_000
FALSE_START_SECONDS = 5 * 60
SPLIT_GAP_SECONDS = 30 * 60  # next file starts within 30 min of the last ending
CLASSES = ("stream", "local_test", "false_start", "in_session", "excluded")


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags=creation_time,encoder",
         "-show_entries", "stream=codec_type", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8",
    )
    if out.returncode != 0:
        return {"duration": None, "audio_tracks": 0, "obs": False}
    d = json.loads(out.stdout)
    fmt = d.get("format", {})
    tags = fmt.get("tags", {})
    return {
        "duration": float(fmt["duration"]) if fmt.get("duration") else None,
        "audio_tracks": sum(1 for s in d.get("streams", []) if s.get("codec_type") == "audio"),
        "obs": "OBS" in (tags.get("encoder") or ""),
    }


def parse_duration(text: str) -> float:
    """Twitch ``1h2m3s`` → seconds."""
    total, num = 0.0, ""
    for ch in text:
        if ch.isdigit() or ch == ".":
            num += ch
        else:
            total += float(num or 0) * {"h": 3600, "m": 60, "s": 1}.get(ch, 0)
            num = ""
    return total


def recording_files(recordings_root: Path) -> list[Path]:
    """Video files in the recordings root and ``WabiSanji/`` only (never other folders)."""
    files = [p for p in recordings_root.glob("*") if p.is_file() and p.suffix.lower() in (".mkv", ".mp4")]
    files += [p for p in (recordings_root / "WabiSanji").glob("*") if p.is_file() and p.suffix.lower() in (".mkv", ".mp4")]
    return sorted(files)


def _session_refs(root: Path) -> dict[str, tuple[str, str]]:
    """lower-cased path → (kind, session id)."""
    refs: dict[str, tuple[str, str]] = {}
    for yml in root.glob(f"*/{sessions.SESSION_YAML}"):
        data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
        sid = str(data.get("session") or yml.parent.name)
        if data.get("recording"):
            refs[str(Path(data["recording"])).lower()] = ("in_session", sid)
        for ex in data.get("excluded_files") or []:
            refs[str(Path(ex["file"])).lower()] = ("excluded", f"{sid}: {ex.get('reason', '')}")
    return refs


def _captures(db: Path) -> list[tuple[dt.datetime, str]]:
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    rows = con.execute("SELECT s.file_path, s.processed_at, so.name FROM screenshots s "
                       "LEFT JOIN sources so ON so.id = s.source_id").fetchall()
    con.close()
    local = dt.datetime.now().astimezone().tzinfo
    out = []
    for path, processed, source in rows:
        try:
            # filename times are local wall-clock; attach this machine's zone (DST-aware via astimezone)
            t, _ = capture_time(path or "", processed, local)
            out.append((t.replace(tzinfo=None).astimezone(), source or "?"))
        except ValueError:
            continue
    return out


def inventory(recordings_root: Path, root: Path, vods: list[dict], db: Path = DEFAULT_DB) -> list[dict]:
    refs = _session_refs(root)
    caps = _captures(db)
    vod_rows = [{
        "id": v["id"], "type": v["type"], "title": v["title"],
        "start": dt.datetime.fromisoformat(v["created_at"].replace("Z", "+00:00")),
        "duration": parse_duration(v["duration"]),
    } for v in vods if v["type"] == "archive"]
    items: list[dict] = []
    claimed: set[str] = set()

    def obs_named(p: Path) -> bool:
        try:
            sessions.parse_obs_filename(p.name)
            return True
        except ValueError:
            return False

    # OBS recordings claim their VODs by time first; downloaded VODs then match by length.
    for f in sorted(recording_files(recordings_root), key=lambda p: (not obs_named(p), str(p))):
        info = probe(f)
        dur = info["duration"] or 0.0
        size = f.stat().st_size
        item = {"file": str(f), "size_gb": round(size / 1e9, 2), "duration": fmt_short(dur),
                "audio_tracks": info["audio_tracks"]}
        start: Optional[dt.datetime] = None
        start_from = None
        try:
            start = sessions.parse_obs_filename(f.name)
            start_from = "filename"
        except ValueError:
            pass
        vod = None
        if start is not None:  # overlap with a VOD
            end = start + dt.timedelta(seconds=dur)
            best = 0.0
            for v in vod_rows:
                ov = (min(end, v["start"] + dt.timedelta(seconds=v["duration"])) - max(start, v["start"])).total_seconds()
                if ov > best:
                    best, vod = ov, v
            if vod and best < min(60.0, 0.5 * min(dur, vod["duration"]) or 60.0):
                vod = None
        else:  # downloaded VOD: match on duration, among VODs no OBS recording claimed
            close = [v for v in vod_rows if abs(v["duration"] - dur) <= 2.0 and v["id"] not in claimed]
            if len(close) == 1:
                vod = close[0]
                start, start_from = vod["start"].astimezone(), "vod (duration match)"
        item["start"] = start.isoformat() if start else None
        item["start_from"] = start_from
        if vod:
            claimed.add(vod["id"])
            item["vod"] = {"id": vod["id"], "title": vod["title"], "duration": fmt_short(vod["duration"])}
        if start:
            end = start + dt.timedelta(seconds=dur)
            inside = [src for t, src in caps if start <= t <= end]
            if inside:
                item["vocab_captures"] = {src: inside.count(src) for src in sorted(set(inside))}

        ref = refs.get(str(f).lower())
        if ref:
            item["class"], item["reason"] = ref[0], f"session {ref[1]}" if ref[0] == "in_session" else ref[1]
        elif size < FALSE_START_BYTES or dur < FALSE_START_SECONDS:
            item["class"] = "false_start"
            item["reason"] = (f"{item['size_gb']} GB, {item['duration']}"
                              + (f"; streamed briefly (VOD {vod['duration']})" if vod else "; no VOD"))
        elif vod:
            item["class"], item["reason"] = "stream", f"matches VOD {vod['id']}"
        elif start is None:
            item["class"], item["reason"] = "local_test", "no OBS timestamp and no VOD of the same length"
        else:
            item["class"], item["reason"] = "local_test", "no Twitch VOD overlaps it"
        items.append(item)

    # Split streams: consecutive streamed files close together on the same VOD title.
    items.sort(key=lambda i: i["start"] or "9999")
    prev = None
    for it in items:
        if prev and it["class"] == prev["class"] == "stream" and it["start"] and prev["start"]:
            gap = (dt.datetime.fromisoformat(it["start"]) - dt.datetime.fromisoformat(prev["start"])).total_seconds() \
                - _seconds(prev["duration"])
            if gap < SPLIT_GAP_SECONDS and it.get("vod", {}).get("title") == prev.get("vod", {}).get("title"):
                it["split_from"] = Path(prev["file"]).name
        prev = it

    # Suggested session ids (date, then _2, _3 … in start order), skipping ids already used.
    used = {p.name for p in root.iterdir() if p.is_dir()} if root.exists() else set()
    for it in items:
        if it["class"] != "stream" or not it["start"]:
            continue
        day = it["start"][:10]
        sid, n = day, 2
        while sid in used:
            sid, n = f"{day}_{n}", n + 1
        used.add(sid)
        it["session_id"] = sid
    for it in items:
        it.setdefault("confirmed", False)
    return items


def _seconds(hms: str) -> float:
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


def merge_previous(items: list[dict], path: Path) -> list[dict]:
    """Keep the streamer's ``class`` / ``confirmed`` / ``notes`` / ``session_id`` edits from an earlier run."""
    if not path.exists():
        return items
    old = {o["file"]: o for o in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("recordings", [])}
    for it in items:
        o = old.get(it["file"])
        if o and o.get("confirmed"):
            for k in ("class", "session_id", "notes", "confirmed"):
                if k in o:
                    it[k] = o[k]
        elif o and o.get("notes"):
            it["notes"] = o["notes"]
    return items


def write_backlog(path: Path, items: list[dict]) -> None:
    head = (f"# Backlog of stream recordings (stream-inventory). {settings.streamer_name()}: check `class` for each recording\n"
            "# (stream | local_test | false_start), fix any that are wrong, add `notes` if useful and set\n"
            "# `confirmed: true`. Reruns keep confirmed entries. Only `stream` recordings get sessions.\n")
    body = yaml.safe_dump({"generated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                           "recordings": items}, sort_keys=False, allow_unicode=True, width=140)
    path.write_text(head + body, encoding="utf-8")
