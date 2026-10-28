"""stream-viewers: live viewer logging and matching samples to segments (no network)."""
from __future__ import annotations

import datetime as dt
import json

from wabisanji_post import metrics, viewers
from wabisanji_post.models import Session

import yaml

from test_stream_models import APPROVED

START = "2026-10-05T00:00:00Z"  # 11:00 AEDT; the APPROVED session's recording starts 10:37:11 +11:00


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_record_logs_while_live_and_stops_after_grace(tmp_path):
    states = [None, None, {"viewer_count": 3, "game_name": "Alchemy Factory", "title": "t", "started_at": START},
              {"viewer_count": 5, "game_name": "Alchemy Factory", "title": "t", "started_at": START}] + [None] * 20

    class Helix:
        def stream(self):
            return states.pop(0)

    clock = FakeClock()
    t0 = dt.datetime(2026, 10, 5, 0, 1, tzinfo=dt.timezone.utc)
    written = viewers.record(tmp_path, Helix(), poll=60, offline_grace=180, sleep=clock.sleep, clock=clock,
                             now=lambda: t0 + dt.timedelta(seconds=clock.t), say=lambda m: None)
    assert len(written) == 1 and written[0].parent.name == "_viewers"
    lines = [json.loads(x) for x in written[0].read_text(encoding="utf-8").splitlines()]
    assert [x["viewers"] for x in lines] == [3, 5]
    assert clock.t == 60 * 6  # 2 waiting, 2 live, then the 3rd offline poll (180 s after the last live one) stops


def test_record_no_wait_returns_when_offline(tmp_path):
    class Helix:
        def stream(self):
            return None

    assert viewers.record(tmp_path, Helix(), wait=False, sleep=lambda s: None, say=lambda m: None) == []


def test_segment_viewers_matches_samples_to_segments(tmp_path):
    s = Session.model_validate(yaml.safe_load(APPROVED))
    t0 = s.recording_start.astimezone(dt.timezone.utc)
    samples = [{"utc": t0 + dt.timedelta(minutes=m), "viewers": v} for m, v in
               [(5, 1),      # Starting Soon: skip segment
                (20, 4), (30, 6),  # segment 2 (00:15:00-00:51:50), Breath of Fire IV
                (60, 2), (90, 8)]]  # segment 3 (00:52:00-02:26:20), Alchemy Factory
    rows = viewers.segment_viewers([s], samples)
    assert [(r["segment"], r["samples"], r["avg"], r["peak"]) for r in rows] == [(2, 2, 5.0, 6), (3, 2, 5.0, 8)]
    assert rows[1]["game"] == "Alchemy Factory"


def test_load_samples_sorted(tmp_path):
    d = tmp_path / "_viewers"
    d.mkdir()
    (d / "b.jsonl").write_text('{"utc": "2026-10-05T01:00:00+00:00", "viewers": 2}\n', encoding="utf-8")
    (d / "a.jsonl").write_text('{"utc": "2026-10-05T02:00:00+00:00", "viewers": 1}\n\n', encoding="utf-8")
    got = viewers.load_samples(tmp_path)
    assert [x["viewers"] for x in got] == [2, 1]


def test_follows_by_day_and_merge():
    follows = metrics.follows_by_day([{"followed_at": "2026-10-04T23:30:00Z"},   # 10:30 on 5 Oct in Melbourne
                                      {"followed_at": "2026-10-05T01:00:00Z"}])
    assert sum(follows.values()) == 2
    days = {dt.date(2026, 10, 5): {"follows": 7.0}}
    merged = metrics.add_follows(days, {dt.date(2026, 10, 5): 2, dt.date(2026, 10, 6): 1})
    assert merged[dt.date(2026, 10, 5)]["follows"] == 7.0  # the dashboard's own count wins
    assert merged[dt.date(2026, 10, 6)]["follows"] == 1.0


def test_report_viewers_per_game_and_off_stream_follows():
    s = Session.model_validate(yaml.safe_load(APPROVED))
    rows = [{"session": "2026-10-05", "segment": 3, "game": "Alchemy Factory", "series": None, "type": "gaming",
             "samples": 60, "avg": 4.0, "peak": 9}]
    md = metrics.build_report([], [], now=dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc),
                              twitch_days={dt.date(2026, 10, 5): {"follows": 2.0}, dt.date(2026, 10, 6): {"follows": 3.0}},
                              days_sessions={dt.date(2026, 10, 5): [s]}, viewer_rows=rows)
    assert "| Alchemy Factory | 1 | 1.0 | 4.0 | 9 |" in md
    assert "3 follows came on days without a stream" in md


def test_by_game_shows_dash_without_viewer_data_and_counts_zero_follow_days():
    s = Session.model_validate(yaml.safe_load(APPROVED))
    md = metrics.build_report([], [], now=dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc),
                              twitch_days={dt.date(2026, 10, 5): {"follows": 0.0}},
                              days_sessions={dt.date(2026, 10, 5): [s]})
    row = next(line for line in md.splitlines() if line.startswith("| Alchemy Factory |"))
    assert row.endswith("| – | 0.00 | – |")  # no viewer CSV: a dash, not 0; a 0-follow day still counts
    assert "No Channel Analytics CSV yet" in md
