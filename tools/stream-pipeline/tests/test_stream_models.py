"""session.yaml / clips.yaml models (docs/post-stream-spec.md)."""
from __future__ import annotations

import datetime as dt
import textwrap

import pytest
import yaml
from pydantic import ValidationError

from wabisanji_post import sessions
from wabisanji_post.models import ClipsFile, Session, fmt_tc, parse_tc


# The approved 2026-10-05 file, verbatim (comments, label/notes and all).
APPROVED = textwrap.dedent("""\
    # APPROVED by Sam 2026-10-05. Cut points from frame sampling (1/min, refined to ~10 s at boundaries).
    session: 2026-10-05
    status: segments_approved
    recording: C:\\Videos\\2026-10-05 10-37-11.mkv
    recording_start: 2026-10-05T10:37:11+11:00
    duration: "02:26:38"
    audio_track: 0              # Track1 and Track2 are the same full mix; Track3 is silent
    streamed: true
    twitch_vod_id: null
    excluded_files:
      - file: C:\\Videos\\2026-10-05 10-16-02.mkv
        reason: local walk-n-talk test, not streamed
    segments:
      - id: 0
        type: skip
        game: null
        label: Starting Soon screen
        start: "00:00:00"
        end: "00:07:40"
        reason: Starting Soon screen
      - id: 1
        type: skip
        game: null
        label: Walk-n-talk IRL setup review
        start: "00:07:40"
        end: "00:15:00"
        reason: audio broken; IRL content waits for a dedicated IRL series
      - id: 2
        type: japanese
        game: Breath of Fire IV (JP)
        series: Learning Japanese with Breath of Fire IV
        start: "00:15:00"
        end: "00:51:50"
        chapters:
          - { at: "00:15:00", title: Setting up the wabisanji parser for Breath of Fire IV }
          - { at: "00:22:20", title: Title screen and naming Ryu }
          - { at: "00:27:00", title: Back to the parser setup }
          - { at: "00:32:00", title: Gameplay with the vocab overlay }
      - id: 3
        type: gaming
        game: Alchemy Factory
        series: Alchemy Factory
        start: "00:52:00"
        end: "02:26:20"
        notes: Joined a viewer's factory partway through (chat around 01:00:00 to 01:05:00); confirm chapter split
      - id: 4
        type: skip
        game: null
        label: Desktop / ending
        start: "02:26:20"
        end: "02:26:38"
        reason: ending
""")


def raw() -> dict:
    return yaml.safe_load(APPROVED)


def session(**changes) -> Session:
    data = raw()
    data.update(changes)
    return Session.model_validate(data)


# -- timecodes -----------------------------------------------------------------

@pytest.mark.parametrize("text,seconds", [
    ("00:15:00", 900.0),
    ("02:26:38", 8798.0),
    ("00:41:12.5", 2472.5),
    ("2:03", 123.0),
    ("1:02:03", 3723.0),
    (3723, 3723.0),  # PyYAML reads unquoted 1:02:03 as base-60 int
    (2472.5, 2472.5),
    ("90", 90.0),
])
def test_parse_tc(text, seconds):
    assert parse_tc(text) == seconds


@pytest.mark.parametrize("bad", ["", "abc", "00:61:00", "00:00:75", -1, True, None])
def test_parse_tc_rejects(bad):
    with pytest.raises(ValueError):
        parse_tc(bad)


@pytest.mark.parametrize("seconds,text", [(900, "00:15:00"), (2472.5, "00:41:12.5"), (8798, "02:26:38"), (0, "00:00:00")])
def test_fmt_tc(seconds, text):
    assert fmt_tc(seconds) == text
    assert parse_tc(fmt_tc(seconds)) == seconds


def test_unquoted_yaml_timecodes_are_seconds():
    data = raw()
    data["segments"][2]["end"] = yaml.safe_load("x: 0:51:50")["x"]  # base-60 int
    assert Session.model_validate(data).segment(2).end == 51 * 60 + 50


# -- session.yaml ----------------------------------------------------------------

def test_approved_session_validates():
    s = session()
    assert s.session == "2026-10-05"  # YAML date → id string
    assert s.status == "segments_approved"
    assert s.recording_start == dt.datetime(2026, 10, 5, 10, 37, 11, tzinfo=dt.timezone(dt.timedelta(hours=11)))
    assert s.duration == 8798
    assert [g.id for g in s.segments] == [0, 1, 2, 3, 4]
    assert [g.id for g in s.segments if g.publishable] == [2, 3]
    assert s.segment(2).chapters[1].at == 22 * 60 + 20
    assert s.segment_at(51 * 60 + 55) is None  # 10 s gap between 2 and 3
    assert s.segment_at(3000).id == 2
    assert s.warnings() == []


def test_round_trip_keeps_timecode_strings():
    dumped = sessions.dump_yaml(session())
    assert '"00:51:50"' in dumped or "'00:51:50'" in dumped
    again = Session.model_validate(yaml.safe_load(dumped))
    assert again == session()


def test_overlapping_segments_rejected():
    data = raw()
    data["segments"][3]["start"] = "00:51:00"
    with pytest.raises(ValidationError, match="overlap"):
        Session.model_validate(data)


def test_full_version_may_overlap_its_segment():
    data = raw()
    edit = data["segments"][3]
    edit["cuts"] = [{"start": "01:00:00", "end": "01:30:00", "reason": "response edit"}]
    full = {"id": 5, "type": "gaming", "game": "Alchemy Factory", "series": "Alchemy Factory",
            "start": "00:52:00", "end": "02:26:20", "full_of": 3}
    data["segments"].insert(4, full)
    s = Session.model_validate(data)
    assert s.segment(5).publishable and s.segment(5).episode_segment == 3
    assert s.segment_at(parse_tc("01:10:00")) is None  # the full version doesn't claim the edit's cuts
    assert s.segment_at(parse_tc("00:55:00")).id == 3
    full["full_of"] = 1  # a skip segment
    with pytest.raises(ValidationError, match="full_of"):
        Session.model_validate(data)
    full["full_of"], full["series"] = 3, "Something Else"
    with pytest.raises(ValidationError, match="series"):
        Session.model_validate(data)


def test_unsorted_segments_rejected():
    data = raw()
    data["segments"][2], data["segments"][3] = data["segments"][3], data["segments"][2]
    with pytest.raises(ValidationError, match="sorted"):
        Session.model_validate(data)


def test_segment_past_duration_rejected():
    data = raw()
    data["segments"][4]["end"] = "02:26:39"
    with pytest.raises(ValidationError, match="after the recording duration"):
        Session.model_validate(data)


def test_skip_needs_reason():
    data = raw()
    data["segments"][0]["reason"] = None
    with pytest.raises(ValidationError, match="need a reason"):
        Session.model_validate(data)


def test_end_after_start():
    data = raw()
    data["segments"][2]["end"] = "00:15:00"
    with pytest.raises(ValidationError, match="must be after start"):
        Session.model_validate(data)


def test_duplicate_segment_ids():
    data = raw()
    data["segments"][3]["id"] = 2
    with pytest.raises(ValidationError, match="duplicate segment ids"):
        Session.model_validate(data)


def test_chapter_outside_segment():
    data = raw()
    data["segments"][2]["chapters"].append({"at": "00:55:00", "title": "Too late"})
    with pytest.raises(ValidationError, match="outside"):
        Session.model_validate(data)


def test_unknown_keys_rejected():
    data = raw()
    data["segments"][2]["sereis"] = "typo"
    with pytest.raises(ValidationError):
        Session.model_validate(data)
    with pytest.raises(ValidationError):
        session(stauts="draft")


def test_bad_status_rejected():
    with pytest.raises(ValidationError):
        session(status="approved")


def test_recording_start_needs_offset():
    with pytest.raises(ValidationError, match="UTC offset"):
        session(recording_start="2026-10-05T10:37:11")


def test_draft_with_no_segments_is_valid():
    s = session(status="draft", segments=[])
    assert s.segments == []


def test_warnings_short_segment_and_missing_series():
    data = raw()
    data["segments"][2]["end"] = "00:20:00"
    data["segments"][2]["chapters"] = []
    data["segments"][3]["series"] = None
    w = Session.model_validate(data).warnings()
    assert any("under_10_min" in x for x in w)
    assert any("segment 3 has no series" in x for x in w)


def test_status_order():
    s = session()
    assert s.at_least("draft") and s.at_least("segments_approved")
    assert not s.at_least("transcribed")


# -- set_status keeps the hand-edited file intact --------------------------------

def test_set_status_preserves_comments(tmp_path):
    (tmp_path / "session.yaml").write_bytes(APPROVED.replace("\n", "\r\n").encode())
    sessions.set_status(tmp_path, "transcribed")
    text = (tmp_path / "session.yaml").read_bytes().decode()
    assert text.startswith("# APPROVED by Sam 2026-10-05.")
    assert "status: transcribed\r\n" in text
    assert "# Track1 and Track2 are the same full mix" in text
    assert text.replace("status: transcribed", "status: segments_approved") == APPROVED.replace("\n", "\r\n")
    with pytest.raises(ValueError):
        sessions.set_status(tmp_path, "done")


def test_write_new_session_refuses_overwrite(tmp_path):
    sessions.write_new_session(tmp_path, session(status="draft"), header="# draft")
    with pytest.raises(FileExistsError):
        sessions.write_new_session(tmp_path, session(status="draft"), header="# draft")
    assert sessions.load_session(tmp_path).status == "draft"


# -- clips.yaml --------------------------------------------------------------------

CLIPS = textwrap.dedent("""\
    session: 2026-10-05
    status: proposed
    clips:
      - id: c01
        segment: 2
        start: "00:41:12.5"
        end: "00:41:48.0"
        targets: [short, twitch, discord]
        layout: japanese
        title: "I finally read this line without help"
        hook: first spoken line or on-screen text
        why: vocab capture at 00:41:20 plus laugh at 00:41:31
        caption_fixes: []
        approved: false
""")


def clips(**clip_changes) -> dict:
    data = yaml.safe_load(CLIPS)
    data["clips"][0].update(clip_changes)
    return data


def test_spec_clips_example_validates():
    c = ClipsFile.model_validate(clips())
    assert c.session == "2026-10-05"
    assert c.clips[0].start == 2472.5 and c.clips[0].end == 2508.0
    assert c.clips[0].duration == 35.5
    assert c.problems(session()) == []
    assert c.warnings() == []


def test_clip_caption_fixes_pairs():
    c = ClipsFile.model_validate(clips(caption_fixes=[["bread of fire", "Breath of Fire"]]))
    assert c.clips[0].caption_fixes == [("bread of fire", "Breath of Fire")]
    with pytest.raises(ValidationError):
        ClipsFile.model_validate(clips(caption_fixes=[["only one"]]))


def test_twitch_clip_over_60s_rejected():
    with pytest.raises(ValidationError, match="Twitch"):
        ClipsFile.model_validate(clips(end="00:42:30"))
    ok = ClipsFile.model_validate(clips(end="00:42:30", targets=["short"]))
    assert ok.warnings()  # 77.5 s is outside the 20-60 s Shorts range


def test_clip_bad_target_and_extra_keys():
    with pytest.raises(ValidationError):
        ClipsFile.model_validate(clips(targets=["tiktok"]))
    with pytest.raises(ValidationError):
        ClipsFile.model_validate(clips(aproved=True))


def test_duplicate_clip_ids():
    data = clips()
    data["clips"].append(dict(data["clips"][0]))
    with pytest.raises(ValidationError, match="duplicate clip ids"):
        ClipsFile.model_validate(data)


def test_clip_cross_checks_against_session():
    s = session()
    outside = ClipsFile.model_validate(clips(start="00:51:40", end="00:52:10"))
    assert any("outside segment 2" in p for p in outside.problems(s))
    skip = ClipsFile.model_validate(clips(segment=0, start="00:01:00", end="00:01:30"))
    assert any("skip segment" in p for p in skip.problems(s))
    missing = ClipsFile.model_validate(clips(segment=9))
    assert any("no segment 9" in p for p in missing.problems(s))


def test_round_trip_suffixed_session_id():
    s = session(session="2026-09-15_2", status="draft", segments=[])
    dumped = sessions.dump_yaml(s)
    assert "session: 2026-09-15_2\n" in dumped and "!!timestamp" not in dumped
    assert "recording_start: 2026-10-05T10:37:11+11:00\n" in dumped
    assert Session.model_validate(yaml.safe_load(dumped)) == s


# -- cuts: one episode per game per stream, breaks cut out ----------------------------

def af_session(**seg_changes) -> dict:
    """2026-09-21_2 shape: AF with a cut that holds a break + a WoW segment."""
    data = raw()
    data["segments"] = [
        {"id": 0, "type": "skip", "label": "Starting Soon", "start": "00:00:00", "end": "00:01:40", "reason": "Starting Soon"},
        {"id": 1, "type": "gaming", "game": "Alchemy Factory", "series": "Alchemy Factory",
         "start": "00:01:40", "end": "02:20:00",
         "cuts": [{"start": "00:40:00", "end": "01:36:30", "reason": "break, then World of Warcraft"}]},
        {"id": 2, "type": "skip", "label": "Back Shortly", "start": "00:40:00", "end": "00:42:50", "reason": "break"},
        {"id": 3, "type": "gaming", "game": "World of Warcraft", "series": "World of Warcraft",
         "start": "00:42:50", "end": "01:36:10"},
    ]
    data["segments"][1].update(seg_changes)
    return data


def test_cut_segment_parts_and_duration():
    s = Session.model_validate(af_session())
    af = s.segment(1)
    assert af.parts == [(100.0, 2400.0), (5790.0, 8400.0)]
    assert af.duration == 2300 + 2610
    assert s.segment_at(3000).id == 3  # inside AF's cut: the WoW segment
    assert s.segment_at(5800).id == 1 and s.segment_at(5780) is None


def test_cut_must_be_inside_and_parts_must_not_overlap():
    with pytest.raises(ValidationError, match="must lie inside"):
        Session.model_validate(af_session(cuts=[{"start": "00:01:00", "end": "00:10:00", "reason": "x"}]))
    data = af_session(cuts=[{"start": "00:41:00", "end": "01:36:30", "reason": "x"}])  # break now overlaps AF
    with pytest.raises(ValidationError, match="overlap"):
        Session.model_validate(data)


def test_chapter_in_cut_rejected_and_clip_crossing_cut_flagged():
    with pytest.raises(ValidationError, match="inside a cut"):
        Session.model_validate(af_session(chapters=[{"at": "00:01:40", "title": "a"}, {"at": "01:00:00", "title": "b"}]))
    s = Session.model_validate(af_session())
    c = ClipsFile.model_validate(clips(segment=1, start="00:39:50", end="00:40:20", targets=["twitch"]))
    assert any("crosses one of its cuts" in p for p in c.problems(s))
