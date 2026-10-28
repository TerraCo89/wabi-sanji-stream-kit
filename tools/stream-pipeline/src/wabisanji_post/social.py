"""``social.yaml``: per-session captions for social platforms other than YouTube, and the gate for them.

Instagram has no private or scheduled stage: a Reel is public the moment it posts. So each Short
gets a Claude-drafted caption here with ``approved: false``, and ``stream-social --go`` only posts
the ones the streamer has set to ``approved: true``. One block per platform, so TikTok etc. slot in later::

    session: 2026-10-13
    items:
      c01:
        video: shorts/c01.mp4
        instagram:
          caption: |
            ...
          approved: false

What has been posted is recorded in ``publish.yaml`` (``instagram:``), never here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .models import ClipsFile, SessionId


SOCIAL_YAML = "social.yaml"
HEADER = ("# social.yaml: captions for Instagram (more platforms later). Drafted by /stream-social.\n"
          "# Edit freely. Set approved: true on the ones to post, then run `stream-social <session> --go`.\n"
          "# Instagram posts are PUBLIC straight away. What's been posted is in publish.yaml.\n")
IG_CAPTION_MAX = 2200
IG_HASHTAG_MAX = 30
_HASHTAG = re.compile(r"(?<!\w)#\w+")
_LINK = re.compile(r"https?://|www\.", re.I)


class PlatformPost(BaseModel):
    model_config = ConfigDict(extra="forbid")
    caption: str = ""
    cover_ms: Optional[int] = Field(None, ge=0)  # cover frame, ms into the Short (Instagram's default is 0)
    approved: bool = False


class SocialItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    video: str  # relative to the session folder
    instagram: Optional[PlatformPost] = None


class SocialFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: SessionId
    items: dict[str, SocialItem] = Field(default_factory=dict)


def load_social(session_dir: Path) -> Optional[SocialFile]:
    p = session_dir / SOCIAL_YAML
    if not p.exists():
        return None
    return SocialFile.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")) or {})


class _Dumper(yaml.SafeDumper):
    pass


def _repr_str(dumper: yaml.SafeDumper, value: str):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_Dumper.add_representer(str, _repr_str)


def save_social(session_dir: Path, social: SocialFile) -> Path:
    data = social.model_dump(mode="json", exclude_none=True)
    for item in data["items"].values():  # end block captions with a newline so they dump as `|`, not `|-`
        ig = item.get("instagram")
        if ig and "\n" in ig["caption"] and not ig["caption"].endswith("\n"):
            ig["caption"] += "\n"
    body = yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120)
    p = session_dir / SOCIAL_YAML
    p.write_text(HEADER + body, encoding="utf-8")
    return p


def add_missing(social: Optional[SocialFile], session_id: str, clips: ClipsFile) -> tuple[SocialFile, list[str]]:
    """Add an empty, unapproved entry for every approved Short not in the file yet. Existing
    entries (the streamer's edits) are never touched. Returns the file and the ids added."""
    social = social or SocialFile(session=session_id)
    added = []
    for c in clips.clips:
        if c.approved and "short" in c.targets and c.id not in social.items:
            social.items[c.id] = SocialItem(video=f"shorts/{c.id}.mp4", instagram=PlatformPost())
            added.append(c.id)
    return social, added


def caption_problems(caption: str) -> list[str]:
    out = []
    text = caption.strip()
    if not text:
        return ["no caption yet"]
    if len(text) > IG_CAPTION_MAX:
        out.append(f"caption is {len(text)} characters (Instagram's limit is {IG_CAPTION_MAX})")
    tags = _HASHTAG.findall(text)
    if len(tags) > IG_HASHTAG_MAX:
        out.append(f"{len(tags)} hashtags (Instagram's limit is {IG_HASHTAG_MAX})")
    if _LINK.search(text):
        out.append("has a link (links in Instagram captions aren't clickable: say 'link in bio')")
    if "—" in text:
        out.append("has an em dash (content rules)")
    return out


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@dataclass
class Row:
    key: str
    state: str  # "posted", "ready", "waiting" (not approved yet), "blocked"
    reason: str = ""
    video: Optional[Path] = None
    post: Optional[PlatformPost] = None


@dataclass
class InstagramPlan:
    rows: list[Row] = field(default_factory=list)

    @property
    def ready(self) -> list[Row]:
        return [r for r in self.rows if r.state == "ready"]


def instagram_plan(session_dir: Path, clips: ClipsFile, social: SocialFile, posted: dict) -> InstagramPlan:
    """Every item in social.yaml with what stops it posting. ``posted`` is publish.yaml's
    ``instagram`` dict."""
    plan = InstagramPlan()
    by_id = {c.id: c for c in clips.clips}
    for key, item in social.items.items():
        post, video = item.instagram, session_dir / item.video
        row = Row(key, "ready", video=video, post=post)
        clip = by_id.get(key)
        if key in posted:
            row.state, row.reason = "posted", posted[key].permalink or posted[key].media_id
        elif post is None:
            row.state, row.reason = "blocked", "no instagram block"
        elif clip is None or not clip.approved or "short" not in clip.targets:
            row.state, row.reason = "blocked", "not an approved Short in clips.yaml"
        elif not video.exists():
            row.state, row.reason = "blocked", f"no file {item.video} (stream-render-shorts)"
        elif problems := caption_problems(post.caption):
            row.state, row.reason = "blocked", "; ".join(problems)
        elif not post.approved:
            row.state, row.reason = "waiting", "not approved yet"
        plan.rows.append(row)
    return plan
