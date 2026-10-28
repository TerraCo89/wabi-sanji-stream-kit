"""Chat recorder client, chat signals, viewer dedupe (no network)."""
from __future__ import annotations

import datetime as dt
import json

import pytest
import requests
import yaml

from wabisanji_post import metrics, recorder, signals, viewers
from wabisanji_post.models import Session

from test_stream_models import APPROVED

UTC = dt.timezone.utc


@pytest.fixture
def session():
    return Session.model_validate(yaml.safe_load(APPROVED))  # recording 2026-10-05 10:37:11 +11:00


def at(s: Session, offset: float) -> dt.datetime:
    return (s.recording_start + dt.timedelta(seconds=offset)).astimezone(UTC)


def privmsg(msg_id, user, text, when: dt.datetime, uid="1", badges=""):
    ts = int(when.timestamp() * 1000)
    return {"t": when, "line": f"@badges={badges};display-name={user};id={msg_id};tmi-sent-ts={ts};user-id={uid} "
                               f":{user.lower()}!x@x.tmi.twitch.tv PRIVMSG #wabisanji :{text}"}


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.ok = 200 <= status < 300
        self.text = json.dumps(body)

    def json(self):
        return self._body


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("RECORDER_TOKEN", "t" * 32)
    monkeypatch.setenv("RECORDER_URL", "http://box:8787/")


def test_utc_dates_span_midnight():
    a = dt.datetime(2026, 10, 15, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=11)))  # 2026-10-14 22:00 UTC
    assert recorder.utc_dates(a, a + dt.timedelta(hours=5)) == ["2026-10-14", "2026-10-15"]


def test_chat_lines_keeps_the_window(env, monkeypatch):
    calls = []

    def fake_get(url, params, headers, timeout):
        calls.append((url, params["date"], headers["Authorization"]))
        return Resp(200, {"lines": [{"t": "2026-10-14T21:59:00.000Z", "line": "a"},
                                    {"t": "2026-10-14T22:30:00.000Z", "line": "b"}]})

    monkeypatch.setattr(recorder.requests, "get", fake_get)
    start = dt.datetime(2026, 10, 14, 22, 0, tzinfo=UTC)
    got = recorder.chat_lines(start, start + dt.timedelta(hours=1))
    assert [r["line"] for r in got] == ["b"]
    assert calls == [("http://box:8787/v1/archive/wabisanji", "2026-10-14", "Bearer " + "t" * 32)]


def test_recorder_errors_are_clear(env, monkeypatch):
    monkeypatch.setattr(recorder.requests, "get", lambda *a, **k: Resp(401, {"error": "unauthorized"}))
    with pytest.raises(recorder.RecorderError, match="HISTORY_TOKEN"):
        recorder.chat_lines(dt.datetime(2026, 10, 14, tzinfo=UTC), dt.datetime(2026, 10, 14, 1, tzinfo=UTC))
    monkeypatch.setattr(recorder.requests, "get", lambda *a, **k: Resp(404, {"error": "not found"}))
    with pytest.raises(recorder.RecorderError, match="redeploy"):
        recorder.pull_viewers(__import__("pathlib").Path("."), ["2026-10-14"])

    def down(*a, **k):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(recorder.requests, "get", down)
    with pytest.raises(recorder.RecorderError, match="unreachable"):
        recorder.pull_viewers(__import__("pathlib").Path("."), ["2026-10-14"])


def test_no_token_is_a_recorder_error(monkeypatch):
    monkeypatch.setattr(recorder.secrets, "get", lambda key, default="": "")
    with pytest.raises(recorder.RecorderError, match="RECORDER_TOKEN"):
        recorder.pull_viewers(__import__("pathlib").Path("."), ["2026-10-14"])


def test_pull_viewers_writes_days_with_samples(env, monkeypatch, tmp_path):
    sample = {"utc": "2026-10-14T22:01:00+00:00", "viewers": 3, "game": "g", "title": "t", "started_at": "x"}
    monkeypatch.setattr(recorder.requests, "get", lambda url, params, **k: Resp(
        200, {"samples": [sample] if params["date"] == "2026-10-14" else []}))
    assert recorder.pull_viewers(tmp_path / "_viewers", ["2026-10-14", "2026-10-15", "2026-10-14"]) == 1
    assert [p.name for p in (tmp_path / "_viewers").iterdir()] == ["recorder_2026-10-14.jsonl"]


def test_chat_signals(session):
    lines = [
        privmsg("m0", "Early", "before the stream", at(session, -60)),           # before recording: dropped
        privmsg("m1", "Ana", "hi", at(session, 20 * 60)),                         # segment 2
        privmsg("m2", "Ana", "lol", at(session, 20 * 60 + 5)),
        privmsg("m3", "Ben", "LOL", at(session, 20 * 60 + 9), uid="2"),
        privmsg("m4", "Sam", "thanks!", at(session, 20 * 60 + 12), uid="9", badges="broadcaster/1"),
        privmsg("m5", "Spam", "buy followers", at(session, 21 * 60), uid="3"),
        {"t": at(session, 21 * 60 + 5), "line": "@target-msg-id=m5 :tmi.twitch.tv CLEARMSG #wabisanji :buy followers"},
        privmsg("m6", "Cat", "nice", at(session, 30 * 60), uid="4"),
        {"t": at(session, 60 * 60), "line": "@msg-id=raid;display-name=Raider;system-msg=5\\sraiders "
                                            ":tmi.twitch.tv USERNOTICE #wabisanji"},
    ]
    got = signals.chat_signals(session, lines)
    assert got["status"] == "ok" and got["count"] == 5  # m0 outside, m5 deleted
    seg2 = next(x for x in got["per_segment"] if x["segment"] == 2)
    assert seg2["messages"] == 4 and seg2["chatters"] == 3  # the broadcaster isn't counted
    assert got["bursts"] == [{"t": 1200.0, "hms": "00:20:00", "segment": 2, "count": 3}]
    assert got["events"] == [{"t": 3600.0, "hms": "01:00:00", "segment": 3, "user": "Raider", "type": "raid",
                              "text": "5 raiders"}]
    assert [m["user"] for m in got["messages"]] == ["Ana", "Ana", "Ben", "Sam", "Cat"]


def test_load_samples_one_per_minute(tmp_path):
    d = tmp_path / "_viewers"
    d.mkdir()
    (d / "2026-10-15_0903.jsonl").write_text('{"utc": "2026-10-14T22:05:12+00:00", "viewers": 2}\n', encoding="utf-8")
    (d / "recorder_2026-10-14.jsonl").write_text('{"utc": "2026-10-14T22:05:40+00:00", "viewers": 2}\n'
                                                 '{"utc": "2026-10-14T22:06:40+00:00", "viewers": 3}\n', encoding="utf-8")
    assert [s["viewers"] for s in viewers.load_samples(tmp_path)] == [2, 3]


def test_report_chat_per_game():
    rows = [{"session": "a", "segment": 2, "game": "Alchemy Factory", "messages": 30, "chatters": 4, "hours": 2.0},
            {"session": "b", "segment": 1, "game": "Alchemy Factory", "messages": 10, "chatters": 2, "hours": 2.0}]
    md = metrics.build_report([], [], now=dt.datetime(2026, 10, 15, tzinfo=UTC), chat_rows=rows)
    assert "| Alchemy Factory | 2 | 4.0 | 10.0 | 3.0 |" in md
