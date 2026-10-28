"""ARIA: wake word, meme matching, segmenting, the listener's decisions, the
live API routes and the watch event stream. No mic, Whisper or Speaker.bot."""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from wabisanji.config import LiveConfig
from wabisanji.live import wake
from wabisanji.live.listen import FRAME, Listener, Segmenter, is_noise, to_16k
from wabisanji.live.memes import Meme, MemeMatcher, load_index, phrase_in
from wabisanji.live.store import LiveStore
from wabisanji.live.watch import watch
from wabisanji.stream.live import LiveState
from wabisanji.stream.server import create_app

WORDS = LiveConfig().wake_words
PATTERN = wake.build_pattern(WORDS)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- wake word ----------------------------------------------------------


@pytest.mark.parametrize("text, question", [
    ("Hey Aria, how do I get past the barnacles?", "how do I get past the barnacles?"),
    ("hey arya how do I open this door", "how do I open this door"),
    ("Okay Aria. Where's the next key?", "Where's the next key?"),
    ("Hey, area, what does this item do?", "what does this item do?"),
    ("Aria, how do I parry?", "how do I parry?"),
    ("So um, hey Aria, how do I beat this guy?", "how do I beat this guy?"),
    ("Hey Aria, how do I get more ammo?", "how do I get more ammo?"),
])
def test_wake_finds_questions(text, question):
    hit = wake.parse(text, PATTERN)
    assert hit == wake.Wake("question", question)


@pytest.mark.parametrize("text", [
    "This area is huge.",
    "The area boss is over there.",
    "I love an aria in an opera",   # not at the start
    "Hey there, chat!",
    "",
])
def test_wake_ignores_normal_speech(text):
    assert wake.parse(text, PATTERN) is None


@pytest.mark.parametrize("text, command", [
    ("Hey Aria.", "listen"),
    ("Aria, more.", "more"),
    ("Aria, tell me more please", "more"),
    ("Hey Aria, never mind.", "clear"),
    ("Aria, thanks!", "clear"),
    ("Hey Aria, stop searching.", "clear"),
    ("Hey Aria, stop that!", "clear"),
    ("Aria, stop.", "clear"),
    ("Hey Aria, stop the memes.", "memes_off"),
    ("Aria, say that again?", "repeat"),
    ("Hey Aria, memes off.", "memes_off"),
    ("Aria, memes on", "memes_on"),
])
def test_wake_commands(text, command):
    assert wake.parse(text, PATTERN).command == command


# --- memes --------------------------------------------------------------


def test_phrase_matching():
    assert phrase_in("it's a trap", "Oh no, it's a trap!")
    assert phrase_in("it's a trap", "its a trap")
    assert phrase_in("this is fine", "This is fine. Totally fine.")
    assert not phrase_in("trap", "a trapdoor")  # whole words only
    # 3+ words tolerate a small Whisper slip
    assert phrase_in("everything is fine", "everythings is fine here")
    # short phrases don't
    assert not phrase_in("press f", "press g")


def test_matcher_prefers_longest_phrase_and_respects_cooldowns():
    clock = Clock()
    memes = {
        "trap": Meme("trap", "trap.png", phrases=["trap"], cooldown_s=300),
        "ackbar": Meme("ackbar", "ackbar.png", phrases=["it's a trap"], cooldown_s=300),
    }
    m = MemeMatcher(memes, gap_s=20, clock=clock)
    hit = m.match("wait, it's a trap")
    assert hit.id == "ackbar"
    m.fired(hit.id)

    clock.now += 5
    assert m.match("it's a trap") is None  # global gap

    clock.now += 30
    # ackbar is cooling down, so the shorter phrase's meme can go
    assert m.match("it's a trap").id == "trap"


def test_load_index(tmp_path):
    index = tmp_path / "memes.yaml"
    index.write_text(
        "memes:\n"
        "  - id: fine\n    file: fine.png\n    phrases: ['this is fine']\n"
        "    about: calm in chaos\n",
        encoding="utf-8",
    )
    memes = load_index(index)
    assert memes["fine"].phrases == ["this is fine"]
    assert memes["fine"].cooldown_s == 300
    assert load_index(tmp_path / "missing.yaml") == {}


# --- audio --------------------------------------------------------------


def test_segmenter_cuts_on_silence():
    seg = Segmenter(min_silence_s=0.1, min_speech_s=0.05, preroll_s=0.064)
    frame = np.ones(FRAME, dtype=np.float32)
    outs = [seg.feed(frame, p) for p in [0.0, 0.0, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1, 0.1]]
    done = [o for o in outs if o is not None]
    assert len(done) == 1
    # 1 preroll + 3 voiced + 3 silent frames (cut on the third)
    assert len(done[0]) == 7 * FRAME


def test_segmenter_drops_blips():
    seg = Segmenter(min_silence_s=0.064, min_speech_s=0.3)
    frame = np.ones(FRAME, dtype=np.float32)
    outs = [seg.feed(frame, p) for p in [0.9, 0.1, 0.1, 0.1]]
    assert all(o is None for o in outs)


def test_resample_48k():
    out = to_16k(np.arange(1536, dtype=np.float32), 48000)
    assert len(out) == FRAME


def test_noise_filter():
    assert is_noise("Thank you.")
    assert is_noise("  you ")
    assert not is_noise("Thank you, Aria")


# --- listener -----------------------------------------------------------


class FakeClient:
    def __init__(self):
        self.posts: list[tuple[str, dict | None]] = []
        self.current: dict | None = None

    def post(self, path, body=None):
        self.posts.append((path, body))
        if path == "/api/live/expand":
            return {"id": "q1", "steps": ["Go left", "Jump"], "status": "expanded"}
        return {"ok": True}

    def card(self):
        return self.current


@pytest.fixture
def rig(tmp_path):
    store = LiveStore(tmp_path)
    client = FakeClient()
    spoken: list[str] = []
    clock = Clock()
    memes = {"fine": Meme("fine", "fine.png", phrases=["this is fine"])}
    listener = Listener(
        store=store, client=client, matcher=MemeMatcher(memes, clock=clock),
        wake_words=WORDS, speak=spoken.append, game=lambda: "Half-Life",
        clock=clock, out=lambda line: None,
    )
    return listener, store, client, spoken, clock


def test_listener_question(rig):
    listener, store, client, _, _ = rig
    listener.handle("Hey Aria, how do I get past the barnacles?")
    [q] = store.events("question")
    assert q["text"] == "how do I get past the barnacles?"
    assert q["game"] == "Half-Life"
    assert client.posts == [("/api/live/thinking", {"id": q["id"], "heard": q["text"]})]
    assert store.transcript()[0]["text"].startswith("Hey Aria")
    assert [u["id"] for u in store.unanswered()] == [q["id"]]


def test_listener_question_after_a_pause(rig):
    listener, store, client, _, clock = rig
    listener.handle("Hey Aria.")
    assert client.posts[0][0] == "/api/live/listening"
    clock.now += 4
    listener.handle("How do I crouch jump?")
    assert store.events("question")[0]["text"] == "How do I crouch jump?"


def test_listener_pause_expires(rig):
    listener, store, _, _, clock = rig
    listener.handle("Hey Aria.")
    clock.now += 30
    listener.handle("How do I crouch jump?")
    assert store.events("question") == []


def test_listener_stop_that_cancels_a_search(rig):
    listener, store, client, _, clock = rig
    listener.handle("Hey Aria, how do I get past the barnacles?")
    [q] = store.events("question")
    client.current = {"id": q["id"], "status": "thinking"}
    clock.now += 20
    listener.handle("Stop that.")
    assert ("/api/live/clear", None) in client.posts
    assert store.events("clear")[0]["id"] == q["id"]
    assert store.unanswered() == []


def test_listener_bare_stop_needs_a_recent_question(rig):
    listener, store, client, _, clock = rig
    listener.handle("Stop that.")  # no question yet: talking to the game or the dog
    listener.handle("Hey Aria, how do I parry?")
    clock.now += 120
    listener.handle("Stop searching.")
    assert store.events("clear") == []
    assert ("/api/live/clear", None) not in client.posts
    assert not wake.is_bare_stop("stop that guy")


def test_listener_more_speaks_steps(rig):
    listener, store, _, spoken, _ = rig
    listener.handle("Aria, more.")
    assert spoken == ["1. Go left. 2. Jump."]
    assert store.events("expand")[0]["id"] == "q1"


def test_listener_memes_and_memes_off(rig):
    listener, store, client, spoken, clock = rig
    listener.handle("Yeah, this is fine.")
    assert ("/api/live/meme", {"id": "fine"}) in client.posts
    assert store.events("meme")[0]["via"] == "phrase"

    listener.handle("Hey Aria, memes off.")
    assert store.memes_enabled() is False
    assert spoken == ["Memes off."]
    clock.now += 1000
    client.posts.clear()
    listener.handle("this is fine")
    assert client.posts == []


def test_listener_dry_run_writes_nothing(tmp_path):
    store = LiveStore(tmp_path)
    client = FakeClient()
    listener = Listener(
        store=store, client=client, matcher=MemeMatcher({}), wake_words=WORDS,
        speak=lambda t: pytest.fail("spoke in a dry run"), game=lambda: None,
        dry_run=True, out=lambda line: None,
    )
    listener.handle("Hey Aria, how do I jump?")
    listener.handle("Aria, memes off")
    assert client.posts == []
    assert not tmp_path.joinpath("transcript.jsonl").exists()
    assert not tmp_path.joinpath("state.json").exists()


# --- server routes ------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    (tmp_path / "img").mkdir()
    (tmp_path / "img" / "fine.png").write_bytes(b"\x89PNG fake")
    index = tmp_path / "memes.yaml"
    index.write_text(
        "memes:\n  - id: fine\n    file: fine.png\n  - id: gone\n    file: gone.png\n",
        encoding="utf-8",
    )
    cfg = LiveConfig(memes_dir=str(tmp_path / "img"), memes_index=str(index))
    return TestClient(create_app(tmp_path / "none.db", live_config=cfg))


def test_meme_route(api):
    first = api.get("/api/live/memes").json()
    assert first == {"seq": 0, "memes": []}
    r = api.post("/api/live/meme", json={"id": "fine"})
    assert r.status_code == 200
    data = api.get("/api/live/memes?since=0").json()
    assert [m["id"] for m in data["memes"]] == ["fine"]
    assert api.get(data["memes"][0]["url"]).status_code == 200
    assert api.get(f"/api/live/memes?since={data['seq']}").json()["memes"] == []


def test_meme_route_rejects_unknown_missing_and_traversal(api):
    assert api.post("/api/live/meme", json={"id": "nope"}).status_code == 404
    assert api.post("/api/live/meme", json={"id": "gone"}).status_code == 404
    assert api.get("/memes/..%2Fmemes.yaml").status_code == 404


def test_card_flow(api):
    assert api.get("/api/live/card").json()["card"] is None
    api.post("/api/live/thinking", json={"id": "q1", "heard": "how do I jump"})
    card = api.get("/api/live/card").json()["card"]
    assert card["status"] == "thinking"

    assert api.post("/api/live/expand").status_code == 409  # nothing to expand yet
    api.post("/api/live/answer", json={
        "id": "q1", "hint": "Hold crouch first.", "steps": ["Crouch", "Jump", " "],
        "source_name": "Combine OverWiki",
    })
    card = api.get("/api/live/card").json()["card"]
    assert card["status"] == "hint"
    assert card["heard"] == "how do I jump"  # kept from the thinking card
    assert card["steps"] == ["Crouch", "Jump"]

    assert api.post("/api/live/expand").json()["status"] == "expanded"
    v = api.get("/api/live/card").json()["version"]
    api.post("/api/live/clear")
    after = api.get("/api/live/card").json()
    assert after["card"] is None and after["version"] > v


def test_live_state_new_question_replaces_card():
    live = LiveState()
    live.set_card("hint", id="q1", hint="old", steps=["a"])
    live.set_card("thinking", id="q2", heard="new one")
    card = live.card()["card"]
    assert card["id"] == "q2" and "hint" not in card


# --- watch --------------------------------------------------------------


def test_watch_emits_questions_and_batches(tmp_path):
    store = LiveStore(tmp_path)
    index = tmp_path / "memes.yaml"
    index.write_text("memes:\n  - id: fine\n    file: fine.png\n", encoding="utf-8")
    store.add_utterance("before the watch started", seq=1)

    clock = Clock()
    lines: list[str] = []

    def tick(_):
        clock.now += 1
        if clock.now == 1002:
            store.add_utterance("this is going great", seq=2)
            store.add_event("question", id="q1", text="how do I jump", game=None)

    watch(store, index, batch_s=5, out=lines.append, sleep=tick, clock=clock, rounds=8)
    assert lines[0].startswith("WATCHING")
    assert "QUESTION q1" in lines
    assert "TRANSCRIPT 1 since=1" in lines
    assert lines.count("QUESTION q1") == 1


def test_watch_quiet_without_memes(tmp_path):
    store = LiveStore(tmp_path)
    clock = Clock()
    lines: list[str] = []

    def tick(_):
        clock.now += 1
        store.add_utterance(f"line {clock.now}")

    watch(store, tmp_path / "no-index.yaml", batch_s=2, out=lines.append, sleep=tick,
          clock=clock, rounds=6)
    assert not any(line.startswith("TRANSCRIPT") for line in lines)


def test_answered_and_cleared_questions_are_not_pending(tmp_path):
    store = LiveStore(tmp_path)
    for qid in ("q1", "q2", "q3"):
        store.add_event("question", id=qid, text="?", game=None)
    store.add_event("answer", id="q1", hint="x")
    store.add_event("clear", id="q2")
    assert [q["id"] for q in store.unanswered()] == ["q3"]


def test_lone_name_is_not_a_wake():
    assert wake.parse("Aria.", PATTERN) is None
    assert wake.parse("Hey Aria.", PATTERN).command == "listen"


def test_real_library_triggers():
    """The meme library, against how Whisper actually writes things."""
    from wabisanji.config import load_config

    memes = load_index(load_config().stream.live.index_path)
    clock = Clock()

    def hit(text):
        clock.now += 10_000  # clear every cooldown
        meme = MemeMatcher(memes, clock=clock).match(text)
        return meme.id if meme else None

    twss = {"thats-what-she-said", "twss-michael", "twss-michael-2"}
    assert hit("Oh, that's what she said.") in twss
    assert hit("Thats what she said") in twss
    assert hit("That did 69 damage.") == "noice"
    assert hit("Sixty-nine bits, thank you!") == "noice"
    assert hit("Noice.") == "noice"
    assert hit("It's 690 gold") is None
    assert hit("What did she say?") is None
    assert hit("Nice one.") is None


# --- library fields, family mode, fetch helpers ---------------------------


def test_meme_kinds_and_risk(tmp_path):
    index = tmp_path / "memes.yaml"
    index.write_text(
        "memes:\n"
        "  - {id: a, file: a.mp4, risk: med}\n"
        "  - {id: b, file: b.mp3, caption: 'Oh no.', family: false}\n"
        "  - {id: c, file: c.png}\n",
        encoding="utf-8",
    )
    memes = load_index(index)
    assert [memes[k].kind for k in "abc"] == ["clip", "sound", "image"]
    assert memes["b"].caption == "Oh no." and memes["b"].family is False
    assert memes["c"].risk == "high"  # unknown provenance counts as risky
    index.write_text("memes:\n  - {id: x, file: x.mp4, risk: huge}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_index(index)


def test_family_mode_blocks_not_family_memes(rig):
    listener, store, client, spoken, clock = rig
    listener.matcher.memes["fine"].family = False
    listener.handle("Hey Aria, family mode.")
    assert store.family_mode() is True
    assert spoken[-1] == "Family mode on."
    listener.handle("this is fine")
    assert not any(path == "/api/live/meme" for path, _ in client.posts)
    listener.handle("Aria, family mode off")
    clock.now += 1000
    listener.handle("this is fine")
    assert ("/api/live/meme", {"id": "fine"}) in client.posts


def test_meme_route_sends_kind_and_caption(tmp_path):
    (tmp_path / "img").mkdir()
    (tmp_path / "img" / "oh.mp3").write_bytes(b"ID3")
    index = tmp_path / "memes.yaml"
    index.write_text("memes:\n  - {id: oh, file: oh.mp3, caption: 'Oh no. Anyway.'}\n", encoding="utf-8")
    cfg = LiveConfig(memes_dir=str(tmp_path / "img"), memes_index=str(index))
    api = TestClient(create_app(tmp_path / "none.db", live_config=cfg))
    event = api.post("/api/live/meme", json={"id": "oh"}).json()
    assert event["kind"] == "sound" and event["caption"] == "Oh no. Anyway."


def test_fetch_helpers(tmp_path):
    from wabisanji.live import fetch

    assert fetch.giphy_clip_url("https://giphy.com/clips/theoffice-nHef54opStn5vDl2QJ") == \
        "https://media.giphy.com/media/nHef54opStn5vDl2QJ/giphy480p.mp4"
    assert fetch.giphy_clip_url("https://www.youtube.com/watch?v=UnyBJJI2eqs") is None
    assert fetch._is_ytdlp("https://youtu.be/UnyBJJI2eqs")
    assert fetch._seconds("1:02.5") == 62.5

    index = tmp_path / "memes.yaml"
    index.write_text("# keep me\nmemes: []\n", encoding="utf-8")
    fetch.append_entry(index, "oh-no", "oh-no.mp3", ["oh no anyway"], "Clarkson.",
                       note="1.9 s", caption="Oh no. Anyway.", category="fail", risk="med",
                       family=None)
    text = index.read_text(encoding="utf-8")
    assert text.startswith("# keep me")
    assert "file: oh-no.mp3  # 1.9 s" in text
    entry = load_index(index)["oh-no"]
    assert (entry.caption, entry.category, entry.risk, entry.family) == \
        ("Oh no. Anyway.", "fail", "med", True)
    with pytest.raises(fetch.FetchError):
        fetch.append_entry(index, "oh-no", "x.mp3", [], "")
    with pytest.raises(fetch.FetchError):
        fetch.fetch("x", tmp_path, "Bad Id")


# --- switching game by voice ---------------------------------------------


@pytest.mark.parametrize("text, game", [
    ("Hey Aria, we're playing Enshrouded.", "Enshrouded"),
    ("Hey Aria, we are playing Breath of Fire 4 now", "Breath of Fire 4"),
    ("Aria, switch game to Satisfactory", "Satisfactory"),
    ("Hey Aria, change the game to Alchemy Factory.", "Alchemy Factory"),
    ("Hey Aria, the game is Half-Life.", "Half-Life"),
    ("Hey Aria, playing Enshrouded", "Enshrouded"),
])
def test_wake_game_switch(text, game):
    assert wake.parse(text, PATTERN) == wake.Wake("game", game)


def test_game_switch_with_a_question_stays_a_question():
    hit = wake.parse("Hey Aria, we're playing Enshrouded, how do I get the glider?", PATTERN)
    assert hit.command == "question"


def test_match_source_snaps_misheard_games():
    from wabisanji.session import match_source

    known = ["Breath of Fire IV", "Enshrouded", "Satisfactory"]
    assert match_source("in shrouded", known) == "Enshrouded"
    assert match_source("Breath of Fire 4", known) == "Breath of Fire IV"
    assert match_source("Stardew Valley", known) is None


def test_set_source_returns_to_last_section(tmp_path, monkeypatch):
    from wabisanji import session

    monkeypatch.setattr(session, "SESSION_PATH", tmp_path / "session.json")
    session.set_section("Sarai", source="Breath of Fire IV")
    state = session.set_source("Enshrouded")
    assert (state["source"], state["section"]) == ("Enshrouded", None)
    state = session.set_source("breath of fire 4")
    assert (state["source"], state["section"]) == ("Breath of Fire IV", "01 Sarai")


def test_listener_switches_game(rig):
    listener, store, _, spoken, _ = rig
    switched = []
    listener.set_game = lambda heard: switched.append(heard) or "Enshrouded"
    listener.handle("Hey Aria, we're playing in shrouded.")
    assert switched == ["in shrouded"]
    assert store.events("game")[0]["game"] == "Enshrouded"
    assert spoken == ["Okay, Enshrouded."]
    assert store.events("question") == []


# --- OBS browser source refresh ------------------------------------------


def test_obs_refresh_fixes_and_reloads_our_sources(monkeypatch):
    import asyncio
    import json
    import threading

    import websockets

    from wabisanji.config import ObsConfig
    from wabisanji.stream import obs

    sources = {"Vocab": "http://localhost:8770/overlay", "MEMES": "localhost:8770/memes-overlay",
               "Chat": "https://example.com/chat", "Rain": None}
    calls: list[tuple[str, dict]] = []
    salt, challenge = "c2FsdA==", "Y2hhbGxlbmdl"

    async def handler(ws):
        await ws.send(json.dumps({"op": 0, "d": {"authentication": {"salt": salt, "challenge": challenge}}}))
        ident = json.loads(await ws.recv())["d"]
        if ident.get("authentication") != obs.auth_string("pw", salt, challenge):
            await ws.close(4009)
            return
        await ws.send(json.dumps({"op": 2, "d": {}}))
        async for raw in ws:
            d = json.loads(raw)["d"]
            kind, data = d["requestType"], d["requestData"]
            calls.append((kind, data))
            resp = {}
            if kind == "GetInputList":
                resp = {"inputs": [{"inputName": n} for n in sources]}
            elif kind == "GetInputSettings":
                url = sources[data["inputName"]]
                resp = {"inputSettings": {"url": url} if url else {"local_file": "x.html"}}
            await ws.send(json.dumps({"op": 7, "d": {"requestId": d["requestId"], "requestType": kind,
                                                     "requestStatus": {"result": True, "code": 100},
                                                     "responseData": resp}}))

    ready, stop = threading.Event(), None
    port_box = {}

    def serve():
        async def main():
            nonlocal stop
            stop = asyncio.Event()
            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port_box["port"] = server.sockets[0].getsockname()[1]
                ready.set()
                await stop.wait()
        loop = asyncio.new_event_loop()
        port_box["loop"] = loop
        loop.run_until_complete(main())

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    ready.wait(5)
    try:
        cfg = ObsConfig(host="127.0.0.1", port=port_box["port"], password_env="TEST_OBS_PW")
        monkeypatch.setenv("TEST_OBS_PW", "pw")
        assert obs.refresh_sources(cfg, 8770) == ["Vocab", "MEMES (added http://)"]
        assert ("SetInputSettings", {"inputName": "MEMES",
                                     "inputSettings": {"url": "http://localhost:8770/memes-overlay"}}) in calls
        assert ("PressInputPropertiesButton", {"inputName": "Vocab", "propertyName": "refreshnocache"}) in calls

        monkeypatch.setenv("TEST_OBS_PW", "wrong")
        with pytest.raises(obs.ObsError, match="password"):
            obs.refresh_sources(cfg, 8770)
    finally:
        port_box["loop"].call_soon_threadsafe(stop.set)
        t.join(5)

    with pytest.raises(obs.ObsError, match="isn't running"):
        obs.refresh_sources(ObsConfig(host="127.0.0.1", port=1), 8770, timeout=0.5)


def test_tied_phrases_pick_at_random():
    """Memes sharing a trigger (the three "that's what she said" clips) take turns at random."""
    import random

    memes = {i: Meme(id=i, file=f"{i}.mp4", phrases=["that's what she said"])
             for i in ("a", "b", "c")}
    memes["trap"] = Meme(id="trap", file="trap.mp4", phrases=["trap"])
    clock = Clock()
    m = MemeMatcher(memes, clock=clock, rng=random.Random(1))
    seen = set()
    for _ in range(30):
        clock.now += 10_000
        seen.add(m.match("that's what she said, it's a trap").id)
    assert seen == {"a", "b", "c"}  # longest phrase still beats "trap"
