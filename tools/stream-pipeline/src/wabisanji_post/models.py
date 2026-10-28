"""Pydantic models for ``session.yaml`` (gate 1) and ``clips.yaml`` (gate 2).

Times in both files are offsets into the recording written as
``HH:MM:SS[.s]``. In the models they are float seconds (:data:`Timecode`) and
serialise back to the string form, so a load → dump round trip keeps the files
readable. Bare YAML numbers are accepted as seconds: PyYAML reads an unquoted
``1:02:03`` as the base-60 int 3723, which is the same offset.

Both models forbid unknown keys so a typo in a hand-edited gate file fails
loudly instead of being dropped.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Literal, Optional

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)


# ---------------------------------------------------------------------------
# Timecodes
# ---------------------------------------------------------------------------

_TC_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")


def parse_tc(value: object) -> float:
    """``"01:02:03.5"`` / ``"2:03"`` / ``3723`` → seconds."""
    if isinstance(value, bool):
        raise ValueError(f"not a timecode: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"negative timecode: {value!r}")
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return parse_tc(float(text))
        except ValueError:
            pass
        m = _TC_RE.match(text)
        if m:
            h, mi, s = m.groups()
            if int(mi) >= 60 or float(s) >= 60:
                raise ValueError(f"minutes/seconds out of range in {value!r}")
            return int(h or 0) * 3600 + int(mi) * 60 + float(s)
    raise ValueError(f"not a timecode (want HH:MM:SS[.s]): {value!r}")


def fmt_tc(seconds: float) -> str:
    """Seconds → ``HH:MM:SS``, with one decimal only when it isn't whole."""
    tenths = int(round(seconds * 10))
    whole, frac = divmod(tenths, 10)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    out = f"{h:02d}:{m:02d}:{s:02d}"
    return f"{out}.{frac}" if frac else out


def fmt_short(seconds: float) -> str:
    """Seconds → ``H:MM:SS`` (contact-sheet labels, log lines)."""
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


Timecode = Annotated[
    float,
    BeforeValidator(parse_tc),
    PlainSerializer(fmt_tc, return_type=str),
]


def _session_id(value: object) -> object:
    # PyYAML turns an unquoted ``session: 2026-10-05`` into a date.
    return value.isoformat() if isinstance(value, dt.date) else value


SessionId = Annotated[str, BeforeValidator(_session_id), Field(pattern=r"^\d{4}-\d{2}-\d{2}(_\d+)?$")]


# ---------------------------------------------------------------------------
# session.yaml
# ---------------------------------------------------------------------------

SessionStatus = Literal[
    "draft",
    "segments_approved",
    "transcribed",
    "moments_proposed",
    "clips_approved",
    "rendered",
    "packaged",
    "published",
    "archived",
]
STATUS_ORDER: tuple[str, ...] = SessionStatus.__args__  # type: ignore[attr-defined]

SegmentType = Literal["skip", "talk", "japanese", "gaming", "coding"]

MIN_EPISODE_SECONDS = 10 * 60


class Chapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: Timecode
    title: str = Field(..., min_length=1)


class ExcludedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    reason: str = Field(..., min_length=1)


class Cut(BaseModel):
    """A stretch inside a segment that is left out of the episode (a BRB, a trailer, another game)."""

    model_config = ConfigDict(extra="forbid")

    start: Timecode
    end: Timecode
    reason: str = Field(..., min_length=1)


class Banner(BaseModel):
    """A notice drawn across the top of the episode for a stretch (mic muted, the streamer 2026-10-10).

    Only that stretch is re-encoded when the episode is cut; the rest stays a stream copy.
    """

    model_config = ConfigDict(extra="forbid")

    start: Timecode
    end: Timecode
    text: str = Field(..., min_length=1)


class Blur(BaseModel):
    """A box blurred out of the episode for a stretch (a viewer's personal news in the chat overlay,
    the streamer 2026-10-18). ``box`` is ``[x, y, w, h]`` in 1920x1080 reference pixels, like layouts.yaml
    (a 2560x1440 recording's box is measured at 0.75x). Burned in with the banner pass.
    """

    model_config = ConfigDict(extra="forbid")

    start: Timecode
    end: Timecode
    box: tuple[int, int, int, int]
    reason: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _check(self) -> "Blur":
        x, y, w, h = self.box
        if x < 0 or y < 0 or w < 4 or h < 4:
            raise ValueError(f"blur {fmt_tc(self.start)}: box {list(self.box)} needs x, y >= 0 and w, h >= 4")
        return self


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(..., ge=0)
    type: SegmentType
    game: Optional[str] = None
    series: Optional[str] = None
    label: Optional[str] = None  # short description, mostly for skips
    start: Timecode
    end: Timecode
    chapters: list[Chapter] = Field(default_factory=list)
    reason: Optional[str] = None  # required when type is skip
    # e.g. under_10_min; shorts_only = a source for clips that is never an episode
    # (e.g. the takes left out of a response edit, the streamer 2026-10-14)
    flags: list[str] = Field(default_factory=list)
    # The full version of another segment (e.g. the whole watch-along behind a response edit,
    # The streamer 2026-10-15): its own episode file under that segment's episode number, allowed to
    # overlap it, titled "(Full Version)".
    full_of: Optional[int] = None
    notes: Optional[str] = None  # free text for the streamer / the next step
    # Stretches inside start..end left out of the episode: one episode per game per stream,
    # with breaks (and anything else not worth keeping) cut out. Other segments may sit inside a cut.
    cuts: list[Cut] = Field(default_factory=list)
    banners: list[Banner] = Field(default_factory=list)
    blurs: list[Blur] = Field(default_factory=list)

    @property
    def parts(self) -> list[tuple[float, float]]:
        """The kept ``(start, end)`` ranges, in order: start..end minus the cuts."""
        out, cur = [], self.start
        for c in self.cuts:
            out.append((cur, c.start))
            cur = c.end
        out.append((cur, self.end))
        return [(a, b) for a, b in out if b > a]

    @property
    def duration(self) -> float:
        """Length of the episode: the kept parts only."""
        return sum(b - a for a, b in self.parts)

    def contains(self, t: float) -> bool:
        return any(a <= t < b for a, b in self.parts)

    @property
    def episode_segment(self) -> int:
        """The segment whose episode number this one carries (itself, or the one it's the full version of)."""
        return self.id if self.full_of is None else self.full_of

    @property
    def publishable(self) -> bool:
        return self.type != "skip" and not {"under_10_min", "shorts_only"} & set(self.flags)

    @model_validator(mode="after")
    def _check(self) -> "Segment":
        if self.end <= self.start:
            raise ValueError(
                f"segment {self.id}: end {fmt_tc(self.end)} must be after start {fmt_tc(self.start)}"
            )
        if self.type == "skip" and not (self.reason and self.reason.strip()):
            raise ValueError(f"segment {self.id}: skip segments need a reason")
        last = self.start
        for c in self.cuts:
            if not (last <= c.start < c.end <= self.end) or c.start <= self.start or c.end >= self.end:
                raise ValueError(
                    f"segment {self.id}: cut {fmt_tc(c.start)}-{fmt_tc(c.end)} must lie inside the segment, "
                    f"in time order, without overlapping another cut"
                )
            last = c.end
        prev = None
        for ch in self.chapters:
            if not self.contains(ch.at):
                raise ValueError(
                    f"segment {self.id}: chapter {fmt_tc(ch.at)} {ch.title!r} is outside "
                    f"{fmt_tc(self.start)}-{fmt_tc(self.end)} or inside a cut"
                )
            if prev is not None and ch.at <= prev:
                raise ValueError(f"segment {self.id}: chapters must be in time order")
            prev = ch.at
        last = None
        for bn in self.banners:
            if bn.end <= bn.start or not any(a <= bn.start and bn.end <= b for a, b in self.parts):
                raise ValueError(
                    f"segment {self.id}: banner {fmt_tc(bn.start)}-{fmt_tc(bn.end)} must lie inside one kept part "
                    f"(not across a cut or the segment edges)"
                )
            if last is not None and bn.start < last:
                raise ValueError(f"segment {self.id}: banners must be in time order without overlapping")
            last = bn.end
        for bl in self.blurs:
            if bl.end <= bl.start or not any(a <= bl.start and bl.end <= b for a, b in self.parts):
                raise ValueError(
                    f"segment {self.id}: blur {fmt_tc(bl.start)}-{fmt_tc(bl.end)} must lie inside one kept part "
                    f"(not across a cut or the segment edges)"
                )
        return self


class Session(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: SessionId
    status: SessionStatus = "draft"
    recording: str
    recording_start: dt.datetime  # local time from the OBS filename, with offset
    duration: Timecode
    audio_track: int = Field(0, ge=0)  # ffmpeg a:<n>
    streamed: Optional[bool] = None
    twitch_vod_id: Optional[str] = None
    excluded_files: list[ExcludedFile] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)

    @field_validator("twitch_vod_id", mode="before")
    @classmethod
    def _vod_id_str(cls, v: object) -> object:
        return str(v) if isinstance(v, int) else v

    @field_validator("recording_start")
    @classmethod
    def _aware(cls, v: dt.datetime) -> dt.datetime:
        if v.tzinfo is None:
            raise ValueError("recording_start needs a UTC offset, e.g. 2026-10-05T10:37:11+11:00")
        return v

    @model_validator(mode="after")
    def _check_segments(self) -> "Session":
        ids = [s.id for s in self.segments]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate segment ids: {ids}")
        prev: Optional[Segment] = None
        for seg in self.segments:
            if seg.end > self.duration:
                raise ValueError(
                    f"segment {seg.id} ends at {fmt_tc(seg.end)}, after the recording "
                    f"duration {fmt_tc(self.duration)}"
                )
            if prev is not None and seg.start < prev.start:
                raise ValueError(f"segments must be sorted by start (segment {seg.id})")
            prev = seg
        for seg in self.segments:
            if seg.full_of is not None:
                base = next((s for s in self.segments if s.id == seg.full_of), None)
                if base is None or not base.publishable or base.full_of is not None:
                    raise ValueError(f"segment {seg.id}: full_of {seg.full_of} must be a publishable segment")
                if seg.series != base.series:
                    raise ValueError(f"segment {seg.id}: a full version keeps the series of segment {base.id}")
        # Kept parts must not overlap (a segment may sit inside another one's cut).
        # A full version repeats another segment, so it's left out of this check.
        spans = sorted((a, b, seg.id) for seg in self.segments if seg.full_of is None for a, b in seg.parts)
        for (a1, b1, i1), (a2, b2, i2) in zip(spans, spans[1:]):
            if a2 < b1:
                raise ValueError(f"segments {i1} and {i2} overlap ({fmt_tc(b1)} > {fmt_tc(a2)})")
        return self

    # -- helpers -------------------------------------------------------------

    def segment(self, seg_id: int) -> Segment:
        for s in self.segments:
            if s.id == seg_id:
                return s
        raise KeyError(f"session {self.session} has no segment {seg_id}")

    def segment_at(self, t: float) -> Optional[Segment]:
        for s in self.segments:
            if s.full_of is None and s.contains(t):
                return s
        return None

    def at_least(self, status: str) -> bool:
        return STATUS_ORDER.index(self.status) >= STATUS_ORDER.index(status)

    def warnings(self) -> list[str]:
        """Soft problems worth showing at Gate 1, but not invalid."""
        out = []
        for s in self.segments:
            if s.type == "skip":
                continue
            if s.duration < MIN_EPISODE_SECONDS and not {"under_10_min", "shorts_only"} & set(s.flags):
                out.append(f"segment {s.id} is {fmt_short(s.duration)} long: add flag under_10_min")
            if not s.game and s.type in ("japanese", "gaming"):
                out.append(f"segment {s.id} ({s.type}) has no game")
            if s.publishable and not s.series:
                out.append(f"segment {s.id} has no series (needed for the episode title)")
            if s.chapters and s.chapters[0].at != s.start:
                out.append(
                    f"segment {s.id}: first chapter is at {fmt_tc(s.chapters[0].at)}, "
                    f"not the segment start {fmt_tc(s.start)} (YouTube chapters start at 00:00)"
                )
        return out


# ---------------------------------------------------------------------------
# clips.yaml
# ---------------------------------------------------------------------------

ClipTarget = Literal["short", "twitch", "discord"]

TWITCH_MAX_SECONDS = 60
SHORT_RANGE_SECONDS = (20, 60)


class ClipCard(BaseModel):
    """A word card set by hand, for Japanese said on stream with no capture behind it."""
    model_config = ConfigDict(extra="forbid")

    at: Timecode  # recording offset, inside the clip
    word: str = Field(..., min_length=1)
    reading: Optional[str] = None
    meaning: str = Field(..., min_length=1)


class Clip(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., pattern=r"^[A-Za-z0-9_-]+$")
    segment: int = Field(..., ge=0)
    start: Timecode
    end: Timecode
    # "discord" is accepted but ignored: Discord gets episodes only (the streamer, 2026-10-14)
    targets: list[ClipTarget] = Field(default_factory=lambda: ["short", "twitch"])
    layout: str  # key in layouts.yaml
    title: str = Field(..., min_length=1)
    hook: Optional[str] = None
    why: Optional[str] = None
    caption_fixes: list[tuple[str, str]] = Field(default_factory=list)  # [[heard, correct], ...]
    cards: list[ClipCard] = Field(default_factory=list)  # word cards on top of any from vocab captures
    badge: bool = True  # Short: twitch.tv/WabiSanji badge (where the layout has a badge spot)
    # Short only: jump-cuts over dead air, like the wait while ARIA looks something up (the streamer,
    # 2026-10-22). Twitch clips are cut from the VOD in one piece and keep these stretches.
    cuts: list[Cut] = Field(default_factory=list)
    approved: bool = False

    @property
    def duration(self) -> float:
        """Length in the recording (the Twitch clip)."""
        return self.end - self.start

    @property
    def short_parts(self) -> list[tuple[float, float]]:
        """The kept ``(start, end)`` recording ranges of the Short: start..end minus the cuts."""
        out, cur = [], self.start
        for c in self.cuts:
            out.append((cur, c.start))
            cur = c.end
        out.append((cur, self.end))
        return [(a, b) for a, b in out if b > a]

    @property
    def short_duration(self) -> float:
        return sum(b - a for a, b in self.short_parts)

    def to_short(self, t: float) -> float:
        """Recording time → seconds into the Short. Inside a cut it snaps to the jump."""
        out = 0.0
        for a, b in self.short_parts:
            if t < a:
                return out
            if t <= b:
                return out + (t - a)
            out += b - a
        return out

    @model_validator(mode="after")
    def _check(self) -> "Clip":
        if self.end <= self.start:
            raise ValueError(f"clip {self.id}: end must be after start")
        if "twitch" in self.targets and self.duration > TWITCH_MAX_SECONDS:
            raise ValueError(
                f"clip {self.id}: {self.duration:.1f}s is over the {TWITCH_MAX_SECONDS}s Twitch clip limit"
            )
        prev = self.start
        for c in self.cuts:
            if not prev <= c.start < c.end < self.end or c.start == self.start:
                raise ValueError(
                    f"clip {self.id}: cut {fmt_tc(c.start)}-{fmt_tc(c.end)} must sit inside the clip, "
                    "in order and not overlapping"
                )
            prev = c.end
        for card in self.cards:
            if not self.start <= card.at < self.end:
                raise ValueError(f"clip {self.id}: card {card.word} at {fmt_tc(card.at)} is outside the clip")
            if any(c.start <= card.at < c.end for c in self.cuts):
                raise ValueError(f"clip {self.id}: card {card.word} at {fmt_tc(card.at)} is inside a cut")
        return self


class ClipsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: SessionId
    status: Literal["proposed", "approved"] = "proposed"
    clips: list[Clip] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> "ClipsFile":
        ids = [c.id for c in self.clips]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate clip ids: {dupes}")
        return self

    def problems(self, session: Session) -> list[str]:
        """Cross-checks against session.yaml: segment exists, isn't a skip, clip fits inside it."""
        out = []
        if self.session != session.session:
            out.append(f"clips.yaml is for {self.session}, session.yaml is {session.session}")
        for c in self.clips:
            try:
                seg = session.segment(c.segment)
            except KeyError:
                out.append(f"clip {c.id}: no segment {c.segment}")
                continue
            if seg.type == "skip":
                out.append(f"clip {c.id}: segment {seg.id} is a skip segment")
            if not any(a <= c.start and c.end <= b for a, b in seg.parts):
                out.append(
                    f"clip {c.id}: {fmt_tc(c.start)}-{fmt_tc(c.end)} is outside segment "
                    f"{seg.id} ({fmt_tc(seg.start)}-{fmt_tc(seg.end)}) or crosses one of its cuts"
                )
        return out

    def warnings(self) -> list[str]:
        lo, hi = SHORT_RANGE_SECONDS
        return [
            f"clip {c.id}: {c.short_duration:.1f}s is outside the {lo}-{hi}s Shorts range"
            for c in self.clips
            if "short" in c.targets and not (lo <= c.short_duration <= hi)
        ]
