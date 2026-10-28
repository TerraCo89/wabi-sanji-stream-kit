"""Cross-platform performance (spec: "Metrics"): which posts and which kinds of stream do best where.

Every post is already in a session's ``publish.yaml`` (YouTube video id, Instagram media id,
Twitch clip id), keyed by clip id or ``seg<N>``, and ``session.yaml`` / ``clips.yaml`` say what
it is (segment type, game, series). :func:`collect_posts` joins the two, the ``fetch_*``
functions read the numbers, and :func:`build_report` writes ``_metrics_report.md``.

- YouTube: public counts with ``youtube.readonly``; watch time, retention and subscribers with
  ``yt-analytics.readonly`` (``stream-auth youtube`` again to add it). Thumbnail impressions and
  click-through rate come from the Reporting API's daily reach reports, downloaded into
  ``_metrics/reach/`` (:func:`sync_reach`), and are compared before and after each episode's
  thumbnail changed (``seg<N>_thumbs/pushed.json``).
- Instagram: likes and comments; views, reach, saves, shares and average watch time with
  ``instagram_manage_insights`` (``stream-auth instagram`` again).
- Twitch: clip and VOD view counts from Helix. Per-stream numbers (viewers, follows, chatters)
  aren't in the API: The streamer exports Channel Analytics as CSV and :func:`parse_twitch_csv` reads it.

A platform that isn't logged in, or lacks a scope, is skipped with a note; the rest still runs.
Each run also saves the raw rows to ``_metrics/<date>.json``, so later runs can compare growth.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import publish, sessions, settings
from .models import Segment, Session

METRICS_DIR = "_metrics"
REPORT_MD = "_metrics_report.md"
NEW_DAYS = 3  # posts younger than this are flagged: too early to judge

# Talk segments are told apart by series: a response edit and a dog walk are different content.
TALK_SERIES = {"Wabi Sanji Responds": "response", "Wabi Sanji Outside": "irl", "Wabi Sanji Builds": "build"}

KIND_LABEL = {"short": "YouTube Shorts", "reel": "Instagram Reels", "twitch_clip": "Twitch clips",
              "episode": "YouTube episodes", "vod": "Twitch VODs"}
CLIP_KINDS = ("short", "reel", "twitch_clip")


@dataclass
class Post:
    platform: str  # youtube | instagram | twitch
    kind: str  # episode | short | reel | twitch_clip | vod
    session: str
    item: str  # "seg3", "c01", or "vod"
    ref: str  # the platform's id
    url: str
    title: str
    category: str  # japanese | gaming | coding | response | irl | build | talk | unknown
    game: Optional[str] = None
    series: Optional[str] = None
    published_at: Optional[str] = None
    status: str = ""  # public | scheduled | private | missing | expired
    m: dict = field(default_factory=dict)  # metrics
    thumb_at: Optional[str] = None  # when stream-upload / stream-thumbnail-push last set the thumbnail

    @property
    def public(self) -> bool:
        return self.status == "public"

    def age_days(self, now: dt.datetime) -> Optional[float]:
        if not self.published_at:
            return None
        t = dt.datetime.fromisoformat(self.published_at.replace("Z", "+00:00"))
        return max((now - t).total_seconds() / 86400, 0.0)

    def views_per_day(self, now: dt.datetime) -> Optional[float]:
        age, views = self.age_days(now), self.m.get("views")
        if age is None or views is None:
            return None
        return views / max(age, 1.0)  # a post's first day counts as a whole day

    def matured(self, now: dt.datetime) -> bool:
        """Old enough to compare: Shorts and Reels get most of their views in the first couple of days."""
        age = self.age_days(now)
        return age is not None and age >= NEW_DAYS

    @property
    def engagement(self) -> Optional[float]:
        views = self.m.get("views")
        parts = [self.m.get(k) for k in ("likes", "comments", "shares")]
        if not views or all(v is None for v in parts):  # Twitch clips have no likes
            return None
        return sum(v or 0 for v in parts) / views


def category(seg: Optional[Segment]) -> str:
    if seg is None:
        return "unknown"
    if seg.type == "talk":
        return TALK_SERIES.get(seg.series or "", "talk")
    return seg.type


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------

def _segment(s: Session, seg_id: int) -> Optional[Segment]:
    return next((g for g in s.segments if g.id == seg_id), None)


def collect_posts(root: Path) -> tuple[list[Post], list[str]]:
    """One :class:`Post` per recorded upload in every session's ``publish.yaml``."""
    posts: list[Post] = []
    problems: list[str] = []
    for yml in sorted(root.glob(f"*/{sessions.SESSION_YAML}")):
        d = yml.parent
        if not (d / publish.PUBLISH_YAML).exists():
            continue
        try:
            s = sessions.load_session(d)
            log = publish.load_log(d, s)
            clips = {c.id: c for c in sessions.load_clips(d).clips} if (d / sessions.CLIPS_YAML).exists() else {}
        except Exception as exc:  # report, keep going
            problems.append(f"{d.name}: {str(exc).splitlines()[0]}")
            continue

        def describe(item: str) -> tuple[Optional[Segment], str]:
            if item.startswith("seg") and item[3:].isdigit():
                seg = _segment(s, int(item[3:]))
                return seg, (seg.series or seg.game or seg.label) if seg else item
            clip = clips.get(item)
            return (_segment(s, clip.segment) if clip else None), (clip.title if clip else item)

        def add(platform: str, kind: str, item: str, ref: str, url: str, title: Optional[str] = None) -> None:
            seg, default_title = describe(item)
            posts.append(Post(platform, kind, s.session, item, ref, url, title or default_title, category(seg),
                              seg.game if seg else None, seg.series if seg else None))

        for item, rec in log.youtube.items():
            add("youtube", "episode" if item.startswith("seg") else "short", item, rec.video_id, rec.url)
            pushed = d / "vods" / f"{item}_thumbs" / "pushed.json"
            if item.startswith("seg") and pushed.exists():
                data = json.loads(pushed.read_text(encoding="utf-8"))
                if data.get("video_id") == rec.video_id:
                    posts[-1].thumb_at = data.get("pushed_at")
        for item, rec in log.instagram.items():
            add("instagram", "reel", item, rec.media_id, rec.permalink)
        for item, rec in log.twitch_clips.items():
            add("twitch", "twitch_clip", item, rec.id, rec.url or rec.edit_url, rec.title)
        if s.twitch_vod_id and s.streamed:
            games = sorted({g.game or g.label for g in s.segments if g.type != "skip"})
            posts.append(Post("twitch", "vod", s.session, "vod", s.twitch_vod_id,
                              f"https://www.twitch.tv/videos/{s.twitch_vod_id}", ", ".join(games) or s.session,
                              "stream"))
    return posts, problems


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

_YT_ANALYTICS = {"engagedViews": "engaged_views", "estimatedMinutesWatched": "watch_minutes",
                 "averageViewDuration": "avg_view_s", "averageViewPercentage": "avg_view_pct",
                 "subscribersGained": "subs_gained", "shares": "shares"}


def fetch_youtube(posts: list[Post], notes: list[str], *, analytics: bool = True,
                  today: Optional[dt.date] = None) -> None:
    from . import youtube

    yt = [p for p in posts if p.platform == "youtube"]
    if not yt:
        return
    try:
        stats = youtube.video_stats(sorted({p.ref for p in yt}))
    except (youtube.YouTubeError, RuntimeError) as exc:
        notes.append(f"YouTube skipped: {exc}")
        return
    for p in yt:
        st = stats.get(p.ref)
        if not st:
            p.status = "missing"
            continue
        p.title = st["title"] or p.title
        if st["privacy"] == "public":
            p.status, p.published_at = "public", st["published_at"]
        else:
            p.status = "scheduled" if st.get("publish_at") else st["privacy"]
            p.published_at = st.get("publish_at")
        p.m.update({k: st[k] for k in ("views", "likes", "comments") if k in st})
    if not analytics:
        return
    if not youtube.has_scopes(youtube.ANALYTICS_SCOPE):
        notes.append("YouTube watch time and retention skipped: run `stream-auth youtube` once to add "
                     "the yt-analytics.readonly permission")
        return
    live = [p for p in yt if p.public and p.published_at]
    if not live:
        return
    start = min(dt.date.fromisoformat(p.published_at[:10]) for p in live)
    try:
        rows = youtube.analytics_by_video(sorted({p.ref for p in live}), start, today or dt.date.today())
    except (youtube.YouTubeError, RuntimeError) as exc:
        notes.append(f"YouTube Analytics skipped: {exc}")
        return
    for p in live:
        for k, v in (rows.get(p.ref) or {}).items():
            if k in _YT_ANALYTICS:
                p.m[_YT_ANALYTICS[k]] = v
    notes.append("YouTube Analytics lags about two days behind the public view counts.")


REACH_DIR = "reach"  # _metrics/reach/<day>.csv: the Reporting API's daily thumbnail reach reports
CTR_WINDOW = 14  # days either side of a thumbnail change that the before/after compares


def sync_reach(root: Path, notes: list[str]) -> Path:
    """Download the daily reach reports not already in ``_metrics/reach/``; sets up the reporting job
    on first use. YouTube may reissue a day with corrected numbers: the newest report per day wins."""
    from . import youtube

    d = root / METRICS_DIR / REACH_DIR
    d.mkdir(parents=True, exist_ok=True)
    if not youtube.has_scopes(youtube.ANALYTICS_SCOPE):
        notes.append("YouTube impressions and CTR skipped: run `stream-auth youtube` to add yt-analytics.readonly")
        return d
    try:
        reports = youtube.reporting_reports(youtube.reporting_job())
        latest: dict[str, dict] = {}
        for rep in reports:
            day = rep["startTime"][:10]
            if day not in latest or rep.get("createTime", "") > latest[day].get("createTime", ""):
                latest[day] = rep
        index_p = d / "index.json"
        index = json.loads(index_p.read_text(encoding="utf-8")) if index_p.exists() else {}
        for day, rep in sorted(latest.items()):
            if index.get(day) == rep["id"] and (d / f"{day}.csv").exists():
                continue
            (d / f"{day}.csv").write_text(youtube.download_report(rep["downloadUrl"]), encoding="utf-8")
            index[day] = rep["id"]
            index_p.write_text(json.dumps(index, indent=1), encoding="utf-8")
    except (youtube.YouTubeError, RuntimeError) as exc:
        hint = (" Enable the YouTube Reporting API for the pipeline's Google Cloud project (the link is in "
                "the error), then run stream-metrics again." if "has not been used" in str(exc) else "")
        notes.append(f"YouTube impressions and CTR skipped: {str(exc).splitlines()[0]}{hint}")
        return d
    if not latest:
        notes.append("YouTube impressions and CTR: the reporting job is set up; YouTube writes the first "
                     "daily reports within about two days.")
    return d


def load_reach(d: Path) -> dict[str, dict[dt.date, list[float]]]:
    """``{video_id: {day: [impressions, clicks]}}`` from the daily CSVs. Rows are split by other
    dimensions on some days, so they're summed; clicks = impressions x CTR. CTR is read as a
    percentage when any value is over 1, else as a fraction."""
    rows = []
    for p in sorted(d.glob("*.csv")):
        rows += [r for r in csv.DictReader(p.read_text(encoding="utf-8").splitlines()) if r.get("video_id")]
    ctrs = [float(r.get("video_thumbnail_impressions_ctr") or 0) for r in rows]
    scale = 100.0 if ctrs and max(ctrs) > 1 else 1.0
    out: dict[str, dict[dt.date, list[float]]] = {}
    for r, ctr in zip(rows, ctrs):
        imp = float(r.get("video_thumbnail_impressions") or 0)
        day = dt.datetime.strptime(r["date"].replace("-", ""), "%Y%m%d").date()
        cell = out.setdefault(r["video_id"], {}).setdefault(day, [0.0, 0.0])
        cell[0] += imp
        cell[1] += imp * ctr / scale
    return out


def _ctr(days: dict[dt.date, list[float]], keep: Callable[[dt.date], bool]) -> tuple[float, Optional[float]]:
    imp = sum(v[0] for day, v in days.items() if keep(day))
    clicks = sum(v[1] for day, v in days.items() if keep(day))
    return imp, (clicks / imp if imp else None)


def fetch_reach(posts: list[Post], reach: dict[str, dict[dt.date, list[float]]]) -> None:
    """Impressions and CTR per YouTube post, plus the CTR_WINDOW days before and after its thumbnail
    changed (the change day itself is left out: part of it ran on each thumbnail)."""
    for p in posts:
        days = reach.get(p.ref) if p.platform == "youtube" else None
        if not days:
            continue
        p.m["impressions"], p.m["ctr"] = _ctr(days, lambda day: True)
        if p.thumb_at:
            changed = dt.date.fromisoformat(p.thumb_at[:10])
            lo, hi = changed - dt.timedelta(days=CTR_WINDOW), changed + dt.timedelta(days=CTR_WINDOW)
            p.m["imp_before"], p.m["ctr_before"] = _ctr(days, lambda day: lo <= day < changed)
            p.m["imp_after"], p.m["ctr_after"] = _ctr(days, lambda day: changed < day <= hi)


def fetch_instagram(posts: list[Post], notes: list[str], *, insights: bool = True) -> None:
    from . import instagram

    for p in (p for p in posts if p.platform == "instagram"):
        try:
            try:
                got = instagram.media_metrics(p.ref, insights=insights)
            except instagram.InstagramError as exc:
                if not insights or exc.code not in (10, 200):
                    raise
                insights = False  # no insights permission: counts only, for this and every other Reel
                notes.append("Instagram views, reach, saves and shares skipped: run `stream-auth instagram` "
                             "once to add the instagram_manage_insights permission")
                got = instagram.media_metrics(p.ref, insights=False)
        except instagram.InstagramError as exc:
            if exc.code == 100:  # deleted
                p.status = "missing"
                continue
            notes.append(f"Instagram skipped from {p.session} {p.item}: {exc}")
            return
        except RuntimeError as exc:  # not logged in
            notes.append(f"Instagram skipped: {exc}")
            return
        p.status, p.published_at = "public", got.get("timestamp")
        for k in ("likes", "comments", "views", "reach", "saved", "shares", "total_interactions"):
            if got.get(k) is not None:
                p.m[k] = got[k]
        if got.get("ig_reels_avg_watch_time") is not None:
            p.m["avg_watch_s"] = round(got["ig_reels_avg_watch_time"] / 1000, 1)


def fetch_twitch(posts: list[Post], notes: list[str], helix_factory: Optional[Callable] = None) -> None:
    from . import twitch

    tw = [p for p in posts if p.platform == "twitch"]
    if not tw:
        return
    try:
        h = (helix_factory or twitch.Helix)()
        clips = h.clips(sorted({p.ref for p in tw if p.kind == "twitch_clip"}))
    except (twitch.TwitchError, RuntimeError) as exc:
        notes.append(f"Twitch skipped: {exc}")
        return
    for p in tw:
        if p.kind == "twitch_clip":
            c = clips.get(p.ref)
            if not c:
                p.status = "missing"
                continue
            p.status, p.published_at = "public", c.get("created_at")
            p.m["views"] = c.get("view_count")
        else:
            v = h.video(p.ref)
            if not v:
                p.status = "expired"  # Twitch keeps VODs for about 14 days
                continue
            p.status, p.published_at = "public", v.get("created_at")
            p.m["views"] = v.get("view_count")


def fetch_twitch_follows(notes: list[str], helix_factory: Optional[Callable] = None) -> dict[dt.date, int]:
    """Follows per local day from Get Channel Followers (current followers only)."""
    from . import twitch

    try:
        followers = (helix_factory or twitch.Helix)().followers()
    except (twitch.TwitchError, RuntimeError) as exc:
        notes.append(f"Twitch follows per day skipped: {exc}")
        return {}
    return follows_by_day(followers)


def follows_by_day(followers: Iterable[dict]) -> dict[dt.date, int]:
    out: dict[dt.date, int] = {}
    for f in followers:
        day = dt.datetime.fromisoformat(f["followed_at"].replace("Z", "+00:00")).astimezone().date()
        out[day] = out.get(day, 0) + 1
    return out


def add_follows(days: dict[dt.date, dict], follows: dict[dt.date, int]) -> dict[dt.date, dict]:
    """API follow counts fill in days the CSV doesn't cover (the dashboard's own count wins)."""
    for day, n in follows.items():
        cur = days.setdefault(day, {})
        if cur.get("follows") is None:
            cur["follows"] = float(n)
    return days


# ---------------------------------------------------------------------------
# Twitch Channel Analytics CSV
# ---------------------------------------------------------------------------

TWITCH_COLUMNS = {
    "avg_viewers": ("average viewers", "avg viewers", "average concurrent viewers", "avg concurrent viewers"),
    "peak_viewers": ("max viewers", "peak viewers", "peak concurrent viewers", "max concurrent viewers"),
    "minutes_watched": ("minutes watched", "watch time minutes", "watch time"),
    "unique_viewers": ("unique viewers",),
    "live_views": ("live views", "views"),
    "follows": ("follows", "new followers", "followers gained", "new follows"),
    "chatters": ("chatters", "unique chatters"),
    "chat_messages": ("chat messages", "messages"),
    "hours_streamed": ("hours streamed", "time streamed", "stream time hours", "hours live"),
    "minutes_streamed": ("minutes streamed", "stream time minutes"),
}
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%a %b %d %Y", "%d %b %Y", "%B %d, %Y")


def _norm(h: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", h.lower()).split())


def _number(v: str) -> Optional[float]:
    v = (v or "").strip().replace(",", "").rstrip("%")
    if not v or v in ("-", "--"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _date(v: str) -> Optional[dt.date]:
    v = (v or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", v):
        return dt.date.fromisoformat(v[:10])
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def parse_twitch_csv(path: Path) -> tuple[dict[dt.date, dict], list[str]]:
    """``{date: {avg_viewers, peak_viewers, follows, ...}}`` from a Channel Analytics export.
    Headers are matched loosely; unknown columns are reported, not fatal."""
    notes: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        date_col = next((h for h in headers if "date" in _norm(h) or _norm(h) == "day"), None)
        if not date_col:
            raise ValueError(f"{path.name}: no date column in {headers}")
        cols: dict[str, str] = {}
        for h in headers:
            n = _norm(h)
            for key, names in TWITCH_COLUMNS.items():
                if key not in cols and n in names:
                    cols[key] = h
        unused = [h for h in headers if h != date_col and h not in cols.values()]
        if unused:
            notes.append(f"Twitch CSV columns not used: {', '.join(unused)}")
        days: dict[dt.date, dict] = {}
        for row in reader:
            day = _date(row.get(date_col, ""))
            if not day:
                continue
            days[day] = {k: _number(row.get(h, "")) for k, h in cols.items()}
            if days[day].get("hours_streamed") is None and days[day].get("minutes_streamed") is not None:
                days[day]["hours_streamed"] = days[day]["minutes_streamed"] / 60
    return days, notes


def stream_days(root: Path) -> dict[dt.date, list[Session]]:
    """Streamed sessions by local date (the date in the OBS filename)."""
    out: dict[dt.date, list[Session]] = {}
    for yml in sorted(root.glob(f"*/{sessions.SESSION_YAML}")):
        try:
            s = sessions.load_session(yml.parent)
        except Exception:
            continue
        if s.streamed:
            out.setdefault(s.recording_start.date(), []).append(s)
    return out


def game_minutes(ss: Iterable[Session]) -> dict[str, float]:
    """Minutes on stream per game (or label) across sessions, skips left out."""
    out: dict[str, float] = {}
    for s in ss:
        for g in s.segments:
            if g.type != "skip":
                key = g.game or g.label
                out[key] = out.get(key, 0.0) + (g.end - g.start) / 60
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _median(xs: Iterable[Optional[float]]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _fmt(v: Optional[float], kind: str = "n") -> str:
    if v is None:
        return "–"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "x":
        return f"{v:.2f}x"
    if kind == "s":
        return f"{int(v) // 60}:{int(v) % 60:02d}"
    return f"{v:,.0f}" if v >= 100 else f"{v:,.1f}"


def _ranks(values: list[float]) -> list[float]:
    """Average ranks (ties share), 0-based."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> Optional[float]:
    if len(a) < 4:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return cov / (va * vb) if va and vb else None


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(r) + " |" for r in rows]


def _index_table(posts: list[Post], now: dt.datetime, key: Callable[[Post], Optional[str]],
                 kinds: tuple[str, ...], min_n: int = 2) -> list[str]:
    """Rows = groups, columns = platforms: the group's median views over the platform's median, from
    posts at least NEW_DAYS old. Above 1.00x means that content does better than usual there."""
    posts = [p for p in posts if p.kind in kinds and p.matured(now)]
    base = {k: _median(p.m.get("views") for p in posts if p.kind == k) for k in kinds}
    groups = sorted({key(p) for p in posts if key(p)})
    rows = []
    for g in groups:
        row, best = [g], 0.0
        for k in kinds:
            vals = [p.m.get("views") for p in posts if p.kind == k and key(p) == g]
            vals = [v for v in vals if v is not None]
            med = _median(vals)
            if len(vals) >= min_n and med is not None and base[k]:
                idx = med / base[k]
                best = max(best, idx)
                row.append(f"{idx:.2f}x (n={len(vals)})")
            else:
                row.append(f"– (n={len(vals)})")
        rows.append((best, row))
    rows.sort(key=lambda r: -r[0])
    return _table(["", *[KIND_LABEL[k] for k in kinds]], [r for _, r in rows])


_DAY_RATES = ("avg_viewers", "follows", "chatters")


def build_report(posts: list[Post], notes: list[str], *, now: Optional[dt.datetime] = None,
                 twitch_days: Optional[dict[dt.date, dict]] = None,
                 days_sessions: Optional[dict[dt.date, list[Session]]] = None,
                 viewer_rows: Optional[list[dict]] = None, chat_rows: Optional[list[dict]] = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    live = [p for p in posts if p.public and p.m.get("views") is not None and p.published_at]
    counts = {k: sum(1 for p in live if p.kind == k) for k in KIND_LABEL}
    blind = sum(1 for p in posts if p.public and p.kind == "reel" and p.m.get("views") is None)
    out = [f"# {settings.channel_name()}: what performs where ({now.astimezone().date():%d %b %Y})", "",
           f"Comparisons use total views of posts at least {NEW_DAYS} days old: Shorts and Reels get most of their "
           "views in the first couple of days, so younger posts (marked *new*) aren't judged yet. Views/day "
           "(views ÷ days live, the first day counting as one) is shown alongside. Scheduled and private posts "
           "are left out.", "",
           "Live posts: " + ", ".join(f"{KIND_LABEL[k]} {n}" for k, n in counts.items() if n) + "."
           + (f" {blind} Instagram Reels have no view counts yet (insights permission missing), so they're "
              "left out." if blind else ""), ""]

    # 1. Index: which content beats each platform's usual
    out += ["## Which content does better than usual, per platform", "",
            "Each cell is that content type's median views divided by the platform's median, so platforms "
            "with very different audiences can be compared. Read across a row: a type that's above 1.00x on one "
            "platform and below on another is the \"different things work in different places\" signal.", ""]
    out += _index_table(live, now, lambda p: p.category, (*CLIP_KINDS, "episode")) + [""]
    out += ["### By game or series", ""]
    out += _index_table(live, now, lambda p: p.series or p.game, (*CLIP_KINDS, "episode")) + [""]

    # 2. Same clip, three platforms
    by_clip: dict[tuple[str, str], dict[str, Post]] = {}
    for p in live:
        if p.kind in CLIP_KINDS and p.matured(now):
            by_clip.setdefault((p.session, p.item), {})[p.kind] = p
    pct: dict[str, dict[tuple[str, str], float]] = {}
    for k in CLIP_KINDS:
        items = [(key, ps[k].m["views"]) for key, ps in by_clip.items() if k in ps]
        if items:
            r = _ranks([v for _, v in items])
            n = max(len(items) - 1, 1)
            pct[k] = {key: rk / n for (key, _), rk in zip(items, r)}
    out += ["## Same clip, different platforms", ""]
    pairs = [("short", "reel"), ("short", "twitch_clip"), ("reel", "twitch_clip")]
    for a, b in pairs:
        both = [key for key, ps in by_clip.items() if a in ps and b in ps]
        rho = spearman([by_clip[k][a].m["views"] for k in both], [by_clip[k][b].m["views"] for k in both])
        out.append(f"- {KIND_LABEL[a]} vs {KIND_LABEL[b]}: rank agreement "
                   + (f"{rho:+.2f}" if rho is not None else "–") + f" over {len(both)} clips "
                   "(+1 = the same clips win on both, 0 = unrelated, −1 = opposite)")
    out.append("")
    spread_rows = []
    for key, ps in by_clip.items():
        have = {k: pct[k][key] for k in CLIP_KINDS if k in ps and key in pct.get(k, {})}
        if len(have) < 2:
            continue
        spread = max(have.values()) - min(have.values())
        any_post = next(iter(ps.values()))
        spread_rows.append((spread, [f"{any_post.title}", any_post.category, f"{key[0]} {key[1]}"]
                            + [(f"{_fmt(ps[k].m['views'])} views (top {100 - have[k] * 100:.0f}%)"
                                if k in have else "–") for k in CLIP_KINDS]))
    spread_rows.sort(key=lambda r: -r[0])
    out += ["Biggest disagreements: clips that did well on one platform and badly on another.", ""]
    out += _table(["Clip", "Type", "Session", *[KIND_LABEL[k] for k in CLIP_KINDS]],
                  [r for _, r in spread_rows[:15]]) + [""]

    # 3. Top posts per platform
    out += ["## Top posts per platform", ""]
    for k in (*CLIP_KINDS, "episode"):
        ps = sorted((p for p in live if p.kind == k), key=lambda p: -(p.m.get("views") or 0))[:10]
        if not ps:
            continue
        extra = {"short": ("avg_view_pct", "Avg viewed"), "episode": ("avg_view_pct", "Avg viewed"),
                 "reel": ("avg_watch_s", "Avg watch")}.get(k)
        header = ["Post", "Type", "Views", "Views/day", "Engagement"] + ([extra[1]] if extra else []) + ["Age"]
        rows = []
        for p in ps:
            age = p.age_days(now) or 0
            row = [f"[{p.title}]({p.url})", p.category, _fmt(p.m.get("views")), _fmt(p.views_per_day(now)),
                   _fmt(p.engagement, "pct")]
            if extra:
                v = p.m.get(extra[0])
                row.append(_fmt(v / 100 if v is not None and extra[0] == "avg_view_pct" else v,
                                "pct" if extra[0] == "avg_view_pct" else "n") + ("s" if extra[0] == "avg_watch_s" and v is not None else ""))
            row.append(f"{age:.0f} d" + (" *new*" if age < NEW_DAYS else ""))
            rows.append(row)
        out += [f"### {KIND_LABEL[k]}", ""] + _table(header, rows) + [""]

    # 4. Content type detail
    out += ["## By content type", ""]
    rows = []
    for k in (*CLIP_KINDS, "episode"):
        for cat in sorted({p.category for p in live if p.kind == k and p.matured(now)}):
            ps = [p for p in live if p.kind == k and p.category == cat and p.matured(now)]
            avg_pct = _median(p.m.get("avg_view_pct") for p in ps)
            rows.append([KIND_LABEL[k], cat, str(len(ps)), _fmt(_median(p.m.get("views") for p in ps)),
                         _fmt(_median(p.views_per_day(now) for p in ps)),
                         _fmt(_median(p.engagement for p in ps), "pct"),
                         _fmt(avg_pct / 100 if avg_pct is not None else None, "pct"),
                         _fmt(_median(p.m.get("avg_watch_s") for p in ps))])
    out += _table(["Platform", "Type", "n", "Median views", "Median views/day", "Median engagement", "YT avg viewed",
                   "IG avg watch (s)"], rows) + [""]

    # 5. Long-form episodes by series
    eps = [p for p in live if p.kind == "episode"]
    if eps:
        rows = []
        for ser in sorted({p.series or p.title for p in eps}):
            ps = [p for p in eps if (p.series or p.title) == ser]
            watch = sum(p.m.get("watch_minutes") or 0 for p in ps)
            subs = sum(p.m.get("subs_gained") or 0 for p in ps)
            rows.append([ser, str(len(ps)), _fmt(sum(p.m.get("views") or 0 for p in ps)),
                         _fmt(_median(p.views_per_day(now) for p in ps)),
                         _fmt(watch / 60) if watch else "–", _fmt(_median(p.m.get("avg_view_s") for p in ps), "s"),
                         str(int(subs)) if subs else "–"])
        rows.sort(key=lambda r: -float(r[3].replace(",", "")) if r[3] != "–" else 0)
        out += ["## Long-form episodes by series", ""]
        out += _table(["Series", "Episodes", "Views", "Median views/day", "Watch hours", "Median avg view",
                       "Subs gained"], rows) + [""]

    # 6. Thumbnails: impressions and click-through
    reach = [p for p in live if p.m.get("impressions")]
    if reach:
        out += ["## Thumbnails: impressions and click-through", "",
                "Impressions are times a thumbnail was on screen (over a second, at least half visible); CTR is "
                "the share of those that became a view. From YouTube's daily reach reports, which lag about two days.", ""]
        rows = []
        for k in ("episode", "short"):
            for cat in sorted({p.category for p in reach if p.kind == k}):
                ps = [p for p in reach if p.kind == k and p.category == cat]
                imp = sum(p.m["impressions"] for p in ps)
                clicks = sum(p.m["impressions"] * (p.m.get("ctr") or 0) for p in ps)
                rows.append([KIND_LABEL[k], cat, str(len(ps)), _fmt(imp), _fmt(clicks / imp if imp else None, "pct"),
                             _fmt(_median(p.m.get("ctr") for p in ps), "pct")])
        out += _table(["Platform", "Type", "n", "Impressions", "CTR (all)", "Median CTR"], rows) + [""]
        changed = [p for p in reach if p.kind == "episode" and p.m.get("imp_before") and p.m.get("imp_after")]
        if changed:
            ib = sum(p.m["imp_before"] for p in changed)
            ia = sum(p.m["imp_after"] for p in changed)
            cb = sum(p.m["imp_before"] * (p.m.get("ctr_before") or 0) for p in changed) / ib
            ca = sum(p.m["imp_after"] * (p.m.get("ctr_after") or 0) for p in changed) / ia
            out += ["### Before and after the new thumbnails", "",
                    f"Episodes whose thumbnail was replaced after upload, {CTR_WINDOW} days either side of the change "
                    f"(the change day left out). Across {len(changed)} episodes: CTR {_fmt(cb, 'pct')} on "
                    f"{_fmt(ib)} impressions before, {_fmt(ca, 'pct')} on {_fmt(ia)} after "
                    f"({'+' if ca >= cb else '−'}{abs(ca - cb) * 100:.1f} points). Episodes with under 100 "
                    "impressions on either side swing a lot; read the total first.", ""]
            rows = sorted(([f"[{p.title}]({p.url})", _fmt(p.m["imp_before"]), _fmt(p.m.get("ctr_before"), "pct"),
                            _fmt(p.m["imp_after"]), _fmt(p.m.get("ctr_after"), "pct"),
                            f"{((p.m.get('ctr_after') or 0) - (p.m.get('ctr_before') or 0)) * 100:+.1f}"]
                           for p in changed), key=lambda r: -float(r[5]))
            out += _table(["Episode", "Impr. before", "CTR before", "Impr. after", "CTR after", "Change (points)"],
                          rows) + [""]
        ranked = sorted((p for p in reach if p.kind == "episode" and p.m["impressions"] >= 100),
                        key=lambda p: -(p.m.get("ctr") or 0))
        if ranked:
            out += ["### Episode CTR, best first (100+ impressions)", ""]
            out += _table(["Episode", "Impressions", "CTR"],
                          [[f"[{p.title}]({p.url})", _fmt(p.m["impressions"]), _fmt(p.m.get("ctr"), "pct")]
                           for p in ranked]) + [""]

    # 7. Twitch streams
    out += ["## Twitch streams", ""]
    if viewer_rows:
        games: dict[str, dict] = {}
        for r in viewer_rows:
            g = games.setdefault(r["game"], {"streams": set(), "samples": 0, "sum": 0.0, "peak": 0})
            g["streams"].add(r["session"])
            g["samples"] += r["samples"]
            g["sum"] += r["avg"] * r["samples"]
            g["peak"] = max(g["peak"], r["peak"])
        rows = [[game, str(len(g["streams"])), f"{g['samples'] / 60:.1f}", f"{g['sum'] / g['samples']:.1f}",
                 str(g["peak"])] for game, g in sorted(games.items(), key=lambda x: -x[1]["sum"] / x[1]["samples"])]
        out += ["### Live viewers per game (logged by `stream-viewers`)", "",
                "Sampled once a minute while live and matched to each segment's time range, so each game gets "
                "its own numbers even when several were streamed the same day.", ""]
        out += _table(["Game", "Streams", "Hours logged", "Avg viewers", "Peak"], rows) + [""]
    if chat_rows:
        games = {}
        for r in chat_rows:
            g = games.setdefault(r["game"], {"streams": set(), "messages": 0, "hours": 0.0, "chatters": []})
            g["streams"].add(r["session"])
            g["messages"] += r["messages"]
            g["hours"] += r["hours"]
            g["chatters"].append(r["chatters"])
        rows = [[game, str(len(g["streams"])), f"{g['hours']:.1f}", f"{g['messages'] / (g['hours'] or 1e-9):.1f}",
                 _fmt(_median(g["chatters"]))]
                for game, g in sorted(games.items(), key=lambda x: -x[1]["messages"] / (x[1]["hours"] or 1e-9))]
        out += ["### Chat per game (from the recorder's archive)", "",
                "Viewers' messages only (yours left out), from each session's `signals/chat.json`.", ""]
        out += _table(["Game", "Streams", "Hours", "Messages per hour", "Median chatters per stream"], rows) + [""]
    if twitch_days and days_sessions:
        off = sum(int(d.get("follows") or 0) for day, d in twitch_days.items() if not days_sessions.get(day))
        if off:
            out += [f"{off} follows came on days without a stream (from Shorts, Reels or old VODs, most likely).", ""]
        rows, by_game = [], {}
        for day in sorted(twitch_days):
            ss = days_sessions.get(day, [])
            mins = game_minutes(ss)
            d = twitch_days[day]
            if not ss and not d.get("hours_streamed"):
                continue
            total = sum(mins.values()) or 1.0
            for game, m in mins.items():
                g = by_game.setdefault(game, {"days": 0, "hours": 0.0, **{k: [0.0, 0.0] for k in _DAY_RATES}})
                g["days"] += 1
                g["hours"] += m / 60
                for k in _DAY_RATES:  # [value, hours it covers]: a day without the number doesn't dilute it
                    if d.get(k) is not None:
                        g[k][0] += d[k] * m if k == "avg_viewers" else d[k] * m / total
                        g[k][1] += m / 60
            top = ", ".join(f"{g} {m / 60:.1f} h" for g, m in sorted(mins.items(), key=lambda x: -x[1])[:3])
            rows.append([f"{day:%a %d %b}", top or "(no session found)", _fmt(d.get("avg_viewers")),
                         _fmt(d.get("peak_viewers")), _fmt(d.get("follows")), _fmt(d.get("chatters")),
                         _fmt(d.get("minutes_watched"))])
        out += _table(["Day", "Streamed", "Avg viewers", "Peak", "Follows", "Chatters", "Minutes watched"], rows) + [""]
        def rate(g: dict, k: str) -> Optional[float]:
            val, hours = g[k]
            if not hours:
                return None
            return val / (hours * 60) if k == "avg_viewers" else val / hours

        sort_key = "avg_viewers" if any(rate(g, "avg_viewers") is not None for g in by_game.values()) else "follows"
        game_rows = []
        for game, g in sorted(by_game.items(), key=lambda x: -(rate(x[1], sort_key) or 0)):
            game_rows.append([game, str(g["days"]), f"{g['hours']:.1f}", _fmt(rate(g, "avg_viewers")),
                              *(f"{r:.2f}" if (r := rate(g, k)) is not None else "–" for k in ("follows", "chatters"))])
        out += ["### By game", "",
                "Each day's numbers are shared between the games streamed that day by minutes on stream, so these "
                "are estimates; a game streamed alone on a day gives the cleanest read.", ""]
        out += _table(["Game", "Days", "Hours", "Avg viewers", "Follows per hour", "Chatters per hour"],
                      game_rows) + [""]
    if not any(d.get("avg_viewers") is not None for d in (twitch_days or {}).values()):
        vods = sorted((p for p in posts if p.kind == "vod" and p.public), key=lambda p: p.session)
        out += ["No Channel Analytics CSV yet, so past viewers and chatters per stream aren't here. "
                f"Export it from dashboard.twitch.tv/u/{settings.twitch_login()}/analytics/viewer-engagement (Day view, Export "
                "Data, each 30-day period back to 2026-09-08) into `_metrics/`, and run `stream-metrics` again.", ""]
        if vods:
            out += ["VOD views per stream (the VODs Twitch still has):", ""]
            out += _table(["Session", "Streamed", "VOD views"],
                          [[p.session, p.title, _fmt(p.m.get("views"))] for p in vods]) + [""]

    out += ["## Notes", ""] + [f"- {n}" for n in notes] + [
        f"- Groups with fewer than 2 live posts show –. Small samples swing a lot: treat a 2-post group as a hint.",
        "- Engagement = (likes + comments + shares) ÷ views. Twitch clips only report views.", ""]
    return "\n".join(out)


def save_snapshot(root: Path, posts: list[Post], notes: list[str], now: dt.datetime) -> Path:
    d = root / METRICS_DIR
    d.mkdir(exist_ok=True)
    path = d / f"{now.astimezone().date().isoformat()}.json"
    path.write_text(json.dumps({"taken_at": now.isoformat(), "notes": notes, "posts": [asdict(p) for p in posts]},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def twitch_csvs(root: Path) -> list[Path]:
    """Every CSV in ``_metrics/``, oldest first (Twitch exports 30 days at a time)."""
    return sorted((root / METRICS_DIR).glob("*.csv"), key=lambda p: p.stat().st_mtime)


def merge_twitch_csvs(paths: list[Path]) -> tuple[dict[dt.date, dict], list[str]]:
    """Several exports (two 30-day periods, or Viewer Engagement plus Revenue) merged by day.
    A later file fills in or updates a day's values; blanks never wipe a number."""
    days: dict[dt.date, dict] = {}
    notes: list[str] = []
    for path in paths:
        got, n = parse_twitch_csv(path)
        notes += n
        for day, vals in got.items():
            cur = days.setdefault(day, {})
            cur.update({k: v for k, v in vals.items() if v is not None or k not in cur})
    return days, list(dict.fromkeys(notes))


__all__ = ["Post", "collect_posts", "fetch_youtube", "sync_reach", "load_reach", "fetch_reach", "fetch_instagram", "fetch_twitch", "fetch_twitch_follows",
           "follows_by_day", "add_follows", "parse_twitch_csv",
           "stream_days", "build_report", "save_snapshot", "twitch_csvs", "merge_twitch_csvs", "spearman", "category", "REPORT_MD"]
