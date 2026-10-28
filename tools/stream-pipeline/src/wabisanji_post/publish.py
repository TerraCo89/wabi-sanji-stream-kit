"""Publishing (spec: "Publishing"): episode numbers, the Studio checklist, Twitch clips, Discord.

Everything created is recorded in ``publish.yaml`` in the session folder and
checked first, so a rerun never re-uploads, re-clips or re-posts.

Order: YouTube (manual upload in Studio from ``upload-checklist.md``; the streamer
pastes the URLs back), Twitch clips from the VOD, then Discord posts last,
because a post links the episode on YouTube. Discord gets the long-form
episodes only (the streamer, 2026-10-14): Shorts and clips spammed a channel that
followers already follow, so a clip's ``discord`` target is ignored.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests
import yaml
from pydantic import BaseModel, ConfigDict, Field

from . import secrets, sessions
from .models import Clip, ClipsFile, Session, fmt_short, fmt_tc
from . import settings
from .vods import SERIES_YAML, cut_info, episode_label, load_series, vod_path

PUBLISH_YAML = "publish.yaml"
CHECKLIST_MD = "upload-checklist.md"
DISCORD_NAME = settings.channel_name()
DRIFT_WARN_SECONDS = 2.0
TRUSTED_OFFSET = ("frames", "manual")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# publish.yaml
# ---------------------------------------------------------------------------

class YouTubeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    video_id: str
    recorded_at: str


class TwitchClipRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    edit_url: str
    url: Optional[str] = None
    title: str
    requested_start: float  # recording offset
    requested_end: float
    vod_offset_end: int  # sent to Twitch (where the clip ends in the VOD)
    duration: float
    actual_vod_offset: Optional[int] = None  # where Twitch says it starts in the VOD
    drift: Optional[float] = None  # actual - requested start, seconds
    created_at: str


class DiscordRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str
    channel_id: str
    content: str
    posted_at: str


class InstagramRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media_id: str
    permalink: str = ""
    container_id: Optional[str] = None  # None when found already on the account
    posted_at: str


class PublishLog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str
    twitch_vod_id: Optional[str] = None
    twitch_vod_start: Optional[float] = None  # recording offset of VOD 0:00
    # "frames" (stream-vod-offset) or "manual" (--vod-offset). Anything else, e.g. the VOD's
    # created_at (5 s late on 2026-10-05), is not trusted for clip times.
    twitch_vod_start_method: Optional[str] = None
    twitch_vod_start_samples: list[dict] = Field(default_factory=list)
    youtube: dict[str, YouTubeRecord] = Field(default_factory=dict)  # "seg2", "c01"
    twitch_clips: dict[str, TwitchClipRecord] = Field(default_factory=dict)
    discord: dict[str, DiscordRecord] = Field(default_factory=dict)  # "seg2" (older sessions: "c01")
    instagram: dict[str, InstagramRecord] = Field(default_factory=dict)  # "c01"
    notes: list[str] = Field(default_factory=list)


def load_log(session_dir: Path, session: Session) -> PublishLog:
    p = session_dir / PUBLISH_YAML
    if p.exists():
        return PublishLog.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))
    return PublishLog(session=session.session, twitch_vod_id=session.twitch_vod_id)


def save_log(session_dir: Path, log: PublishLog) -> None:
    body = yaml.safe_dump(log.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True, width=120)
    (session_dir / PUBLISH_YAML).write_text(
        "# Written by stream-publish. Every ID here is done: reruns skip it. Don't edit by hand.\n" + body,
        encoding="utf-8",
    )


_YT_ID = re.compile(r"(?:youtube\.com/(?:shorts/|watch\?v=|live/)|youtu\.be/)([\w-]{11})")


def youtube_video_id(url: str) -> str:
    m = _YT_ID.search(url)
    if not m:
        raise ValueError(f"not a YouTube video URL: {url}")
    return m.group(1)


# ---------------------------------------------------------------------------
# Episode numbering (stream-number)
# ---------------------------------------------------------------------------

@dataclass
class EpisodeRef:
    series: str
    session: str
    segment: int
    starts: dt.datetime  # wall-clock start of the segment

    def key(self) -> tuple[str, int]:
        return (self.session, self.segment)


def collect_episodes(root: Path) -> tuple[dict[str, list[EpisodeRef]], list[str]]:
    """Publishable segments of every approved session, grouped by series, in stream order."""
    by_series: dict[str, list[EpisodeRef]] = {}
    problems: list[str] = []
    for yml in sorted(root.glob(f"*/{sessions.SESSION_YAML}")):
        try:
            s = sessions.load_session(yml.parent)
        except Exception as exc:  # report, keep going
            problems.append(f"{yml.parent.name}: invalid session.yaml ({str(exc).splitlines()[0]})")
            continue
        if not s.at_least("segments_approved"):
            continue
        for seg in s.segments:
            if not seg.publishable or seg.full_of is not None:  # a full version shares its segment's number
                continue
            if not seg.series:
                problems.append(f"{s.session} segment {seg.id}: no series")
                continue
            by_series.setdefault(seg.series, []).append(
                EpisodeRef(seg.series, s.session, seg.id, s.recording_start + dt.timedelta(seconds=seg.start)))
    for eps in by_series.values():
        eps.sort(key=lambda e: e.starts)
    return by_series, problems


def unsegmented_recordings(root: Path, recordings_root: Path) -> list[Path]:
    """Recordings (read-only listing) that no session.yaml points at or excludes."""
    known: set[str] = set()
    for yml in root.glob(f"*/{sessions.SESSION_YAML}"):
        data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
        if data.get("recording"):
            known.add(str(Path(data["recording"])).lower())
        for ex in data.get("excluded_files") or []:
            known.add(str(Path(ex["file"])).lower())
    files = [p for p in recordings_root.glob("*") if p.suffix.lower() in (".mkv", ".mp4") and p.is_file()]
    files += [p for p in (recordings_root / "WabiSanji").glob("*.mp4") if p.is_file()]
    return sorted(p for p in files if str(p).lower() not in known)


def write_episodes(path: Path, episodes: dict[str, list[dict]]) -> None:
    """Rewrite only the ``episodes:`` list of each named series, keeping comments."""
    lines = path.read_text(encoding="utf-8").split("\n")
    out: list[str] = []
    current = None
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^\s*- name:\s*(.+?)\s*$", line)
        if m:
            current = str(yaml.safe_load(m.group(1)))  # unquote "Star Wars: Zero Company", drop comments
        m = re.match(r"^(\s*)episodes:(.*)$", line)
        if m and current in episodes:
            indent, rest = m.groups()
            comment = rest[rest.index("#"):] if "#" in rest else ""
            eps = episodes[current]
            pad = f"   {comment}" if comment else ""
            if eps:
                out.append(f"{indent}episodes:{pad}")
                out += [f"{indent}  - {{ session: \"{e['session']}\", segment: {e['segment']}, episode: {e['episode']} }}"
                        for e in eps]
            else:
                out.append(f"{indent}episodes: []{pad}")
            i += 1
            while i < len(lines) and lines[i].startswith(indent + "  ") and lines[i].strip().startswith("-"):
                i += 1
            continue
        out.append(line)
        i += 1
    path.write_text("\n".join(out), encoding="utf-8")


def write_playlist_id(path: Path, series: str, playlist_id: str) -> None:
    """Set one series' ``playlist_id:`` in series.yaml, keeping the rest of the file and its comments."""
    lines = path.read_text(encoding="utf-8").split("\n")
    current, done = None, False
    for i, line in enumerate(lines):
        m = re.match(r"^\s*- name:\s*(.+?)\s*$", line)
        if m:
            current = str(yaml.safe_load(m.group(1)))
            continue
        m = re.match(r"^(\s*)playlist_id:.*$", line)
        if m and current == series:
            lines[i] = f"{m.group(1)}playlist_id: {playlist_id}   # https://www.youtube.com/playlist?list={playlist_id}"
            done = True
            break
    if not done:
        raise ValueError(f"series {series!r} has no playlist_id line in {path.name}")
    path.write_text("\n".join(lines), encoding="utf-8")


@dataclass
class NumberingReport:
    series: dict[str, list[dict]] = field(default_factory=dict)  # new episodes per series
    changed: dict[str, bool] = field(default_factory=dict)
    locked_conflicts: list[str] = field(default_factory=list)
    unknown_series: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    unsegmented: list[Path] = field(default_factory=list)


def renumber(root: Path, recordings_root: Path, series_path: Path = SERIES_YAML) -> NumberingReport:
    registry = load_series(series_path)
    by_series, problems = collect_episodes(root)
    rep = NumberingReport(problems=problems, unsegmented=unsegmented_recordings(root, recordings_root))
    for name, eps in by_series.items():
        if name not in registry:
            rep.unknown_series.append(name)
            continue
        new = [{"session": e.session, "segment": e.segment, "episode": n} for n, e in enumerate(eps, 1)]
        old = [{"session": str(x["session"]), "segment": int(x["segment"]), "episode": int(x["episode"])}
               for x in registry[name].get("episodes") or []]
        rep.series[name] = new
        rep.changed[name] = new != old
        if registry[name].get("numbering") == "locked" and old and new[: len(old)] != old:
            rep.locked_conflicts.append(name)
    for name in registry:
        rep.series.setdefault(name, [])
        rep.changed.setdefault(name, bool(registry[name].get("episodes")))
    return rep


# ---------------------------------------------------------------------------
# Copy for posts and the checklist
# ---------------------------------------------------------------------------

def _md_section(text: str, heading: str) -> str:
    m = re.search(rf"^\*\*{re.escape(heading)}\*\*[^\n]*\n\n(.*?)(?=\n\*\*|\Z)", text, re.S | re.M)
    return m.group(1).strip() if m else ""


def short_copy(session_dir: Path, clip_id: str) -> dict:
    p = session_dir / "shorts" / f"{clip_id}.copy.md"
    if not p.exists():
        return {}
    t = p.read_text(encoding="utf-8")
    return {"title": _md_section(t, "Title"), "description": _md_section(t, "Description"),
            "hashtags": _md_section(t, "Hashtags")}


def vod_meta(session_dir: Path, seg_id: int) -> dict:
    p = session_dir / "vods" / f"seg{seg_id}.youtube.md"
    if not p.exists():
        return {}
    t = p.read_text(encoding="utf-8")
    title = re.search(r"^## Title[^\n]*\n\n(.+?)$", t, re.M)
    desc = re.search(r"^## Description[^\n]*\n\n```\n(.*?)\n```", t, re.M | re.S)
    tags = re.search(r"^## Tags[^\n]*\n\n(.+?)$", t, re.M)
    return {"title": title.group(1).strip() if title else "", "description": desc.group(1) if desc else "",
            "tags": tags.group(1).strip() if tags else ""}


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    session: Session
    clips: ClipsFile
    log: PublishLog
    session_dir: Path
    shorts: list[Clip] = field(default_factory=list)
    episodes: list = field(default_factory=list)  # publishable segments
    blocked_episodes: dict[int, str] = field(default_factory=dict)  # seg id -> reason
    twitch_todo: list[Clip] = field(default_factory=list)
    twitch_blocked: Optional[str] = None
    discord_todo: list[str] = field(default_factory=list)  # episode keys ("seg2") ready to post
    discord_waiting: dict[str, str] = field(default_factory=dict)  # episode key -> what it waits for

    def clip_window(self, clip: Clip) -> tuple[int, float]:
        """(vod_offset_end, duration) for Twitch: start exact, end rounded up to a whole second."""
        start_vod = clip.start - (self.log.twitch_vod_start or 0.0)
        end_vod = math.ceil(clip.end - (self.log.twitch_vod_start or 0.0))
        return end_vod, round(min(60.0, max(5.0, end_vod - start_vod)), 1)


def build_plan(session_dir: Path, session: Session, clips: ClipsFile, log: PublishLog,
               twitch_vod: Optional[dict] = None, vod_checked: bool = False) -> Plan:
    plan = Plan(session, clips, log, session_dir)
    approved = [c for c in clips.clips if c.approved]
    plan.shorts = [c for c in approved if "short" in c.targets]
    plan.episodes = [s for s in session.segments if s.publishable]
    registry = load_series()
    for seg in plan.episodes:
        ser = registry.get(seg.series or "")
        if ser is None:
            plan.blocked_episodes[seg.id] = f"series {seg.series!r} not in series.yaml"
        elif episode_label(ser, session.session, seg.episode_segment) == "#?":
            plan.blocked_episodes[seg.id] = f"{seg.series} numbering is {ser.get('numbering')}: episode number not final"
        elif not vod_path(session_dir, seg).exists():
            plan.blocked_episodes[seg.id] = "no VOD file yet (stream-cut-vods)"

    want_twitch = [c for c in approved if "twitch" in c.targets and c.id not in log.twitch_clips]
    if want_twitch:
        if not session.twitch_vod_id:
            plan.twitch_blocked = "no twitch_vod_id in session.yaml"
        elif vod_checked and twitch_vod is None:
            plan.twitch_blocked = f"VOD {session.twitch_vod_id} is gone (past the 14-day window)"
        elif log.twitch_vod_start is None or log.twitch_vod_start_method not in TRUSTED_OFFSET:
            plan.twitch_blocked = (f"VOD offset not measured: run `stream-vod-offset {session.session}` "
                                   f"(the VOD's created_at is not its first frame)")
    if not plan.twitch_blocked:
        plan.twitch_todo = want_twitch

    for seg in plan.episodes:
        key = f"seg{seg.id}"
        if key in log.discord:
            continue
        if key in log.youtube:
            plan.discord_todo.append(key)
        else:
            plan.discord_waiting[key] = "YouTube URL"
    return plan


_ABBREV_END = re.compile(r"\b(?:Dr|Mr|Mrs|Ms|St|Prof|vs|e\.g|i\.e)\.$")


def _first_sentence(text: str, limit: int = 280, min_len: int = 60) -> str:
    """The description's opening sentence, plus the next one while it's under ``min_len``."""
    para = text.strip().split("\n\n")[0].replace("\n", " ").strip()
    line = ""
    for m in re.finditer(r".+?[.!?](?=\s|$)|.+$", para):
        line = (line + " " + m.group(0).strip()).strip()
        if _ABBREV_END.search(line):   # "Dr." isn't the end of a sentence
            continue
        if len(line) >= min_len:
            break
    return line if len(line) <= limit else line[: limit - 1].rsplit(" ", 1)[0] + "…"


def discord_content(plan: Plan, key: str) -> str:
    """An episode post: title, the first sentence of its description, the YouTube link."""
    seg = plan.session.segment(int(key[3:]))
    meta = vod_meta(plan.session_dir, seg.id)
    parts = [f"**{meta.get('title') or seg.series}**"]
    line = _first_sentence(meta.get("description") or "")
    if line:
        parts.append(line)
    parts.append(plan.log.youtube[key].url)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------

def write_checklist(plan: Plan) -> Path:
    s, d, log = plan.session, plan.session_dir, plan.log
    registry = load_series()
    out = [f"# Upload checklist: {s.session}", "",
           "Upload in YouTube Studio as **Private**, then set public or scheduled there.",
           f"After uploading, record each URL: `uv run python pipeline.py stream-publish {s.session} "
           f"--youtube <id>=<url> --go` (ids below).", ""]
    out += ["## Shorts", ""]
    for n, c in enumerate(plan.shorts, 1):
        copy = short_copy(d, c.id)
        done = log.youtube.get(c.id)
        seg = s.segment(c.segment)
        pl = registry.get(seg.series or "", {}).get("playlist_id")
        out += [f"### {n}. `{c.id}` {copy.get('title') or c.title}" + ("  (uploaded)" if done else ""), ""]
        if done:
            out += [f"- [x] {done.url}", ""]
            continue
        out += [f"- [ ] **File:** `{d / 'shorts' / (c.id + '.mp4')}` ({fmt_short(c.short_duration)})",
                f"- **Title:** {copy.get('title') or c.title}",
                "- **Description:**", "", "```", copy.get("description", "").strip(), "```", "",
                f"- **Playlist (optional for Shorts):** {seg.series}"
                + (f" (https://www.youtube.com/playlist?list={pl})" if pl else ""),
                "- **Visibility:** Private", ""]
    out += ["## Episodes", ""]
    for seg in plan.episodes:
        meta = vod_meta(d, seg.id)
        done = log.youtube.get(f"seg{seg.id}")
        ci = cut_info(d, seg.id) or {}
        ser = registry.get(seg.series or "", {})
        head = f"### `seg{seg.id}` {meta.get('title') or seg.series}"
        if done:
            out += [head + "  (uploaded)", "", f"- [x] {done.url}", ""]
            continue
        if seg.id in plan.blocked_episodes:
            out += [head, "", f"**Don't upload yet:** {plan.blocked_episodes[seg.id]}.", ""]
            continue
        thumbs = d / "vods" / f"seg{seg.id}_thumbs"
        out += [head, "",
                f"- [ ] **File:** `{vod_path(d, seg)}` ({fmt_short(ci.get('duration', seg.duration))})",
                f"- **Title:** {meta.get('title')}",
                "- **Description:**", "", "```", meta.get("description", ""), "```", "",
                f"- **Tags:** {meta.get('tags')}",
                f"- **Playlist:** {seg.series} (https://www.youtube.com/playlist?list={ser.get('playlist_id')})",
                f"- **Thumbnail:** `{thumbs / 'pick1.jpg'}` (alternatives pick2, pick3)",
                "- **Visibility:** Private", "- **Audience:** not made for kids", ""]
    p = d / CHECKLIST_MD
    p.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def create_twitch_clip(plan: Plan, helix, clip: Clip) -> TwitchClipRecord:
    end, duration = plan.clip_window(clip)
    title = clip.title[:100]
    made = helix.create_clip_from_vod(plan.session.twitch_vod_id, end, duration, title)
    rec = TwitchClipRecord(id=made["id"], edit_url=made.get("edit_url", ""), title=title,
                           requested_start=clip.start, requested_end=clip.end, vod_offset_end=end,
                           duration=duration, created_at=_now())
    plan.log.twitch_clips[clip.id] = rec  # record the ID before polling: never create it twice
    refresh_twitch_clip(plan, helix, clip.id)
    return plan.log.twitch_clips[clip.id]


def refresh_twitch_clip(plan: Plan, helix, clip_id: str, wait: bool = True) -> None:
    rec = plan.log.twitch_clips[clip_id]
    info = helix.wait_for_clip(rec.id) if wait else helix.clip(rec.id)
    if not info:
        return
    rec.url = info.get("url") or rec.url
    if info.get("vod_offset") is not None:
        rec.actual_vod_offset = int(info["vod_offset"])
        requested = rec.requested_start - (plan.log.twitch_vod_start or 0.0)
        rec.drift = round(rec.actual_vod_offset - requested, 1)


def post_discord(webhook: str, content: str) -> dict:
    payload = {"content": content, "username": DISCORD_NAME, "allowed_mentions": {"parse": []}}
    for _ in range(5):
        r = requests.post(webhook, params={"wait": "true"}, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        if not r.ok:
            raise RuntimeError(f"Discord webhook failed ({r.status_code}): {r.text[:200]}")
        return r.json()
    raise RuntimeError("Discord webhook kept rate-limiting")


def all_done(plan: Plan) -> bool:
    log = plan.log
    approved = [c for c in plan.clips.clips if c.approved]
    yt = all(c.id in log.youtube for c in plan.shorts) and all(f"seg{s.id}" in log.youtube for s in plan.episodes)
    tw = plan.twitch_blocked is not None and "gone" in plan.twitch_blocked or all(
        c.id in log.twitch_clips for c in approved if "twitch" in c.targets)
    dc = all(f"seg{s.id}" in log.discord for s in plan.episodes)
    return yt and tw and dc


__all__ = [
    "PublishLog", "load_log", "save_log", "youtube_video_id", "renumber", "write_episodes",
    "build_plan", "write_checklist", "discord_content", "create_twitch_clip", "refresh_twitch_clip",
    "post_discord", "all_done", "DRIFT_WARN_SECONDS", "secrets",
]
