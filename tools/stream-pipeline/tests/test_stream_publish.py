"""Publishing logic: numbering, plan readiness, Twitch clip windows, publish.yaml (no network)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from wabisanji_post import publish, sessions
from wabisanji_post.models import ClipsFile, Session

from test_stream_models import APPROVED, CLIPS


def make_session(root: Path, sid: str, text: str = APPROVED) -> Path:
    d = root / sid
    d.mkdir(parents=True)
    (d / "session.yaml").write_text(text, encoding="utf-8")
    return d


SERIES = textwrap.dedent("""\
    # registry comment kept
    series:
      - name: Learning Japanese with Breath of Fire IV
        game: Breath of Fire IV (JP)
        playlist_id: PLMbQUfTUoXv8
        numbering: open
        episodes: []           # [{session, segment, episode}]
      - name: Alchemy Factory
        game: Alchemy Factory
        playlist_id: PLU_VXeoYFHXE
        numbering: locked
        episodes:
          - { session: "2026-09-20", segment: 1, episode: 1 }
""")


def test_youtube_video_id():
    assert publish.youtube_video_id("https://youtube.com/shorts/AbCdEfGhIjK?feature=share") == "AbCdEfGhIjK"
    assert publish.youtube_video_id("https://www.youtube.com/watch?v=AbCdEfGhIjK&t=3") == "AbCdEfGhIjK"
    assert publish.youtube_video_id("https://youtu.be/AbCdEfGhIjK") == "AbCdEfGhIjK"
    with pytest.raises(ValueError):
        publish.youtube_video_id("https://www.youtube.com/playlist?list=PLMbQUfTUoXv8")


def test_write_episodes_keeps_comments_and_other_fields(tmp_path):
    p = tmp_path / "series.yaml"
    p.write_text(SERIES, encoding="utf-8")
    publish.write_episodes(p, {
        "Learning Japanese with Breath of Fire IV": [{"session": "2026-10-05", "segment": 2, "episode": 1}],
        "Alchemy Factory": [],
    })
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# registry comment kept")
    assert "episodes:   # [{session, segment, episode}]" in text
    data = yaml.safe_load(text)
    bof, af = data["series"]
    assert bof["episodes"] == [{"session": "2026-10-05", "segment": 2, "episode": 1}]
    assert bof["playlist_id"] == "PLMbQUfTUoXv8" and af["numbering"] == "locked"
    assert af["episodes"] == []


def test_write_episodes_matches_quoted_series_names(tmp_path):
    p = tmp_path / "series.yaml"
    p.write_text('series:\n  - name: "Star Wars: Zero Company"   # quoted for the colon\n'
                 '    numbering: locked\n    episodes: []\n', encoding="utf-8")
    publish.write_episodes(p, {"Star Wars: Zero Company": [{"session": "2026-09-08", "segment": 0, "episode": 1}]})
    ser = yaml.safe_load(p.read_text(encoding="utf-8"))["series"][0]
    assert ser["episodes"] == [{"session": "2026-09-08", "segment": 0, "episode": 1}]


def test_renumber_orders_by_stream_date_and_guards_locked(tmp_path):
    root = tmp_path / "sessions"
    recs = tmp_path / "Videos"
    recs.mkdir()
    make_session(root, "2026-10-05", APPROVED.replace(
        r"recording: C:\Videos\2026-10-05 10-37-11.mkv", f"recording: {recs / '2026-10-05 10-37-11.mkv'}"))
    earlier = APPROVED.replace("session: 2026-10-05", "session: 2026-09-20").replace(
        "recording_start: 2026-10-05T10:37:11+11:00", "recording_start: 2026-09-20T19:00:00+10:00").replace(
        "recording: C:\\Videos\\2026-10-05 10-37-11.mkv", "recording: C:\\Videos\\2026-09-20 19-00-00.mkv")
    make_session(root, "2026-09-20", earlier)
    draft = APPROVED.replace("status: segments_approved", "status: draft").replace(
        "session: 2026-10-05", "session: 2026-10-01")
    make_session(root, "2026-10-01", draft)  # drafts don't get numbers
    series = tmp_path / "series.yaml"
    series.write_text(SERIES, encoding="utf-8")
    (recs / "2026-09-15 18-00-00.mkv").write_bytes(b"")
    (recs / "2026-10-05 10-37-11.mkv").write_bytes(b"")
    rep = publish.renumber(root, recs, series)
    bof = rep.series["Learning Japanese with Breath of Fire IV"]
    assert [(e["session"], e["episode"]) for e in bof] == [("2026-09-20", 1), ("2026-10-05", 2)]
    af = rep.series["Alchemy Factory"]
    assert [(e["session"], e["segment"], e["episode"]) for e in af] == [("2026-09-20", 3, 1), ("2026-10-05", 3, 2)]
    assert rep.locked_conflicts == ["Alchemy Factory"]  # old #1 was 2026-09-20 segment 1
    assert [p.name for p in rep.unsegmented] == ["2026-09-15 18-00-00.mkv"]


def plan_for(tmp_path, log=None, vod=True):
    d = make_session(tmp_path, "2026-10-05",
                     APPROVED.replace("status: segments_approved", "status: rendered")
                     .replace("twitch_vod_id: null", 'twitch_vod_id: "2892170919"'))
    session = sessions.load_session(d)
    raw = yaml.safe_load(CLIPS)
    raw["status"] = "approved"
    c = raw["clips"][0]
    c["approved"] = True
    c2 = dict(c, id="c07", targets=["twitch", "discord"], start="00:31:50.0", end="00:32:31.4")
    c3 = dict(c, id="c06", targets=["short", "discord"], start="00:42:30.2", end="00:43:05.4")
    raw["clips"] += [c2, c3]
    clips = ClipsFile.model_validate(raw)
    log = log or publish.PublishLog(session="2026-10-05", twitch_vod_id="2892170919", twitch_vod_start=13.0, twitch_vod_start_method="frames")
    return publish.build_plan(d, session, clips, log, {"id": "2892170919"} if vod else None, vod_checked=True)


def test_clip_window_shifts_by_vod_start_and_ends_on_whole_second(tmp_path):
    plan = plan_for(tmp_path)
    c01 = plan.clips.clips[0]  # 00:41:12.5-00:41:48.0 in the recording
    end, dur = plan.clip_window(c01)
    assert end == 2495  # ceil(2508.0 - 13)
    assert dur == pytest.approx(35.5)  # start stays exact: 2495 - 35.5 = 2459.5 = 2472.5 - 13
    c07 = next(c for c in plan.clips.clips if c.id == "c07")  # 00:31:50.0-00:32:31.4
    assert plan.clip_window(c07) == (1939, 42.0)  # ends rounded up 0.6 s: 1897.0-1939


def test_plan_readiness_and_discord_waits(tmp_path):
    plan = plan_for(tmp_path)
    assert [c.id for c in plan.twitch_todo] == ["c01", "c07"]
    # Discord posts episodes only (Sam, 2026-10-14): clip "discord" targets are ignored
    assert plan.discord_todo == []
    assert plan.discord_waiting == {"seg2": "YouTube URL", "seg3": "YouTube URL"}
    # segments 2 and 3 are blocked: series numbering open / no VOD files in tmp
    assert set(plan.blocked_episodes) == {2, 3}


def test_plan_after_clips_and_urls(tmp_path):
    log = publish.PublishLog(session="2026-10-05", twitch_vod_id="2892170919", twitch_vod_start=13.0, twitch_vod_start_method="frames")
    log.youtube["c06"] = publish.YouTubeRecord(url="https://youtube.com/shorts/AbCdEfGhIjK", video_id="AbCdEfGhIjK",
                                               recorded_at="t")
    log.youtube["seg3"] = publish.YouTubeRecord(url="https://youtu.be/ZyXwVuTsRqP", video_id="ZyXwVuTsRqP",
                                                recorded_at="t")
    log.twitch_clips["c07"] = publish.TwitchClipRecord(
        id="Clip07", edit_url="https://dashboard.twitch.tv/c", url="https://clips.twitch.tv/Clip07", title="t",
        requested_start=1910.0, requested_end=1951.4, vod_offset_end=1939, duration=41.4, created_at="t")
    plan = plan_for(tmp_path, log)
    vods = plan.session_dir / "vods"
    vods.mkdir()
    (vods / "seg3.youtube.md").write_text(textwrap.dedent("""\
        # Segment 3

        ## Title (40 chars)

        Alchemy Factory #2 | Steam and soap maths

        ## Description

        ```
        Back in the factory. I fix the steam
        and the soap maths.

        Live on Twitch: x
        ```
        """), encoding="utf-8")
    assert [c.id for c in plan.twitch_todo] == ["c01"]
    assert plan.discord_todo == ["seg3"]
    assert plan.discord_waiting == {"seg2": "YouTube URL"}
    content = publish.discord_content(plan, "seg3")
    assert content == ("**Alchemy Factory #2 | Steam and soap maths**\n"
                       "Back in the factory. I fix the steam and the soap maths.\n"  # short first line: next one too
                       "https://youtu.be/ZyXwVuTsRqP")


def test_twitch_blocked_when_vod_gone(tmp_path):
    plan = plan_for(tmp_path, vod=False)
    assert plan.twitch_todo == [] and "gone" in plan.twitch_blocked
    assert plan.discord_waiting == {"seg2": "YouTube URL", "seg3": "YouTube URL"}


def test_publish_log_round_trip(tmp_path):
    log = publish.PublishLog(session="2026-10-05", twitch_vod_start=13.0)
    log.discord["c07"] = publish.DiscordRecord(message_id="1", channel_id="2", content="x", posted_at="t")
    publish.save_log(tmp_path, log)
    text = (tmp_path / "publish.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Written by stream-publish")
    again = publish.load_log(tmp_path, Session.model_validate(yaml.safe_load(APPROVED)))
    assert again == log


def test_set_recording_keeps_layout(tmp_path):
    d = make_session(tmp_path, "2026-10-05")
    new = Path(r"C:\Videos\WabiSanji\sessions\2026-10-05\raw\2026-10-05 10-37-11.mkv")
    sessions.set_recording(d, new)
    text = (d / "session.yaml").read_text(encoding="utf-8")
    assert f"recording: {new}\n" in text
    assert text.startswith("# APPROVED by Sam")
    assert sessions.load_session(d).recording == str(new)


def test_twitch_blocked_until_offset_measured(tmp_path):
    rough = publish.PublishLog(session="2026-10-05", twitch_vod_id="2892170919", twitch_vod_start=13.0)
    plan = plan_for(tmp_path, rough)  # created_at estimate only: not trusted
    assert plan.twitch_todo == [] and "stream-vod-offset" in plan.twitch_blocked


def test_sharp_offsets_finds_shift_and_ignores_static_frames():
    import numpy as np
    from wabisanji_post.vodsync import sharp_offsets

    rng = np.random.default_rng(0)
    rec = [(100 + i / 10, rng.uniform(0, 255, (8, 8)).astype(np.float32)) for i in range(200)]
    # VOD frames are recording frames 7.8 s earlier in VOD time, plus a little compression noise
    vod = [(t - 7.8, a + rng.normal(0, 2, a.shape).astype(np.float32)) for t, a in rec[50:80]]
    static = np.full((8, 8), 128, np.float32)
    rec_static = rec + [(130 + i / 10, static) for i in range(20)]
    offs = sharp_offsets(vod + [(200.0, static)], rec_static)
    assert len(offs) == 30 and all(abs(o - 7.8) < 1e-6 for o in offs)  # the static frame is ambiguous: dropped


def test_parse_playlist_keeps_fmp4_init_segment():
    from wabisanji_post.vodsync import parse_playlist

    base = "https://cdn.example/abc/360p30/"
    # 2K VODs (2026-10-18_2 on) are fragmented MP4: segments can't be read without the init segment
    fmp4 = ('#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-MAP:URI="init-0.mp4"\n'
            "#EXTINF:10.000,\n0.mp4\n#EXTINF:10.000,\n1.mp4\n#EXTINF:4.500,\n2.mp4\n")
    segs, init = parse_playlist(fmp4, base)
    assert init == base + "init-0.mp4"
    assert segs == [(0.0, 10.0, base + "0.mp4"), (10.0, 10.0, base + "1.mp4"), (20.0, 4.5, base + "2.mp4")]
    # 1080p VODs are MPEG-TS with no init segment
    segs, init = parse_playlist("#EXTM3U\n#EXTINF:10.000,\n0.ts\n#EXTINF:10.000,\n1.ts\n", base)
    assert init is None and [s[0] for s in segs] == [0.0, 10.0]


def test_first_sentence_skips_abbreviations():
    text = ("The last stretch of Blue Shift. I pick up in Xen, then Dr. Rosenberg needs one more power cell, "
            "so it's down to the generators. Then the credits.")
    assert publish._first_sentence(text) == (
        "The last stretch of Blue Shift. I pick up in Xen, then Dr. Rosenberg needs one more power cell, "
        "so it's down to the generators.")
