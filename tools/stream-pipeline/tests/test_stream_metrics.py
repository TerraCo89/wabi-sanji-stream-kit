"""stream-metrics: joining posts to content, platform fetchers (faked), Twitch CSV, report (no network)."""
from __future__ import annotations

import datetime as dt
import textwrap

import pytest
import yaml

from wabisanji_post import instagram, metrics, youtube
from wabisanji_post.metrics import Post

from test_stream_models import APPROVED, CLIPS

NOW = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)

PUBLISH = textwrap.dedent("""\
    session: '2026-10-05'
    youtube:
      seg2: {url: https://youtu.be/AAAAAAAAAAA, video_id: AAAAAAAAAAA, recorded_at: '2026-10-06T00:00:00+00:00'}
      c01: {url: https://youtube.com/shorts/BBBBBBBBBBB, video_id: BBBBBBBBBBB, recorded_at: '2026-10-06T00:00:00+00:00'}
      c02: {url: https://youtube.com/shorts/CCCCCCCCCCC, video_id: CCCCCCCCCCC, recorded_at: '2026-10-06T00:00:00+00:00'}
    instagram:
      c01: {media_id: '111', permalink: https://www.instagram.com/reel/x/, posted_at: '2026-10-06T00:00:00+00:00'}
    twitch_clips:
      c01: {id: Clip1, edit_url: https://clips.twitch.tv/Clip1/edit, url: https://clips.twitch.tv/Clip1,
            title: Read it, requested_start: 1.0, requested_end: 2.0, vod_offset_end: 2, duration: 1.0,
            created_at: '2026-10-06T00:00:00+00:00'}
""")


@pytest.fixture
def root(tmp_path):
    d = tmp_path / "2026-10-05"
    d.mkdir()
    (d / "session.yaml").write_text(APPROVED.replace("twitch_vod_id: null", "twitch_vod_id: '999'"), encoding="utf-8")
    clips = yaml.safe_load(CLIPS)
    clips["clips"].append({**clips["clips"][0], "id": "c02", "segment": 3, "start": "01:00:00", "end": "01:00:30",
                           "title": "Factory fire", "layout": "gaming"})
    (d / "clips.yaml").write_text(yaml.safe_dump(clips), encoding="utf-8")
    (d / "publish.yaml").write_text(PUBLISH, encoding="utf-8")
    return tmp_path


def test_collect_posts_joins_content(root):
    posts, problems = metrics.collect_posts(root)
    assert problems == []
    by = {(p.platform, p.item): p for p in posts}
    assert by[("youtube", "seg2")].kind == "episode"
    assert by[("youtube", "seg2")].series == "Learning Japanese with Breath of Fire IV"
    assert by[("youtube", "c01")].kind == "short" and by[("youtube", "c01")].category == "japanese"
    assert by[("youtube", "c02")].category == "gaming" and by[("youtube", "c02")].title == "Factory fire"
    assert by[("instagram", "c01")].kind == "reel"
    assert by[("twitch", "c01")].kind == "twitch_clip" and by[("twitch", "c01")].ref == "Clip1"
    assert by[("twitch", "vod")].ref == "999" and by[("twitch", "vod")].category == "stream"


def test_category_tells_talk_series_apart():
    class S:  # duck-typed Segment
        def __init__(self, type, series=None):
            self.type, self.series = type, series
    assert metrics.category(S("talk", "Wabi Sanji Responds")) == "response"
    assert metrics.category(S("talk", "Something else")) == "talk"
    assert metrics.category(S("japanese")) == "japanese"
    assert metrics.category(None) == "unknown"


def test_fetch_youtube_statuses_and_analytics(root, monkeypatch):
    posts, _ = metrics.collect_posts(root)
    monkeypatch.setattr(youtube, "video_stats", lambda ids: {
        "AAAAAAAAAAA": {"title": "BoF4 #1", "published_at": "2026-10-10T00:00:00Z", "privacy": "public",
                        "publish_at": None, "views": 100, "likes": 5, "comments": 1},
        "BBBBBBBBBBB": {"title": "Short", "published_at": "2026-10-06T00:00:00Z", "privacy": "private",
                        "publish_at": "2026-10-20T00:00:00Z", "views": 0},
    })
    monkeypatch.setattr(youtube, "has_scopes", lambda *s: True)
    seen = {}

    def fake_analytics(ids, start, end):
        seen.update(ids=ids, start=start)
        return {"AAAAAAAAAAA": {"averageViewPercentage": 41.5, "subscribersGained": 2, "views": 90}}

    monkeypatch.setattr(youtube, "analytics_by_video", fake_analytics)
    notes: list[str] = []
    metrics.fetch_youtube(posts, notes, today=dt.date(2026, 10, 15))
    by = {p.item: p for p in posts if p.platform == "youtube"}
    assert by["seg2"].status == "public" and by["seg2"].title == "BoF4 #1"
    assert by["seg2"].m == {"views": 100, "likes": 5, "comments": 1, "avg_view_pct": 41.5, "subs_gained": 2}
    assert by["c01"].status == "scheduled" and by["c01"].published_at == "2026-10-20T00:00:00Z"
    assert by["c02"].status == "missing"
    assert seen == {"ids": ["AAAAAAAAAAA"], "start": dt.date(2026, 10, 10)}  # public ones only


def test_fetch_youtube_without_analytics_scope_notes_it(root, monkeypatch):
    posts, _ = metrics.collect_posts(root)
    monkeypatch.setattr(youtube, "video_stats", lambda ids: {})
    monkeypatch.setattr(youtube, "has_scopes", lambda *s: False)
    notes: list[str] = []
    metrics.fetch_youtube(posts, notes)
    assert any("stream-auth youtube" in n for n in notes)


def test_fetch_instagram_falls_back_without_insights_permission(root, monkeypatch):
    posts, _ = metrics.collect_posts(root)
    calls = []

    def fake(media_id, insights=True):
        calls.append(insights)
        if insights:
            raise instagram.InstagramError("(#10) permission", 10)
        return {"timestamp": "2026-10-06T00:00:00+0000", "likes": 7, "comments": 2}

    monkeypatch.setattr(instagram, "media_metrics", fake)
    notes: list[str] = []
    metrics.fetch_instagram(posts, notes)
    reel = next(p for p in posts if p.kind == "reel")
    assert reel.status == "public" and reel.m == {"likes": 7, "comments": 2}
    assert calls == [True, False]
    assert any("instagram_manage_insights" in n for n in notes)


def test_media_metrics_skips_metrics_meta_rejects(monkeypatch, tmp_path):
    tok = tmp_path / "ig.json"
    tok.write_text('{"page_token": "T", "ig_user_id": "1"}', encoding="utf-8")
    monkeypatch.setattr(instagram, "TOKEN_FILE", tok)

    def fake_call(method, path, token, **params):
        if not path.endswith("/insights"):
            return {"timestamp": "t", "like_count": 3, "comments_count": 1}
        names = params["metric"].split(",")
        if "ig_reels_avg_watch_time" in names:
            raise instagram.InstagramError("(#100) metric not supported", 100)
        return {"data": [{"name": n, "values": [{"value": 10}]} for n in names]}

    monkeypatch.setattr(instagram, "_call", fake_call)
    got = instagram.media_metrics("111")
    assert got["likes"] == 3 and got["views"] == 10 and got["reach"] == 10
    assert "ig_reels_avg_watch_time" not in got


def test_fetch_twitch_clips_and_expired_vod(root):
    posts, _ = metrics.collect_posts(root)

    class FakeHelix:
        def clips(self, ids):
            return {"Clip1": {"id": "Clip1", "view_count": 42, "created_at": "2026-10-06T00:00:00Z"}}

        def video(self, vod_id):
            return None

    notes: list[str] = []
    metrics.fetch_twitch(posts, notes, helix_factory=FakeHelix)
    clip = next(p for p in posts if p.kind == "twitch_clip")
    vod = next(p for p in posts if p.kind == "vod")
    assert clip.m == {"views": 42} and clip.status == "public"
    assert vod.status == "expired"


def test_parse_twitch_csv_loose_headers(tmp_path):
    p = tmp_path / "channel.csv"
    p.write_text("﻿Date,Average Viewers,Max Viewers,New Followers,Chatters,Revenue\n"
                 "10/14/2026,12.5,20,\"1,203\",9,0\n"
                 "2026-10-15,8,15,3,,0\n"
                 "not a date,1,1,1,1,1\n", encoding="utf-8")
    days, notes = metrics.parse_twitch_csv(p)
    assert days[dt.date(2026, 10, 14)] == {"avg_viewers": 12.5, "peak_viewers": 20.0, "follows": 1203.0,
                                           "chatters": 9.0}
    assert days[dt.date(2026, 10, 15)]["chatters"] is None
    assert len(days) == 2
    assert notes == ["Twitch CSV columns not used: Revenue"]


def test_spearman():
    assert metrics.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert metrics.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    assert metrics.spearman([1, 2, 3], [1, 2, 3]) is None  # too few to say


def _post(kind, cat, views, item, days_old=10.0, platform=None):
    published = (NOW - dt.timedelta(days=days_old)).isoformat()
    platform = platform or {"short": "youtube", "episode": "youtube", "reel": "instagram"}.get(kind, "twitch")
    return Post(platform, kind, "2026-10-05", item, item, "u", f"{cat} {item}", cat,
                published_at=published, status="public", m={"views": views, "likes": views // 10})


def test_report_index_shows_what_works_where():
    posts = [
        _post("short", "japanese", 1000, "c01"), _post("short", "japanese", 900, "c02"),
        _post("short", "gaming", 100, "c03"), _post("short", "gaming", 120, "c04"),
        _post("reel", "japanese", 50, "c01"), _post("reel", "japanese", 40, "c02"),
        _post("reel", "gaming", 500, "c03"), _post("reel", "gaming", 450, "c04"),
        _post("short", "gaming", 5, "c05", days_old=1),  # new
    ]
    md = metrics.build_report(posts, ["note"], now=NOW)
    assert "## Which content does better than usual, per platform" in md
    rows = {line.split("|")[1].strip(): line for line in md.splitlines() if line.startswith("| japanese")
            or line.startswith("| gaming")}
    # japanese beats the Shorts median, gaming beats the Reels median
    assert "| 1.86x (n=2) | 0.18x (n=2) |" in rows["japanese"]
    assert "| 0.22x (n=2) | 1.90x (n=2) |" in rows["gaming"]  # the 1-day-old Short isn't judged yet
    assert "YouTube Shorts vs Instagram Reels: rank agreement -0.80 over 4 clips" in md  # opposite winners
    assert "*new*" in md
    assert "No Channel Analytics CSV yet" in md


def test_report_twitch_days_split_by_game(root):
    from wabisanji_post import sessions as S

    s = S.load_session(root / "2026-10-05")
    md = metrics.build_report([], [], now=NOW, twitch_days={dt.date(2026, 10, 5): {"avg_viewers": 10.0,
                                                                                     "follows": 4.0}},
                              days_sessions={dt.date(2026, 10, 5): [s]})
    assert "### By game" in md
    assert "Alchemy Factory" in md and "Breath of Fire IV (JP)" in md


def test_merge_twitch_csvs_joins_periods_and_files(tmp_path):
    a = tmp_path / "engagement_sep.csv"
    a.write_text("Date,Average Viewers,Follows\n2026-09-15,3,1\n2026-09-16,2,\n", encoding="utf-8")
    b = tmp_path / "engagement_oct.csv"
    b.write_text("Date,Average Viewers,Follows\n2026-09-16,2.5,2\n2026-10-01,4,3\n", encoding="utf-8")
    c = tmp_path / "revenue.csv"
    c.write_text("Date,Hours Streamed,Follows\n2026-09-16,5,\n", encoding="utf-8")
    days, _ = metrics.merge_twitch_csvs([a, b, c])
    assert days[dt.date(2026, 9, 15)] == {"avg_viewers": 3.0, "follows": 1.0}
    assert days[dt.date(2026, 9, 16)] == {"avg_viewers": 2.5, "follows": 2.0, "hours_streamed": 5.0}  # blank kept 2
    assert days[dt.date(2026, 10, 1)]["follows"] == 3.0


# --- thumbnail reach (impressions, CTR) -------------------------------------------------------

REACH_HEAD = "date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"


def test_collect_posts_reads_thumbnail_change(root):
    t = root / "2026-10-05" / "vods" / "seg2_thumbs"
    t.mkdir(parents=True)
    (t / "pushed.json").write_text('{"video_id": "AAAAAAAAAAA", "pushed_at": "2026-10-16T18:53:00+11:00"}',
                                   encoding="utf-8")
    posts, _ = metrics.collect_posts(root)
    ep = next(p for p in posts if p.platform == "youtube" and p.item == "seg2")
    assert ep.thumb_at == "2026-10-16T18:53:00+11:00"


def test_load_reach_sums_rows_and_reads_percent_or_fraction(tmp_path):
    (tmp_path / "2026-10-10.csv").write_text(
        REACH_HEAD + "20261010,UC1,AAA,100,2.0\n20261010,UC1,AAA,100,4.0\n20261010,UC1,BBB,50,10.0\n", encoding="utf-8")
    got = metrics.load_reach(tmp_path)
    assert got["AAA"][dt.date(2026, 10, 10)] == pytest.approx([200, 6.0])   # 2 % and 4 % of 100 each
    assert got["BBB"][dt.date(2026, 10, 10)] == pytest.approx([50, 5.0])
    frac = tmp_path / "f"
    frac.mkdir()
    (frac / "a.csv").write_text(REACH_HEAD + "20261010,UC1,AAA,100,0.05\n", encoding="utf-8")
    assert metrics.load_reach(frac)["AAA"][dt.date(2026, 10, 10)] == pytest.approx([100, 5.0])


def test_fetch_reach_before_and_after_the_change():
    p = _post("episode", "gaming", 500, "seg1")
    p.ref, p.thumb_at = "AAA", "2026-10-16T18:53:00+11:00"
    days = {dt.date(2026, 10, 10): [100, 2.0], dt.date(2026, 10, 15): [100, 2.0],
            dt.date(2026, 10, 16): [500, 50.0],                      # change day: left out of both sides
            dt.date(2026, 10, 17): [100, 6.0], dt.date(2026, 9, 1): [1000, 0.0]}   # outside the window
    metrics.fetch_reach([p], {"AAA": days})
    assert p.m["impressions"] == 1800
    assert (p.m["imp_before"], p.m["ctr_before"]) == (200, pytest.approx(0.02))
    assert (p.m["imp_after"], p.m["ctr_after"]) == (100, pytest.approx(0.06))


def test_report_shows_ctr_and_before_after():
    a, b = _post("episode", "gaming", 500, "seg1"), _post("episode", "japanese", 300, "seg2")
    a.m.update(impressions=1000, ctr=0.04, imp_before=400, ctr_before=0.02, imp_after=300, ctr_after=0.06)
    b.m.update(impressions=200, ctr=0.05, imp_before=100, ctr_before=0.05, imp_after=100, ctr_after=0.05)
    md = metrics.build_report([a, b], [], now=NOW)
    assert "## Thumbnails: impressions and click-through" in md
    # (400*2% + 100*5%) / 500 = 2.6 %; (300*6% + 100*5%) / 400 = 5.75 %
    assert "CTR 2.6% on 500 impressions before, 5.8% on 400 after (+3.1 points)" in md
    assert "### Episode CTR, best first (100+ impressions)" in md


def test_sync_reach_downloads_newest_report_per_day(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "has_scopes", lambda *s: True)
    monkeypatch.setattr(youtube, "reporting_job", lambda: "job1")
    monkeypatch.setattr(youtube, "reporting_reports", lambda job: [
        {"id": "r1", "startTime": "2026-10-14T07:00:00Z", "createTime": "2026-10-16T01:00:00Z", "downloadUrl": "u1"},
        {"id": "r2", "startTime": "2026-10-14T07:00:00Z", "createTime": "2026-10-17T01:00:00Z", "downloadUrl": "u2"},
        {"id": "r3", "startTime": "2026-10-15T07:00:00Z", "createTime": "2026-10-17T01:00:00Z", "downloadUrl": "u3"},
    ])
    fetched = []

    def download(url):
        fetched.append(url)
        return REACH_HEAD + "20261014,UC1,AAA,10,1.0\n"

    monkeypatch.setattr(youtube, "download_report", download)
    notes: list[str] = []
    d = metrics.sync_reach(tmp_path, notes)
    assert sorted(fetched) == ["u2", "u3"] and notes == []
    assert sorted(p.name for p in d.glob("*.csv")) == ["2026-10-14.csv", "2026-10-15.csv"]
    metrics.sync_reach(tmp_path, notes)
    assert sorted(fetched) == ["u2", "u3"]   # nothing new: nothing downloaded again


def test_sync_reach_explains_a_disabled_api(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "has_scopes", lambda *s: True)

    def job():
        raise youtube.YouTubeError("reporting jobs failed (403): YouTube Reporting API has not been used in project 1")

    monkeypatch.setattr(youtube, "reporting_job", job)
    notes: list[str] = []
    metrics.sync_reach(tmp_path, notes)
    assert "Enable the YouTube Reporting API" in notes[0]
