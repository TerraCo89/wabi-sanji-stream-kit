"""social.yaml (the Instagram gate), the Instagram client's call order, and stream-social (no network)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
import yaml
from typer.testing import CliRunner

from wabisanji_post import cli, instagram, publish, social
from wabisanji_post.models import ClipsFile

from test_stream_models import APPROVED, clips


CAPTION = "I finally read this line without help.\n\n読めた (yometa): I could read it!\n\nLink in bio.\n\n#wabisanji #learnjapanese"


def clips_file(**changes) -> ClipsFile:
    return ClipsFile.model_validate({**clips(**{"approved": True, **changes}), "status": "approved"})


# ---------------------------------------------------------------------------
# social.yaml
# ---------------------------------------------------------------------------

def test_add_missing_keeps_existing_entries():
    cf = clips_file()
    sf, added = social.add_missing(None, "2026-10-05", cf)
    assert added == ["c01"] and sf.items["c01"].video == "shorts/c01.mp4"
    assert sf.items["c01"].instagram.approved is False
    sf.items["c01"].instagram.caption = "Sam's edit"
    sf, added = social.add_missing(sf, "2026-10-05", cf)
    assert added == [] and sf.items["c01"].instagram.caption == "Sam's edit"


def test_add_missing_skips_unapproved_and_non_shorts():
    assert social.add_missing(None, "2026-10-05", clips_file(approved=False))[1] == []
    assert social.add_missing(None, "2026-10-05", clips_file(targets=["twitch"]))[1] == []


def test_social_round_trip_block_caption(tmp_path):
    sf = social.SocialFile(session="2026-10-05", items={
        "c01": social.SocialItem(video="shorts/c01.mp4", instagram=social.PlatformPost(caption=CAPTION, cover_ms=1500))})
    p = social.save_social(tmp_path, sf)
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# social.yaml")
    assert "caption: |\n" in text and "読めた" in text
    assert yaml.safe_load(text)["session"] == "2026-10-05"  # quoted: stays a string, not a date
    again = social.load_social(tmp_path)
    assert again.items["c01"].instagram.caption.strip() == CAPTION
    assert again.items["c01"].instagram.cover_ms == 1500
    assert social.load_social(tmp_path / "nope") is None


def test_caption_problems():
    assert social.caption_problems(CAPTION) == []
    assert social.caption_problems("  ") == ["no caption yet"]
    assert "2201 characters" in social.caption_problems("x" * 2201)[0]
    assert "31 hashtags" in social.caption_problems(" ".join(f"#t{i}" for i in range(31)))[0]
    assert "link" in social.caption_problems("watch it at https://youtu.be/x")[0]
    assert "em dash" in social.caption_problems("this — that")[0]


def test_instagram_plan_states(tmp_path):
    (tmp_path / "shorts").mkdir()
    (tmp_path / "shorts" / "c01.mp4").write_bytes(b"x")
    cf = clips_file()
    post = social.PlatformPost(caption=CAPTION)
    sf = social.SocialFile(session="2026-10-05", items={
        "c01": social.SocialItem(video="shorts/c01.mp4", instagram=post),
        "c09": social.SocialItem(video="shorts/c09.mp4", instagram=social.PlatformPost(caption=CAPTION, approved=True)),
    })

    def states(posted=None):
        return {r.key: (r.state, r.reason) for r in social.instagram_plan(tmp_path, cf, sf, posted or {}).rows}

    assert states()["c01"] == ("waiting", "not approved yet")
    assert states()["c09"][0] == "blocked" and "clips.yaml" in states()["c09"][1]
    post.approved = True
    assert states()["c01"] == ("ready", "")
    post.caption = ""
    assert states()["c01"] == ("blocked", "no caption yet")
    post.caption = CAPTION
    (tmp_path / "shorts" / "c01.mp4").unlink()
    assert states()["c01"][0] == "blocked" and "stream-render-shorts" in states()["c01"][1]
    rec = publish.InstagramRecord(media_id="1", permalink="https://www.instagram.com/reel/x/", posted_at="t")
    assert states({"c01": rec})["c01"] == ("posted", "https://www.instagram.com/reel/x/")


def test_publish_log_keeps_instagram(tmp_path):
    from wabisanji_post.models import Session

    s = Session.model_validate(yaml.safe_load(APPROVED))
    log = publish.load_log(tmp_path, s)
    log.instagram["c01"] = publish.InstagramRecord(media_id="179", container_id="555", posted_at="2026-10-14T00:00:00+00:00")
    publish.save_log(tmp_path, log)
    assert publish.load_log(tmp_path, s).instagram["c01"].media_id == "179"


# ---------------------------------------------------------------------------
# Instagram client
# ---------------------------------------------------------------------------

class Resp:
    def __init__(self, status: int = 200, body: dict | None = None):
        self.status_code, self._body = status, body or {}
        self.ok = status < 400
        self.headers = {"Content-Type": "application/json"}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


@pytest.fixture
def logged_in(tmp_path, monkeypatch):
    tok = tmp_path / "instagram.token.json"
    tok.write_text(json.dumps({"page_id": "P1", "page_name": "Wabi Sanji", "page_token": "PAGETOK",
                               "ig_user_id": "IG1", "ig_username": "wabisanji", "obtained_at": "x"}))
    monkeypatch.setattr(instagram, "TOKEN_FILE", tok)
    video = tmp_path / "c01.mp4"
    video.write_bytes(b"\0" * 1234)
    return video


def test_publish_reel_call_order(logged_in, monkeypatch):
    calls: list[tuple] = []
    statuses = iter(["IN_PROGRESS", "FINISHED"])

    def fake_request(method, url, timeout=None, params=None, **kw):
        calls.append((method, url.removeprefix(instagram.GRAPH), dict(params)))
        if url.endswith("/IG1/media"):
            return Resp(body={"id": "C9", "uri": "https://rupload.facebook.com/ig-api-upload/C9"})
        if url.endswith("/C9"):
            return Resp(body={"status_code": next(statuses)})
        if url.endswith("/IG1/media_publish"):
            return Resp(body={"id": "M7"})
        raise AssertionError(url)

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append(("UPLOAD", url, headers, len(data.read())))
        return Resp(body={"success": True})

    monkeypatch.setattr(instagram.requests, "request", fake_request)
    monkeypatch.setattr(instagram.requests, "post", fake_post)
    res = instagram.publish_reel(logged_in, "hello", cover_ms=2000, sleep=lambda s: None)
    assert res == {"media_id": "M7", "container_id": "C9"}

    create, upload, poll1, poll2, pub = calls
    assert create[0] == "POST" and create[1] == "/IG1/media"
    assert create[2].items() >= {"media_type": "REELS", "upload_type": "resumable", "caption": "hello",
                              "share_to_feed": "true", "thumb_offset": "2000", "access_token": "PAGETOK"}.items()
    assert upload[1] == f"{instagram.RUPLOAD}/C9"
    assert upload[2]["Authorization"] == "OAuth PAGETOK"
    assert upload[2]["offset"] == "0" and upload[2]["file_size"] == "1234" and upload[3] == 1234
    assert poll1[1] == poll2[1] == "/C9"
    assert pub[1] == "/IG1/media_publish" and pub[2]["creation_id"] == "C9"


def test_publish_reel_processing_error(logged_in, monkeypatch):
    def fake_request(method, url, timeout=None, params=None, **kw):
        if url.endswith("/media"):
            return Resp(body={"id": "C9"})
        if url.endswith("/C9"):
            return Resp(body={"status_code": "ERROR", "status": "Error: unsupported video (2207026)"})
        raise AssertionError("must not publish")

    monkeypatch.setattr(instagram.requests, "request", fake_request)
    monkeypatch.setattr(instagram.requests, "post", lambda *a, **k: Resp(body={"success": True}))
    with pytest.raises(instagram.InstagramError, match="2207026"):
        instagram.publish_reel(logged_in, "hello", sleep=lambda s: None)


def test_graph_error_carries_code(logged_in, monkeypatch):
    monkeypatch.setattr(instagram.requests, "request", lambda *a, **k: Resp(400, {"error": {
        "message": "Error validating access token", "code": 190}}))
    with pytest.raises(instagram.InstagramError) as e:
        instagram.publish_reel(logged_in, "hello")
    assert e.value.code == 190 and e.value.stop and "Error validating access token" in str(e.value)


def test_pick_page():
    other = {"id": "1", "name": "Other Page", "instagram_business_account": {"id": "a"}}
    ws = {"id": "2", "name": "Wabi Sanji", "instagram_business_account": {"id": "b"}}
    bare = {"id": "3", "name": "No IG"}
    assert instagram._pick_page([bare, ws]) is ws
    assert instagram._pick_page([other, ws], "Wabi Sanji") is ws
    with pytest.raises(instagram.InstagramError, match="INSTAGRAM_PAGE"):
        instagram._pick_page([other, ws])
    with pytest.raises(instagram.InstagramError, match="no Page"):
        instagram._pick_page([bare])


def test_same_caption():
    assert instagram.same_caption("a  b\nc", "A b c")
    assert not instagram.same_caption("a b", "a b c")


# ---------------------------------------------------------------------------
# stream-social
# ---------------------------------------------------------------------------

@pytest.fixture
def session_dir(tmp_path) -> Path:
    d = tmp_path / "2026-10-05"
    (d / "shorts").mkdir(parents=True)
    (d / "session.yaml").write_text(APPROVED.replace("status: segments_approved", "status: rendered"), encoding="utf-8")
    (d / "clips.yaml").write_text(yaml.safe_dump({**clips(approved=True), "status": "approved"}), encoding="utf-8")
    (d / "shorts" / "c01.mp4").write_bytes(b"x")
    sf = social.SocialFile(session="2026-10-05", items={
        "c01": social.SocialItem(video="shorts/c01.mp4", instagram=social.PlatformPost(caption=CAPTION, approved=True))})
    social.save_social(d, sf)
    return d


def run(*args):
    app = typer.Typer()
    app.command()(cli.stream_social)
    return CliRunner().invoke(app, [str(a) for a in args])


@pytest.fixture
def fake_ig(monkeypatch):
    state = {"posted": [], "recent": []}
    monkeypatch.setattr(instagram, "publishing_quota", lambda: {"quota_used": 0, "quota_total": 100})
    monkeypatch.setattr(instagram, "recent_media", lambda: state["recent"])
    monkeypatch.setattr(instagram, "permalink", lambda mid: f"https://www.instagram.com/reel/{mid}/")

    def fake_publish(path, caption, cover_ms=None, on_stage=None):
        state["posted"].append((path.name, caption))
        return {"media_id": "M1", "container_id": "C1"}

    monkeypatch.setattr(instagram, "publish_reel", fake_publish)
    return state


def test_stream_social_dry_run_posts_nothing(session_dir, fake_ig):
    r = run(session_dir)
    assert r.exit_code == 0, r.output
    assert "c01: ready" in r.output and fake_ig["posted"] == []


def test_stream_social_go_posts_and_records_once(session_dir, fake_ig):
    r = run(session_dir, "--go")
    assert r.exit_code == 0, r.output
    assert fake_ig["posted"] == [("c01.mp4", CAPTION)]
    log = yaml.safe_load((session_dir / "publish.yaml").read_text(encoding="utf-8"))
    assert log["instagram"]["c01"]["media_id"] == "M1"
    assert log["instagram"]["c01"]["permalink"] == "https://www.instagram.com/reel/M1/"
    r = run(session_dir, "--go")
    assert "c01: posted" in r.output and len(fake_ig["posted"]) == 1


def test_stream_social_records_existing_reel_instead_of_reposting(session_dir, fake_ig):
    fake_ig["recent"] = [{"id": "OLD", "caption": CAPTION.replace("\n\n", "\n"), "permalink": "https://ig/old",
                          "timestamp": "2026-10-13T01:00:00+0000"}]
    r = run(session_dir, "--go")
    assert r.exit_code == 0, r.output
    assert fake_ig["posted"] == [] and "already on Instagram" in r.output
    log = yaml.safe_load((session_dir / "publish.yaml").read_text(encoding="utf-8"))
    assert log["instagram"]["c01"]["media_id"] == "OLD"


def test_stream_social_unapproved_never_posts(session_dir, fake_ig):
    sf = social.load_social(session_dir)
    sf.items["c01"].instagram.approved = False
    social.save_social(session_dir, sf)
    r = run(session_dir, "--go")
    assert "not approved yet" in r.output and fake_ig["posted"] == []


def test_stream_social_init_adds_entries(session_dir, fake_ig):
    (session_dir / "social.yaml").unlink()
    r = run(session_dir, "--init")
    assert r.exit_code == 0, r.output
    sf = social.load_social(session_dir)
    assert sf.items["c01"].instagram.caption == "" and not sf.items["c01"].instagram.approved
    assert "no caption yet" in r.output


def test_fit_for_upload_keeps_small_files(logged_in, tmp_path):
    assert instagram.fit_for_upload(logged_in, tmp_path / "out") == logged_in
    assert not (tmp_path / "out").exists()
