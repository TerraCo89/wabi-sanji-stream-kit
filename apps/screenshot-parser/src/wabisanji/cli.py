"""Wabi Sanji CLI.

    wabisanji analyze <image> [--method hybrid] [--source "Game Name"] [--json] [--no-save]
    wabisanji watch [--source "Game Name"]
    wabisanji pending [--limit N]
    wabisanji ingest <results.json> [--source "Game Name"]
    wabisanji section ["Pallet Town"] [--source "Game Name"]
    wabisanji anki-sync
    wabisanji stats
    wabisanji serve [--port 8770] [--source "Game Name"]
    wabisanji wotd [--word 猫 --reading ねこ --meaning cat]
    wabisanji listen [--device "Sonar"] [--file rec.mkv --track 1 --start 600]
    wabisanji live watch|show|transcript|answer|meme|memes|clear|say
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
import time

from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from . import session, wotd
from .analyzer import _clean_vocab, analyze
from .anki import AnkiClient, AnkiConnectError, is_pokemon_game, sync_vocabulary
from .config import load_config
from .db import Database, connect_readonly
from .watcher import _print_summary, pending_screenshots, process_file, watch


def main(argv: list[str] | None = None) -> int:
    # Windows consoles often default to cp1252, which can't print Japanese
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="wabisanji", description=__doc__)
    parser.add_argument("--config", help="Path to config.yaml", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="Analyze a single screenshot")
    p_analyze.add_argument("image", help="Path to the screenshot")
    p_analyze.add_argument("--method", choices=["vision", "manga-ocr", "hybrid"], default=None)
    p_analyze.add_argument("--source", help="Game/source name", default=None)
    p_analyze.add_argument("--json", action="store_true", help="Print raw JSON result")
    p_analyze.add_argument("--no-save", action="store_true", help="Don't write to the capture DB")

    p_watch = sub.add_parser("watch", help="Watch the screenshot folder")
    p_watch.add_argument("--source", help="Game/source name for this session", default=None)

    p_pending = sub.add_parser(
        "pending", help="List screenshots not yet in the capture log (oldest first)"
    )
    p_pending.add_argument("--limit", type=int, default=None, help="Only the N most recent")

    p_ingest = sub.add_parser(
        "ingest", help="Save analysis JSON produced outside the parser (e.g. by Claude Code)"
    )
    p_ingest.add_argument("json_file", help="JSON file: one result object or a list of them")
    p_ingest.add_argument("--source", help="Game/source name (default: current session)", default=None)
    p_ingest.add_argument("--method", default="claude", help="ocr_method to record")

    p_section = sub.add_parser(
        "section", help="Show or set the current section (town/route); new ones get the next number"
    )
    p_section.add_argument("name", nargs="?", help='e.g. "Pallet Town"; omit to show current')
    p_section.add_argument("--source", help="Game/source name (needed the first time)", default=None)

    sub.add_parser("anki-sync", help="Push unsynced vocabulary to Anki")
    sub.add_parser("stats", help="Show capture log statistics")

    p_wotd = sub.add_parser(
        "wotd", help="Pick the Japanese Word of the Day and write data/wotd.txt (for Streamer.bot)"
    )
    p_wotd.add_argument("--word", help="Use this word instead of picking one", default=None)
    p_wotd.add_argument("--reading", default=None)
    p_wotd.add_argument("--meaning", default=None)

    p_serve = sub.add_parser(
        "serve", help="Serve the OBS vocab overlay and JSON API (needs the [stream] extra)"
    )
    p_serve.add_argument("--port", type=int, default=None, help="Default: stream.port (8770)")
    p_serve.add_argument(
        "--source", help="Game/source name (default: current session)", default=None
    )

    p_listen = sub.add_parser(
        "listen", help="ARIA: listen to the mic on the CPU (needs the [listen] extra)"
    )
    p_listen.add_argument("--device", default=None, help="Mic name substring or index")
    p_listen.add_argument("--list-devices", action="store_true", help="List mics and exit")
    p_listen.add_argument("--model", default=None, help="faster-whisper model (default: base.en)")
    p_listen.add_argument("--file", default=None, help="Run over a recording instead of the mic")
    p_listen.add_argument("--track", type=int, default=0, help="Audio track of --file (0 = first)")
    p_listen.add_argument("--start", type=float, default=0.0, help="Seconds into --file")
    p_listen.add_argument("--duration", type=float, default=None, help="Seconds of --file")
    p_listen.add_argument(
        "--post", action="store_true",
        help="With --file: really log, post to the overlay and speak (default is a dry run)",
    )
    p_listen.add_argument(
        "--dry-run", action="store_true", help="Print what would happen; write and post nothing"
    )

    p_live = sub.add_parser("live", help="ARIA: what Claude uses during /aria watch")
    live_sub = p_live.add_subparsers(dest="live_command", required=True)
    live_sub.add_parser("watch", help="Event stream for Claude Code's Monitor")
    p_show = live_sub.add_parser("show", help="A question, the game and what was said before it")
    p_show.add_argument("id")
    p_tr = live_sub.add_parser("transcript", help="Recent speech")
    p_tr.add_argument("--since", type=int, default=None, help="Transcript seq to start after")
    p_tr.add_argument("--last", type=int, default=20, help="Without --since: last N lines")
    p_ans = live_sub.add_parser("answer", help="Show an answer on the ARIA card and speak the hint")
    p_ans.add_argument("json_file", help="{id, hint, steps, source_name, source_url}")
    p_ans.add_argument("--no-speak", action="store_true")
    p_meme = live_sub.add_parser("meme", help="Pop a meme from the library")
    p_meme.add_argument("id")
    p_meme.add_argument("--via", default="manual", choices=["manual", "claude"])
    p_meme.add_argument("--force", action="store_true", help="Ignore memes-off and the gap rule")
    live_sub.add_parser("memes", help="List the meme library")
    p_fetch = live_sub.add_parser(
        "fetch", help="Add a meme: GIPHY clip, YouTube section, URL or file -> levelled clip"
    )
    p_fetch.add_argument(
        "source",
        help="ytsearch:<query>, myinstants:<query>, YouTube URL, myinstants page, URL or file")
    p_fetch.add_argument("--id", required=True, help="Library id, e.g. emotional-damage")
    p_fetch.add_argument("--from", dest="start", default=None, help="Start, e.g. 0:01.5")
    p_fetch.add_argument("--to", dest="end", default=None, help="End, e.g. 0:04")
    p_fetch.add_argument("--say", action="append", default=[],
                         help="Phrase that fires it instantly (repeatable)")
    p_fetch.add_argument("--about", default="", help="What it is and when it fits (for Claude)")
    p_fetch.add_argument("--note", default="", help="Comment on the file line, e.g. the source")
    p_fetch.add_argument("--cooldown", type=int, default=None, help="Seconds before it repeats")
    p_fetch.add_argument("--expect", default=None,
                         help="The line said in it: Whisper finds it and trims around it")
    p_fetch.add_argument("--audio-only", action="store_true", help="Keep only the sound (.mp3)")
    p_fetch.add_argument("--caption", default="", help="Text shown with a sound bite")
    p_fetch.add_argument("--category", default=None, help="fail, shock, win, numbers, ...")
    p_fetch.add_argument("--risk", choices=["low", "med", "high"], default="high",
                         help="Content ID risk of the audio (default high)")
    p_fetch.add_argument("--not-family", action="store_true",
                         help="Innuendo etc.: skipped in family mode and kids' segments")
    p_fetch.add_argument("--no-index", action="store_true", help="Only fetch the file")
    live_sub.add_parser("clear", help="Hide the ARIA card")
    p_say = live_sub.add_parser("say", help="Speak text in ARIA's voice (test Speaker.bot)")
    p_say.add_argument("text")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)

    if args.command == "analyze":
        if args.no_save:
            result = analyze(
                args.image,
                method=args.method or config.ocr_method,
                ollama=config.ollama,
            )
        else:
            if args.method:
                config.ocr_method = args.method
            with Database(config.db_path) as db:
                result = process_file(args.image, config, db, source=args.source)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _print_summary(result)
        return 1 if result.get("error") else 0

    if args.command == "watch":
        watch(config, source=args.source)
        return 0

    if args.command == "pending":
        with Database(config.db_path) as db:
            paths = pending_screenshots(config, db)
        if args.limit:
            paths = paths[-args.limit:]
        for p in paths:
            print(p)
        return 0

    if args.command == "ingest":
        with open(args.json_file, encoding="utf-8") as f:
            data = json.load(f)
        results = data if isinstance(data, list) else [data]
        state = session.load()
        source = args.source or state.get("source")
        with Database(config.db_path) as db:
            for result in results:
                result["vocabulary"] = _clean_vocab(
                    result.get("vocabulary", []), pokemon=is_pokemon_game(source)
                )
                result["ocr_method"] = args.method
                result["section"] = result.get("section") or session.section_for(
                    result.get("screen"), state
                )
                db.save_analysis(result, source_name=source)
                print(
                    f"saved {Path(result.get('file_path', '?')).name} -> "
                    f"{result['section'] or 'no section'}: "
                    f"{len(result['vocabulary'])} vocab word(s)"
                )
            if config.anki.auto_sync:
                try:
                    synced, failed = sync_vocabulary(db, AnkiClient(config.anki))
                    print(f"Anki sync: {synced} pushed, {failed} failed")
                except AnkiConnectError as e:
                    print(f"Anki sync skipped: {e} (run `wabisanji anki-sync` later)")
        return 0

    if args.command == "section":
        if args.name:
            try:
                state = session.set_section(args.name, source=args.source)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
        else:
            state = session.load()
        print(f"game:    {state.get('source') or '(not set)'}")
        print(f"section: {state.get('section') or '(not set)'}")
        return 0

    if args.command == "anki-sync":
        with Database(config.db_path) as db:
            try:
                synced, failed = sync_vocabulary(db, AnkiClient(config.anki))
            except AnkiConnectError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
        print(f"Synced {synced} note(s) to Anki ({failed} failed).")
        return 1 if failed else 0

    if args.command == "stats":
        with Database(config.db_path) as db:
            for key, value in db.stats().items():
                print(f"{key:>16}: {value}")
        return 0

    if args.command == "serve":
        return _serve(config, port=args.port or config.stream.port, source=args.source)

    if args.command == "wotd":
        return _wotd(config, args.word, args.reading, args.meaning)

    if args.command == "listen":
        return _listen(config, args)

    if args.command == "live":
        return _live(config, args)

    return 0


def _wotd(config, word: str | None, reading: str | None, meaning: str | None) -> int:
    today = date.today()
    if word:
        picked = {"word": word, "reading": reading or word, "meaning": meaning or ""}
    else:
        try:
            conn = connect_readonly(config.db_path)
        except sqlite3.OperationalError as e:
            print(f"error: can't open {config.db_path}: {e}", file=sys.stderr)
            return 1
        with closing(conn):
            picked = wotd.pick(conn, wotd.load_history(), today)
        if not picked:
            print("error: no words with a reading and meaning yet; pass --word", file=sys.stderr)
            return 1
    wotd.save(picked["word"], picked["reading"], picked["meaning"], today)
    print(f"{picked['word']} ({picked['reading']}): {picked['meaning']}")
    return 0


def _serve(config, port: int, source: str | None) -> int:
    try:
        import uvicorn

        from .stream.server import create_app
        from .stream.state import StreamState
    except ImportError as e:
        print(
            f"error: {e.name} is missing. Install the stream extra:\n"
            "  uv sync --extra stream   (add --extra ocr if you use manga-ocr)",
            file=sys.stderr,
        )
        return 1

    if source:
        game = source
    else:
        game = session.load().get("source")
        game = f"{game} (from session)" if game else "(not set: showing all sources)"
    base = f"http://localhost:{port}"
    print(f"overlay: {base}/overlay")
    print(f"health:  {base}/health")
    print(f"game:    {game}")
    print(f"memes:   {base}/memes-overlay")
    print(f"ARIA:    {base}/aria")
    app = create_app(config.db_path, StreamState(source=source), live_config=config.stream.live)
    threading.Thread(target=_refresh_obs, args=(config.stream.obs, port), daemon=True).start()
    # Localhost only: OBS and the browser run on this machine
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


def _series_games(memes_index: Path) -> list[str]:
    """Game names from content/stream/series.yaml (next to memes.yaml), for snapping
    a misheard "we're playing ..." to a real game."""
    import yaml

    try:
        data = yaml.safe_load((memes_index.parent / "series.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    games = [s.get("game") for s in (data or {}).get("series") or []]
    return [g.removesuffix(" (JP)") for g in games if g]


def _refresh_obs(obs_config, port: int) -> None:
    """Once the server answers, reload OBS's browser sources that point at it.

    After a reboot OBS usually opens first and its sources are stuck on an error page.
    """
    import urllib.request

    from .stream.obs import ObsError, refresh_sources

    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).close()
            break
        except OSError:
            time.sleep(0.25)
    try:
        names = refresh_sources(obs_config, port)
    except ObsError as e:
        print(f"obs:     {e} (sources load when OBS starts)")
    except ImportError:
        print("obs:     websockets is missing (uv sync --extra stream); refresh sources by hand")
    else:
        print(f"obs:     refreshed {', '.join(names)}" if names else
              f"obs:     no browser sources point at :{port}")


def _listen(config, args) -> int:
    from .live.memes import MemeMatcher, load_index
    from .live.store import LiveStore

    try:
        from .live import listen
        from .live.client import LiveClient
        from .live.speaker import speak
    except ImportError as e:
        print(f"error: {e.name} is missing. Install the listen extra:\n"
              "  uv sync --extra ocr --extra stream --extra listen", file=sys.stderr)
        return 1

    live = config.stream.live
    if args.list_devices:
        print(listen.list_devices())
        return 0

    dry_run = args.dry_run or (args.file is not None and not args.post)
    store = LiveStore()
    memes = load_index(live.index_path)
    listener = listen.Listener(
        store=store,
        client=LiveClient(f"http://127.0.0.1:{config.stream.port}"),
        matcher=MemeMatcher(memes, gap_s=live.meme_gap_s),
        wake_words=live.wake_words,
        speak=lambda text: speak(text, live.speaker_bot, live.voice, store),
        game=lambda: session.load().get("source"),
        set_game=lambda heard: session.set_source(heard, _series_games(live.index_path))["source"],
        dry_run=dry_run,
    )

    model = args.model or live.model
    print(f"loading {model} on the CPU...", flush=True)
    transcriber = listen.Transcriber(model, cpu_threads=live.cpu_threads)

    def hotwords() -> str:
        # The game name helps spell game words. Not "Aria": as a hotword Whisper
        # starts hearing the name in every mumble.
        return session.load().get("source") or ""

    if not args.verbose:
        for noisy in ("faster_whisper", "httpx", "huggingface_hub"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    print(f"memes:   {len(memes)} in {live.index_path}")
    if dry_run:
        print("dry run: nothing is logged, posted or spoken")
    try:
        if args.file:
            print(f"file:    {args.file} (track {args.track}, from {args.start:.0f}s)\n", flush=True)
            chunks = listen.file_chunks(args.file, args.track, args.start, args.duration)
            listen.run(listener, transcriber, chunks, live=False, hotwords=hotwords,
                       offset_s=args.start)
        else:
            import sounddevice as sd

            device = listen.find_device(args.device if args.device is not None else live.mic)
            print(f"mic:     {sd.query_devices(device, 'input')['name']}")
            print('Listening. Say "Hey ARIA, ..." (Ctrl+C to stop)\n', flush=True)
            listen.run(listener, transcriber, listen.mic_chunks(device, threading.Event()),
                       live=True, hotwords=hotwords, is_speaking=store.is_speaking)
    except KeyboardInterrupt:
        print("stopped")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _live(config, args) -> int:
    from .live.client import LiveClient
    from .live.memes import load_index
    from .live.store import LiveStore

    live = config.stream.live
    store = LiveStore()
    client = LiveClient(f"http://127.0.0.1:{config.stream.port}")
    cmd = args.live_command

    if cmd == "watch":
        from .live.watch import watch

        try:
            watch(store, live.index_path, batch_s=live.transcript_batch_s)
        except KeyboardInterrupt:
            pass
        return 0

    if cmd == "show":
        q = store.question(args.id)
        if not q:
            print(f"error: no question {args.id}", file=sys.stderr)
            return 1
        print(f"question: {q['text']}")
        print(f"game:     {q.get('game') or '(not set)'}  "
              f"section: {session.load().get('section') or '-'}")
        print(f"asked:    {q['t']}")
        if any(e.get("id") == args.id for e in store.events("clear")):
            print("CANCELLED: the streamer said stop or never mind; don't search")
        asked =datetime.fromisoformat(q["t"])
        before = [r for r in store.transcript()
                  if 0 <= (asked - datetime.fromisoformat(r["t"])).total_seconds() <= 90]
        print("said just before:")
        for r in before[-12:]:
            print(f"  {r['t'][11:19]}  {r['text']}")
        return 0

    if cmd == "transcript":
        rows = store.transcript(since=args.since or 0)
        if args.since is None:
            rows = rows[-args.last:]
        print(f"game: {session.load().get('source') or '(not set)'}")
        for r in rows:
            print(f"{r['seq']:>5}  {r['t'][11:19]}  {r['text']}")
        return 0

    if cmd == "answer":
        from .live.speaker import SpeakerError, speak

        with open(args.json_file, encoding="utf-8") as f:
            ans = json.load(f)
        if not ans.get("id") or not ans.get("hint"):
            print("error: answer needs at least id and hint", file=sys.stderr)
            return 1
        if any(e.get("id") == ans["id"] for e in store.events("clear")):
            print(f"skipped: the streamer dismissed {ans['id']} before the answer was ready")
            store.add_event("answer", **ans, shown=False)
            return 0
        q = store.question(ans["id"])
        body = {**ans, "heard": q["text"]} if q else ans
        if client.post("/api/live/answer", body) is None:
            print("error: couldn't reach the overlay server; is `wabisanji serve` running?",
                  file=sys.stderr)
            return 1
        store.add_event("answer", **ans, shown=True)
        print(f"shown: {ans['hint']}")
        if not args.no_speak:
            try:
                speak(ans["hint"], live.speaker_bot, live.voice, store)
                print("spoken")
            except SpeakerError as e:
                print(f"not spoken: {e}")
        return 0

    if cmd == "meme":
        memes = load_index(live.index_path)
        if args.id not in memes:
            print(f"error: unknown meme {args.id!r}. Known: {', '.join(memes) or 'none'}",
                  file=sys.stderr)
            return 1
        if not args.force:
            if not store.memes_enabled():
                print("skipped: memes are off (the streamer turned them off)")
                return 0
            if store.family_mode() and not memes[args.id].family:
                print(f"skipped: family mode is on and {args.id} isn't family-friendly")
                return 0
            ago = store.seconds_since_meme()
            gap = live.claude_meme_gap_s if args.via == "claude" else live.meme_gap_s
            if ago is not None and ago < gap:
                print(f"skipped: a meme popped {ago:.0f}s ago (gap is {gap}s)")
                return 0
        if client.post("/api/live/meme", {"id": args.id, "via": args.via}) is None:
            print("error: the overlay server refused or isn't running (see log above)",
                  file=sys.stderr)
            return 1
        store.add_event("meme", id=args.id, via=args.via)
        print(f"meme: {args.id}")
        return 0

    if cmd == "memes":
        memes = load_index(live.index_path)
        folder = live.memes_path
        print(f"index:  {live.index_path}")
        if os.environ.get("WABISANJI_MEMES") or live.memes_dir:
            print(f"images: {folder}")
        else:
            print("images: (not set: stream.live.memes_dir in config.yaml, or WABISANJI_MEMES)")
        print(f"memes:  {'on' if store.memes_enabled() else 'off'}"
              f"{', family mode' if store.family_mode() else ''}\n")
        for m in memes.values():
            missing = "" if (folder / m.file).is_file() else "  [MISSING FILE]"
            tags = ", ".join(x for x in (m.category, m.kind, f"risk {m.risk}",
                                         "" if m.family else "not family") if x)
            print(f"{m.id} ({tags}): {m.about}{missing}")
            if m.phrases:
                print(f"    says: {' | '.join(m.phrases)}")
        return 0

    if cmd == "fetch":
        from .live import fetch

        try:
            if not args.no_index:
                fetch.check_new(live.index_path, args.id)  # before overwriting any file
            path, used = fetch.fetch(args.source, live.memes_path, args.id, args.start,
                                     args.end, audio_only=args.audio_only, expect=args.expect)
            if used != args.source:
                args.note = ", ".join(x for x in (args.note, used) if x)
            info = fetch.probe(path) if path.suffix in (".mp4", ".mp3") else {}
            length = f"{info['duration']:.1f} s" if info.get("duration") else "image"
            sound = ", with sound" if info.get("audio") else ""
            print(f"saved {path} ({length}{sound})")
            if not args.no_index:
                note = ", ".join(x for x in (args.note, length) if x)
                fetch.append_entry(live.index_path, args.id, path.name, args.say, args.about,
                                   note=note, cooldown_s=args.cooldown, caption=args.caption,
                                   category=args.category, risk=args.risk,
                                   family=False if args.not_family else None)
                print(f"added {args.id} to {live.index_path.name}")
        except fetch.FetchError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0

    if cmd == "clear":
        card = client.card()
        if client.post("/api/live/clear") is None:
            return 1
        store.add_event("clear", id=card["id"] if card else None)
        print("cleared")
        return 0

    if cmd == "say":
        from .live.speaker import SpeakerError, speak

        try:
            speak(args.text, live.speaker_bot, live.voice, store)
        except SpeakerError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print("spoken")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
