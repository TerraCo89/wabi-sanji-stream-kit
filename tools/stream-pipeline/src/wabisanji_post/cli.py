"""``stream-*`` commands, registered on the main Typer app by :func:`register`."""
from __future__ import annotations

import datetime as dt
import difflib
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import media, sessions, settings
from .models import Session, fmt_short, fmt_tc, parse_tc


console = Console()


def _session_dir(ref: str) -> Path:
    try:
        return sessions.resolve_session_dir(ref)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)


def _load(session_dir: Path) -> Session:
    try:
        return sessions.load_session(session_dir)
    except Exception as exc:  # pydantic / yaml errors: show them, don't trace
        console.print(f"[red]{session_dir / sessions.SESSION_YAML} is invalid:[/red]\n{exc}")
        raise typer.Exit(code=2)


def _recording(session: Session) -> Path:
    rec = Path(session.recording)
    if not rec.exists():
        console.print(f"[red]Recording not found: {rec}[/red]")
        raise typer.Exit(code=2)
    return rec


def _tc(value: str) -> float:
    try:
        return parse_tc(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc))


# ---------------------------------------------------------------------------
# stream-init
# ---------------------------------------------------------------------------

def stream_init(
    recording: Path = typer.Argument(..., help='Raw OBS recording, e.g. "2026-10-05 10-37-11.mkv" in the recordings folder.'),
    start: Optional[str] = typer.Option(
        None, "--start",
        help="Recording start as ISO time with offset (2026-09-11T19:00:00+10:00). "
             "Needed when the filename isn't an OBS timestamp.",
    ),
    session_id: Optional[str] = typer.Option(None, "--session-id", help="Override the folder name (default: the date, then _2, _3 …)."),
    audio_track: Optional[int] = typer.Option(None, "--audio-track", help="ffmpeg a:<n> for transcripts, Shorts and episodes. Default: 1 (Track 2, everything but Spotify) when the file has several tracks, else 0."),
    vod_id: Optional[str] = typer.Option(None, "--vod-id", help="Twitch VOD of this stream (from _backlog.yaml); also marks it streamed."),
) -> None:
    """Create a session folder and a draft session.yaml for a recording.

    Probes duration and audio tracks and reads recording_start from the OBS
    filename. If a session already points at this recording, says so and stops.
    """
    recording = recording.resolve()
    if not recording.exists():
        console.print(f"[red]Recording not found: {recording}[/red]")
        raise typer.Exit(code=2)
    root = sessions.sessions_root()
    existing = sessions.find_session_for_recording(recording, root)
    if existing:
        console.print(f"[yellow]Already initialised:[/yellow] {existing / sessions.SESSION_YAML}")
        return

    if start:
        rec_start = dt.datetime.fromisoformat(start)
        if rec_start.tzinfo is None:
            raise typer.BadParameter("--start needs a UTC offset, e.g. +11:00")
    else:
        try:
            rec_start = sessions.parse_obs_filename(recording)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red] Pass --start.")
            raise typer.Exit(code=2)

    duration = media.probe_duration(recording)
    tracks = media.probe_audio_tracks(recording)
    if audio_track is None:
        audio_track = 1 if len(tracks) > 1 else 0
    if audio_track >= len(tracks):
        console.print(f"[red]--audio-track {audio_track}, but the file has {len(tracks)} audio track(s).[/red]")
        raise typer.Exit(code=2)

    sid = session_id or sessions.next_session_id(rec_start.date(), root)
    session = Session(
        session=sid,
        status="draft",
        recording=str(recording),
        recording_start=rec_start,
        duration=float(round(duration)),  # whole seconds, as in the approved 2026-10-05 file
        audio_track=audio_track,
        streamed=True if vod_id else None,
        twitch_vod_id=vod_id,
    )
    session_dir = root / sid
    path = sessions.write_new_session(
        session_dir, session,
        header=f"# DRAFT from stream-init {dt.date.today().isoformat()}. "
               f"Segment with /stream-segment, then {settings.streamer_name()} approves (Gate 1).",
    )

    table = Table(title=f"Audio tracks in {recording.name}")
    for col in ("a:", "title", "codec", "ch", "Hz"):
        table.add_column(col)
    for t in tracks:
        mark = " ←" if t.index == audio_track else ""
        table.add_row(f"{t.index}{mark}", t.title, t.codec, str(t.channels), str(t.sample_rate))
    console.print(table)
    console.print(f"[green]✔[/green] {path}")
    console.print(f"  start {rec_start.isoformat()}   duration {fmt_tc(session.duration)}   audio_track {audio_track}")
    console.print(f"  next: stream-frames {sid} && stream-sheets {sid}")


# ---------------------------------------------------------------------------
# stream-frames
# ---------------------------------------------------------------------------

def stream_frames(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id (2026-10-05) or folder."),
    fine: Optional[str] = typer.Option(
        None, "--fine",
        help="Comma-separated ranges a-b (HH:MM:SS or seconds), one frame every --step s, into fine/. "
             "E.g. 0:07:00-0:08:30,0:51:00-0:53:00",
    ),
    at: Optional[list[str]] = typer.Option(None, "--at", help="Single still at t (repeatable), 960 px wide, into at/."),
    step: float = typer.Option(media.FINE_STEP, "--step", help="Seconds between --fine frames."),
    force: bool = typer.Option(False, "--force", help="Re-extract frames that already exist."),
    workers: int = typer.Option(6, "--workers", help="Parallel ffmpeg processes."),
) -> None:
    """Sample frames from the recording.

    No options: one frame per minute (t = 30, 90, 150 …) into frames/.
    --fine: frames every 10 s across boundary ranges. --at: single stills.
    """
    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    rec = _recording(session)

    jobs: list[tuple[str, list[float], Path, int]] = []
    if fine:
        times: list[float] = []
        for part in fine.split(","):
            if "-" not in part:
                raise typer.BadParameter(f"--fine wants a-b ranges, got {part!r}")
            a, b = part.split("-", 1)
            times += media.fine_times(_tc(a), _tc(b), step)
        jobs.append(("fine", sorted(set(times)), session_dir / "fine", media.SHEET_FRAME_WIDTH))
    if at:
        jobs.append(("at", [_tc(t) for t in at], session_dir / "at", media.AT_FRAME_WIDTH))
    if not jobs:
        jobs.append(("per-minute", media.per_minute_times(session.duration), session_dir / "frames", media.SHEET_FRAME_WIDTH))

    for kind, times, out_dir, width in jobs:
        bad = [t for t in times if t >= session.duration]
        if bad:
            console.print(f"[red]{kind}: {', '.join(fmt_short(t) for t in bad)} is past the end ({fmt_tc(session.duration)}).[/red]")
            raise typer.Exit(code=2)
        t0 = time.monotonic()
        paths = media.extract_frames(rec, times, out_dir, width=width, force=force, workers=workers)
        console.print(f"[green]✔[/green] {kind}: {len(paths)} frame(s) in {out_dir} ({time.monotonic() - t0:.0f}s)")
        if kind == "at":
            for p in paths:
                console.print(f"  {p}")
    if not at:
        console.print(f"  next: stream-sheets {session.session}")


# ---------------------------------------------------------------------------
# stream-sheets
# ---------------------------------------------------------------------------

def stream_sheets(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id (2026-10-05) or folder."),
) -> None:
    """Rebuild contact sheets from frames/ and fine/ (20 tiles, 5 columns, H:MM:SS labels).

    Labels also show the session.yaml segment each frame falls in, so the
    sheets double as a check of the cut points.
    """
    from . import sheets

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    made = sheets.build_all(session_dir, session)
    if not made:
        console.print(f"[yellow]No frames yet. Run stream-frames {session.session} first.[/yellow]")
        raise typer.Exit(code=1)
    for path, a, b in made:
        console.print(f"  {path.name:<22} {fmt_short(a)} - {fmt_short(b)}")
    console.print(f"[green]✔[/green] {len(made)} sheet(s) in {session_dir / 'sheets'}")


# ---------------------------------------------------------------------------
# stream-validate
# ---------------------------------------------------------------------------

def stream_validate(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id (2026-10-05) or folder."),
) -> None:
    """Validate session.yaml (and clips.yaml if present) and print the segment table."""
    session_dir = _session_dir(session_ref)
    session = _load(session_dir)

    table = Table(title=f"{session.session}  ({session.status})  {Path(session.recording).name}")
    for col in ("#", "type", "start", "end", "length", "game / reason", "chapters"):
        table.add_column(col)
    for s in session.segments:
        what = s.game or s.label or ""
        if s.type == "skip":
            what = f"[dim]{s.reason}[/dim]"
        table.add_row(str(s.id), s.type, fmt_tc(s.start), fmt_tc(s.end), fmt_short(s.duration), what, str(len(s.chapters)))
    console.print(table)
    for w in session.warnings():
        console.print(f"[yellow]warning:[/yellow] {w}")
    console.print(f"[green]✔[/green] session.yaml valid")

    if (session_dir / sessions.CLIPS_YAML).exists():
        try:
            clips = sessions.load_clips(session_dir)
        except Exception as exc:
            console.print(f"[red]clips.yaml is invalid:[/red]\n{exc}")
            raise typer.Exit(code=2)
        problems = clips.problems(session)
        for p in problems:
            console.print(f"[red]clips.yaml:[/red] {p}")
        for w in clips.warnings():
            console.print(f"[yellow]warning:[/yellow] {w}")
        if problems:
            raise typer.Exit(code=2)
        n_ok = sum(c.approved for c in clips.clips)
        console.print(f"[green]✔[/green] clips.yaml valid ({len(clips.clips)} clips, {n_ok} approved, status {clips.status})")


# ---------------------------------------------------------------------------
# stream-transcribe
# ---------------------------------------------------------------------------

def stream_transcribe(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id (2026-10-05) or folder."),
    segment: Optional[list[int]] = typer.Option(None, "--segment", "-s", help="Segment id (repeatable). Default: every non-skip segment."),
    model: str = typer.Option("large-v3-turbo", "--model", help="faster-whisper model (chosen 2026-10-05, see the spec)."),
    language: str = typer.Option("en", "--language", help='Whisper language, or "auto". "en" for Japanese segments too: Japanese read aloud comes out as romaji.'),
    prompt: Optional[str] = typer.Option(None, "--prompt", help='Initial prompt. Default: game + series names. "" for none.'),
    force: bool = typer.Option(False, "--force", help="Redo segments that already have a transcript."),
    keep_audio: bool = typer.Option(False, "--keep-audio", help="Keep audio/seg<N>.wav (16 kHz mono) after transcribing."),
) -> None:
    """Transcribe segments on the GPU: transcript/seg<N>.words.json + seg<N>.txt.

    Times in the output are recording offsets. Needs session status
    segments_approved or later. Sets status transcribed once every non-skip
    segment has a transcript. Don't run while OBS is live.
    """
    from . import transcribe as tr

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    _recording(session)
    if not session.at_least("segments_approved"):
        console.print(f"[red]{session.session} is {session.status}: approve the segments (Gate 1) first.[/red]")
        raise typer.Exit(code=2)

    targets = [s for s in session.segments if s.type != "skip"]
    if segment:
        try:
            targets = [session.segment(i) for i in segment]
        except KeyError as exc:
            console.print(f"[red]{exc.args[0]}[/red]")
            raise typer.Exit(code=2)
        skips = [s.id for s in targets if s.type == "skip"]
        if skips:
            console.print(f"[red]Segment(s) {skips} are skip segments.[/red]")
            raise typer.Exit(code=2)
    todo = [s for s in targets if force or not tr.is_done(session_dir, s.id)]
    for s in targets:
        if s not in todo:
            console.print(f"  segment {s.id}: already transcribed (--force to redo)")

    if todo:
        opts = tr.TranscribeOptions(
            model=model,
            language=None if language == "auto" else language,
            initial_prompt=prompt,
        )
        t0 = time.monotonic()
        wm = tr.load_whisper(model)
        console.print(f"  model {model} loaded in {time.monotonic() - t0:.0f}s")
        for s in todo:
            label = f"segment {s.id} ({s.type}, {s.game or s.label}, {fmt_short(s.duration)})"
            with console.status(f"{label}: cutting audio …") as status:
                def progress(t: float, s=s, status=status) -> None:
                    status.update(f"{label}: {fmt_short(t)} / {fmt_short(s.duration)}")
                r = tr.transcribe_segment(wm, session, s, session_dir, opts, keep_audio=keep_audio, progress=progress)
            console.print(
                f"[green]✔[/green] segment {s.id}: {r.words} words, {fmt_short(r.audio_seconds)} audio in "
                f"{r.elapsed_seconds:.0f}s ({r.speed:.0f}x realtime)  → {tr.paths(session_dir, s.id)['txt']}"
            )

    all_done = all(tr.is_done(session_dir, s.id) for s in session.segments if s.type != "skip")
    if all_done and session.status == "segments_approved":
        sessions.set_status(session_dir, "transcribed")
        console.print(f"[green]✔[/green] {session.session}: status segments_approved → transcribed")
        console.print(f"  next: stream-signals {session.session} (Phase 3)")


# ---------------------------------------------------------------------------
# stream-signals
# ---------------------------------------------------------------------------

def stream_signals(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id (2026-10-05) or folder."),
    db: Optional[Path] = typer.Option(None, "--db", help="Parser capture log (read-only). Default: apps/screenshot-parser/data/wabisanji.db."),
    above: float = typer.Option(6.0, "--above", help="Loudness peaks: LU above the segment median."),
    no_loudness: bool = typer.Option(False, "--no-loudness", help="Skip the ebur128 pass."),
) -> None:
    """Write signals/vocab.json, loudness.json, markers.json and chat.json for /stream-moments.

    Vocab captures come from the parser DB (opened read-only), mapped to
    recording offsets from the screenshot filenames, with the spots where
    the word was heard in the transcript. Needs transcripts (stream-transcribe).
    Chat comes from the chat recorder's archive (RECORDER_URL and RECORDER_TOKEN in .env).
    """
    import json

    from . import signals
    from . import transcribe as tr

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    rec = _recording(session)
    segs = [s for s in session.segments if s.type != "skip"]
    missing = [s.id for s in segs if not tr.is_done(session_dir, s.id)]
    if missing:
        console.print(f"[red]No transcript for segment(s) {missing}. Run stream-transcribe {session.session} first.[/red]")
        raise typer.Exit(code=2)
    transcripts = {
        s.id: json.loads(tr.paths(session_dir, s.id)["words"].read_text(encoding="utf-8")) for s in segs
    }
    out_dir = session_dir / "signals"
    out_dir.mkdir(exist_ok=True)

    def write(name: str, data: dict) -> Path:
        path = out_dir / name
        path.write_text(json.dumps({"session": session.session, **data}, ensure_ascii=False, indent=1), encoding="utf-8")
        return path

    vocab = signals.vocab_signals(session, transcripts, db or signals.DEFAULT_DB)
    p = write("vocab.json", vocab)
    if vocab["status"] == "ok":
        n_words = sum(len(c["vocab"]) for c in vocab["captures"])
        n_heard = sum(1 for c in vocab["captures"] for v in c["vocab"] if v["heard"])
        console.print(f"[green]✔[/green] vocab: {vocab['count']} captures, {n_words} words, {n_heard} heard in the transcript → {p}")
    else:
        console.print(f"[yellow]vocab skipped:[/yellow] {vocab['reason']}")

    if not no_loudness:
        t0 = time.monotonic()
        loud = signals.loudness_signals(session, rec, segs, above=above)
        p = write("loudness.json", loud)
        per = ", ".join(f"seg{s['segment']} {len(s['peaks'])} (median {s['median_lufs']} LUFS)" for s in loud["segments"])
        console.print(f"[green]✔[/green] loudness peaks: {per} in {time.monotonic() - t0:.0f}s → {p}")

    markers = signals.marker_signals(session)
    p = write("markers.json", markers)
    console.print(f"[yellow]markers skipped:[/yellow] {markers['reason']}" if markers["status"] != "ok" else f"[green]✔[/green] markers → {p}")
    from . import recorder

    end = session.recording_start + dt.timedelta(seconds=session.duration)
    try:
        chat = signals.chat_signals(session, recorder.chat_lines(session.recording_start, end))
    except recorder.RecorderError as exc:
        chat = {"status": "skipped", "reason": str(exc), "messages": []}
    p = write("chat.json", chat)
    if chat["status"] == "ok":
        per = ", ".join(f"seg{x['segment']} {x['messages']} from {x['chatters']}" for x in chat["per_segment"])
        console.print(f"[green]✔[/green] chat: {chat['count']} messages ({per}), {len(chat['bursts'])} bursts, "
                      f"{len(chat['events'])} subs/raids → {p}")
    else:
        console.print(f"[yellow]chat skipped:[/yellow] {chat['reason']}")
    console.print(f"  next: /stream-moments {session.session}")


# ---------------------------------------------------------------------------
# stream-render-shorts
# ---------------------------------------------------------------------------

def stream_render_shorts(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id (2026-10-05) or folder."),
    clip: Optional[list[str]] = typer.Option(None, "--clip", "-c", help="Clip id (repeatable). Default: every approved clip targeting short."),
    still: bool = typer.Option(False, "--still", help="Only a preview frame per clip (40 %% in), no video. For layout checks."),
    force: bool = typer.Option(False, "--force", help="Re-render clips that already have shorts/<id>.mp4."),
) -> None:
    """Render approved clips as 1080x1920 Shorts: shorts/<id>.mp4 + <id>.ass.

    Game over facecam per layouts.yaml, Wabi Sanji captions (Noto Sans JP),
    vocab word cards on Japanese clips, -14 LUFS, NVENC. Writes preview frames
    (25 %% / 75 %%) and shorts/_preview/sheet.jpg. Uses the GPU: not while OBS is live.
    """
    import json

    from PIL import Image

    from . import render
    from . import transcribe as tr

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    _recording(session)
    try:
        clips_file = sessions.load_clips(session_dir)
    except FileNotFoundError:
        console.print(f"[red]No clips.yaml. Run /stream-moments {session.session} first.[/red]")
        raise typer.Exit(code=2)
    if clips_file.status != "approved":
        console.print("[red]clips.yaml isn't approved yet (Gate 2).[/red]")
        raise typer.Exit(code=2)
    problems = clips_file.problems(session)
    if problems:
        for p in problems:
            console.print(f"[red]clips.yaml:[/red] {p}")
        raise typer.Exit(code=2)

    wanted = [c for c in clips_file.clips if c.approved and "short" in c.targets]
    if clip:
        unknown = set(clip) - {c.id for c in clips_file.clips}
        if unknown:
            console.print(f"[red]No clip(s) {sorted(unknown)} in clips.yaml.[/red]")
            raise typer.Exit(code=2)
        not_ok = [c for c in clip if c not in {w.id for w in wanted}]
        if not_ok:
            console.print(f"[red]Clip(s) {not_ok} aren't approved for a Short.[/red]")
            raise typer.Exit(code=2)
        wanted = [c for c in wanted if c.id in clip]

    layouts = render.load_layouts()
    missing_layouts = sorted({c.layout for c in wanted} - set(layouts))
    if missing_layouts:
        console.print(f"[red]No layout(s) {missing_layouts} in {render.LAYOUTS_YAML}.[/red]")
        raise typer.Exit(code=2)
    fonts_dir = render.ensure_fonts()
    words = {
        s.id: json.loads(tr.paths(session_dir, s.id)["words"].read_text(encoding="utf-8"))
        for s in session.segments if tr.is_done(session_dir, s.id)
    }
    preview_dir = session_dir / "shorts" / "_preview"
    previews: list[Path] = []

    for c in wanted:
        if c.segment not in words:
            console.print(f"[red]{c.id}: no transcript for segment {c.segment}.[/red]")
            raise typer.Exit(code=2)
        target = session_dir / "shorts" / f"{c.id}.mp4"
        if not still and target.exists() and not force:
            console.print(f"  {c.id}: already rendered (--force to redo)")
            previews.append(preview_dir / f"{c.id}_25.jpg")
            continue
        t0 = time.monotonic()
        with console.status(f"{c.id}: {'still' if still else 'rendering'} ({c.short_duration:.0f}s, layout {c.layout}) …"):
            out, n_cards = render.render_clip(
                session, c, layouts[c.layout], words[c.segment], session_dir,
                fonts_dir=fonts_dir, still_at=c.short_duration * 0.4 if still else None,
            )
            if still:
                previews.append(out)
            else:
                previews.extend(render.preview_frames(out, c.short_duration, preview_dir)[:1])
        cards = f", {n_cards} word card(s)" if n_cards else ""
        console.print(f"[green]✔[/green] {c.id} {c.title!r}{cards} in {time.monotonic() - t0:.0f}s → {out}")

    # One contact sheet of the previews, for a single look at every Short.
    shots = [p for p in previews if p.exists()]
    if shots:
        tiles = [Image.open(p).convert("RGB").resize((270, 480)) for p in shots]
        cols = min(7, len(tiles))
        sheet = Image.new("RGB", (270 * cols, 480 * (-(-len(tiles) // cols))), (0, 0, 0))
        for i, t in enumerate(tiles):
            sheet.paste(t, ((i % cols) * 270, (i // cols) * 480))
        sheet_path = preview_dir / ("stills.jpg" if still else "sheet.jpg")
        sheet.save(sheet_path, quality=85)
        console.print(f"  previews: {sheet_path}")

    if not still:
        done = all((session_dir / "shorts" / f"{c.id}.mp4").exists()
                   for c in clips_file.clips if c.approved and "short" in c.targets)
        if done and session.status == "clips_approved":
            sessions.set_status(session_dir, "rendered")
            console.print(f"[green]✔[/green] {session.session}: status clips_approved → rendered")


# ---------------------------------------------------------------------------
# stream-cut-vods / stream-thumbs
# ---------------------------------------------------------------------------

def _publishable(session: Session, segment: Optional[list[int]]) -> list:
    segs = [s for s in session.segments if s.publishable]
    if segment:
        try:
            chosen = [session.segment(i) for i in segment]
        except KeyError as exc:
            console.print(f"[red]{exc.args[0]}[/red]")
            raise typer.Exit(code=2)
        bad = [s.id for s in chosen if not s.publishable]
        if bad:
            console.print(f"[red]Segment(s) {bad} are skip or flagged under_10_min / shorts_only.[/red]")
            raise typer.Exit(code=2)
        segs = chosen
    return segs


def stream_cut_vods(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id (2026-10-05) or folder."),
    segment: Optional[list[int]] = typer.Option(None, "--segment", "-s", help="Segment id (repeatable). Default: every publishable segment."),
    force: bool = typer.Option(False, "--force", help="Re-cut segments that already have a VOD file."),
) -> None:
    """Cut each publishable segment to vods/seg<N>_<series>.mp4 (stream copy, keyframe start).

    Records the real start in vods/seg<N>.cut.json for chapter times, and
    reports each series' playlist and numbering status from series.yaml.
    """
    from . import vods

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    _recording(session)
    if not session.at_least("segments_approved"):
        console.print(f"[red]{session.session} is {session.status}: approve the segments (Gate 1) first.[/red]")
        raise typer.Exit(code=2)
    series = vods.load_series()
    for s in _publishable(session, segment):
        target = vods.vod_path(session_dir, s)
        if target.exists() and vods.cut_info(session_dir, s.id) and not force:
            console.print(f"  segment {s.id}: already cut (--force to redo) → {target}")
            continue
        t0 = time.monotonic()
        info = vods.cut_segment(session, s, session_dir)
        size = target.stat().st_size / 1e9
        console.print(
            f"[green]✔[/green] segment {s.id}: {fmt_short(info['duration'])}, {size:.1f} GB in "
            f"{time.monotonic() - t0:.0f}s (starts {info['requested_start'] - info['start']:.2f}s before "
            f"{fmt_tc(info['requested_start'])} on a keyframe) → {target}"
        )
        ser = series.get(s.series or "")
        if ser is None:
            console.print(f"  [yellow]series {s.series!r} isn't in {vods.SERIES_YAML}: add it before publishing[/yellow]")
        else:
            label = vods.episode_label(ser, session.session, s.episode_segment)
            console.print(f"  series {ser['name']!r} {label} (numbering {ser.get('numbering')}), "
                          f"playlist {ser.get('playlist_id') or 'not created yet'}")


def stream_thumbs(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id (2026-10-05) or folder."),
    segment: Optional[list[int]] = typer.Option(None, "--segment", "-s", help="Segment id (repeatable)."),
    count: int = typer.Option(16, "--count", help="Candidate frames per segment."),
) -> None:
    """Thumbnail candidates per segment: full-res frames at loudness peaks, spoken vocab
    and chapter starts, into vods/seg<N>_thumbs/candidates/, plus a labelled sheet.

    stream-thumbnail then composes pick1-3.jpg from them.
    """
    from . import sheets, vods

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    _recording(session)
    for s in _publishable(session, segment):
        frames = vods.extract_thumbs(session, s, session_dir, count)
        sheet = sheets.build_sheet(frames, vods.thumbs_dir(session_dir, s.id) / "candidates_sheet.jpg",
                                   session, cols=4, tile_w=640)
        console.print(f"[green]✔[/green] segment {s.id}: {len(frames)} candidates → {sheet}")


def stream_thumbnail(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id (2026-10-05) or folder."),
    segment: Optional[list[int]] = typer.Option(None, "--segment", "-s", help="Segment id (repeatable)."),
    force: bool = typer.Option(False, "--force", help="Rebuild even when nothing changed."),
) -> None:
    """Compose the episode thumbnails vods/seg<N>_thumbs/pick1-3.jpg (1280x720): best game
    frame, the streamer's reaction cut out of the cam, the `thumb` text from seg<N>.meta.json (a
    Japanese word or an English hook), and a series + episode badge. pick1 is what
    stream-upload sets. Extracts candidates first if stream-thumbs hasn't. CPU only."""
    from . import thumbnail

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    for s in _publishable(session, segment):
        try:
            out = thumbnail.build_episode_thumbs(session, s, session_dir, force=force, say=console.print)
        except (RuntimeError, OSError) as exc:
            console.print(f"[red]segment {s.id}: {exc}[/red]")
            continue
        if out:
            console.print(f"[green]✔[/green] segment {s.id}: {', '.join(p.name for p in out)} in {out[0].parent}")
        else:
            console.print(f"  segment {s.id}: up to date (--force to rebuild)")


def stream_thumbnail_push(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id or folder."),
    go: bool = typer.Option(False, "--go", help="Set the thumbnails. Without --go it only lists what would change."),
    item: Optional[list[str]] = typer.Option(None, "--item", "-i", help="Limit to these episodes (seg2; repeatable)."),
) -> None:
    """Set pick1.jpg on episodes already on YouTube (publish.yaml), for thumbnails rebuilt
    by stream-thumbnail after upload. Remembers what it pushed in seg<N>_thumbs/pushed.json,
    so reruns only send changed ones."""
    from . import publish, thumbnail, vods, youtube

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    log = publish.load_log(session_dir, session)
    keys = [k for k in log.youtube if k.startswith("seg") and (not item or k in set(item))]
    console.print(f"stream-thumbnail-push {session.session}: " + ("[bold green]GO[/bold green]" if go
                  else "[bold yellow]DRY RUN[/bold yellow] (nothing is changed)"))
    if not keys:
        console.print("  no episodes in publish.yaml")
    for key in keys:
        tdir = vods.thumbs_dir(session_dir, int(key[3:]))
        pick, stamp = tdir / "pick1.jpg", tdir / "thumb.json"
        if not stamp.exists() or not pick.exists():
            console.print(f"  [yellow]{key}: no composed thumbnail yet (stream-thumbnail)[/yellow]")
            continue
        rec = log.youtube[key]
        if thumbnail.is_pushed(pick, rec.video_id):
            console.print(f"  {key}: up to date")
            continue
        console.print(f"  {key}: {rec.url} ← {pick.parent.name}/{pick.name}")
        if not go:
            continue
        try:
            youtube.set_thumbnail(rec.video_id, pick)
        except (youtube.YouTubeError, RuntimeError) as exc:
            console.print(f"    [red]{exc}[/red]")
            if "quota" in str(exc).casefold():
                break
            continue
        thumbnail.mark_pushed(pick, rec.video_id)
        console.print("    [green]✔[/green] set")
    if not go:
        console.print("\n[yellow]Dry run.[/yellow] Re-run with --go to set the thumbnails on YouTube.")


# ---------------------------------------------------------------------------
# stream-auth
# ---------------------------------------------------------------------------

def stream_auth(
    service: str = typer.Argument("twitch", help="'twitch', 'youtube' or 'instagram'."),
    check: bool = typer.Option(False, "--check", help="Just check the stored login."),
    token: Optional[str] = typer.Option(None, "--token", help="Instagram: a user token from Graph API Explorer "
                                        "instead of the browser login."),
) -> None:
    """Log in once (browser approval). Tokens are kept in <service>.token.json at the repo root (gitignored)."""
    if service == "instagram":
        from . import instagram

        try:
            if check:
                info = instagram.check()
            else:
                instagram.login(user_token=token, say=console.print)
                info = instagram.check()
        except (instagram.InstagramError, RuntimeError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2)
        console.print(f"[green]✔[/green] Instagram: @{info['username']} ({info['ig_user_id']}) through the Page "
                      f"{info['page_name']} ({info['page_id']}); token "
                      + ("never expires" if not info["expires_at"] else "EXPIRES")
                      + f"; {info['quota_used']}/{info['quota_total']} API posts used in the last 24 h")
        if info["data_access_expires_at"]:
            ends = dt.datetime.fromtimestamp(info["data_access_expires_at"], dt.timezone.utc)
            console.print(f"  data access window ends {ends:%Y-%m-%d} (logging in again renews it)")
        if not info.get("insights"):
            console.print("  [yellow]no instagram_manage_insights: stream-metrics gets likes and comments only "
                          "(log in again to add it)[/yellow]")
        for p in info["problems"]:
            console.print(f"  [red]✘ {p}[/red]")
        if info["problems"]:
            raise typer.Exit(code=2)
        return
    if service == "youtube":
        from . import youtube

        try:
            info = youtube.whoami() if check else youtube.login(say=console.print)
        except (youtube.YouTubeError, RuntimeError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2)
        can_upload = youtube.has_scopes(*youtube.UPLOAD_SCOPES)
        console.print(f"[green]✔[/green] YouTube: {'upload and read' if can_upload else 'read-only'} access to "
                      f"{info['title']} ({info['channel_id']}), refreshes automatically")
        if not youtube.has_scopes(youtube.ANALYTICS_SCOPE):
            console.print("  [yellow]no yt-analytics.readonly: stream-metrics gets public counts only "
                          "(run `stream-auth youtube` to add it)[/yellow]")
        return
    from . import twitch

    if service != "twitch":
        raise typer.BadParameter("'twitch', 'youtube' or 'instagram'")
    try:
        info = twitch.validate() if check else twitch.login(say=console.print)
    except (twitch.TwitchError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    console.print(f"[green]✔[/green] Twitch: logged in as {info['login']} ({info['user_id']}), "
                  f"scopes {', '.join(info.get('scopes', []))}, token valid {int(info.get('expires_in', 0)) // 60} min "
                  f"(refreshes automatically)")
    if twitch.FOLLOWERS_SCOPE not in info.get("scopes", []):
        console.print("  [yellow]no moderator:read:followers: stream-metrics can't count follows per day "
                      "(run `stream-auth twitch` to add it)[/yellow]")


# ---------------------------------------------------------------------------
# stream-youtube-sync
# ---------------------------------------------------------------------------

def stream_youtube_sync(
    session_refs: list[str] = typer.Argument(..., metavar="SESSION...", help="Session ids (2026-10-13) or folders."),
    go: bool = typer.Option(False, "--go", help="Record the matches in publish.yaml. Without --go it's a dry run."),
) -> None:
    """Find each session's uploaded Shorts and episodes on the channel by exact title (read-only
    YouTube API) and record their URLs in publish.yaml, like `stream-publish --youtube`. Private and
    scheduled uploads count. Titles that match more than one upload are reported, not recorded.
    """
    from . import publish, youtube

    def norm(t: str) -> str:
        return " ".join(t.split()).casefold()

    try:
        ups = youtube.uploads()
    except (youtube.YouTubeError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    by_title: dict[str, list[dict]] = {}
    for u in ups:
        by_title.setdefault(norm(u["title"]), []).append(u)
    console.print(f"YouTube: {len(ups)} upload(s) on the channel. "
                  + ("[bold green]GO[/bold green]" if go else "[bold yellow]DRY RUN[/bold yellow]"))

    for ref in session_refs:
        session_dir = _session_dir(ref)
        session = _load(session_dir)
        try:
            clips_file = sessions.load_clips(session_dir)
        except FileNotFoundError:
            console.print(f"\n[bold]{session.session}[/bold]: no clips.yaml, skipped")
            continue
        log = publish.load_log(session_dir, session)
        plan = publish.build_plan(session_dir, session, clips_file, log)
        wanted: list[tuple[str, str, bool]] = []  # (key, title, is_short)
        for c in plan.shorts:
            wanted.append((c.id, publish.short_copy(session_dir, c.id).get("title") or c.title, True))
        for seg in plan.episodes:
            if seg.id not in plan.blocked_episodes:
                wanted.append((f"seg{seg.id}", publish.vod_meta(session_dir, seg.id).get("title", ""), False))
        console.print(f"\n[bold]{session.session}[/bold]")
        changed = False
        for key, title, is_short in wanted:
            if key in log.youtube:
                console.print(f"  {key}: already recorded {log.youtube[key].url}")
                continue
            hits = by_title.get(norm(title), []) if title else []
            if not hits:
                console.print(f"  [yellow]{key}: not found[/yellow] ({title!r})")
                continue
            if len(hits) > 1:
                ids = ", ".join(h["video_id"] for h in hits)
                console.print(f"  [yellow]{key}: {len(hits)} uploads share this title ({ids}); record one with "
                              f"`stream-publish {session.session} --youtube {key}=<url> --go`[/yellow]")
                continue
            h = hits[0]
            url = (f"https://youtube.com/shorts/{h['video_id']}" if is_short
                   else f"https://youtu.be/{h['video_id']}")
            if youtube.video_status([h["video_id"]]).get(h["video_id"], {}).get("upload") != "processed":
                # "uploaded" is reported from the start of an upload: it may still be arriving
                console.print(f"  [yellow]{key}: {url} isn't processed yet (may still be uploading); "
                              f"not recorded, run again later[/yellow]")
                continue
            console.print(f"  [green]✔[/green] {key}: {url} ({h['privacy']}) {title}")
            if go:
                log.youtube[key] = publish.YouTubeRecord(url=url, video_id=h["video_id"], recorded_at=publish._now())
                changed = True
        if changed:
            publish.save_log(session_dir, log)
            publish.write_checklist(publish.build_plan(session_dir, session, clips_file, log))
            console.print(f"  recorded in {session_dir / 'publish.yaml'}; checklist updated")
    if not go:
        console.print("\n[yellow]Dry run: nothing recorded.[/yellow] Re-run with --go.")


# ---------------------------------------------------------------------------
# stream-playlist
# ---------------------------------------------------------------------------

def stream_playlist(
    series: str = typer.Argument(..., help='Series name as in series.yaml, e.g. "Wabi Sanji Responds".'),
    privacy: str = typer.Option("public", "--privacy", help="public, unlisted or private (only for a new playlist)."),
    go: bool = typer.Option(False, "--go", help="Create, record and add. Without --go it's a dry run."),
) -> None:
    """Give a series its YouTube playlist: the one in series.yaml, else a channel playlist named
    exactly like the series, else a new one. Records playlist_id in series.yaml and adds the series'
    uploaded episodes (publish.yaml seg<N>) that aren't in it yet, in episode order.
    """
    import yaml

    from . import publish, vods, youtube

    registry = vods.load_series()
    ser = registry.get(series)
    if ser is None:
        console.print(f"[red]No series {series!r} in series.yaml ({', '.join(registry)}).[/red]")
        raise typer.Exit(code=2)
    try:
        pid, source = ser.get("playlist_id"), "series.yaml"
        if not pid:
            hits = [p for p in youtube.my_playlists() if p["title"].strip().casefold() == series.casefold()]
            if len(hits) > 1:
                console.print(f"[red]{len(hits)} playlists on the channel are called {series!r} "
                              f"({', '.join(h['id'] for h in hits)}): set playlist_id in series.yaml by hand.[/red]")
                raise typer.Exit(code=2)
            if hits:
                pid, source = hits[0]["id"], "found on the channel"
        uploaded: list[tuple[int, str, str]] = []  # (episode, video id, session)
        for e in sorted(ser.get("episodes") or [], key=lambda e: e["episode"]):
            p = sessions.sessions_root() / e["session"] / publish.PUBLISH_YAML
            rec = ((yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("youtube") or {}).get(
                f"seg{e['segment']}") if p.exists() else None
            if rec:
                uploaded.append((e["episode"], rec["video_id"], e["session"]))
        present = set(youtube.playlist_video_ids(pid)) if pid else set()
    except (youtube.YouTubeError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)

    console.print(f"stream-playlist {series!r}: " + ("[bold green]GO[/bold green]" if go else "[bold yellow]DRY RUN[/bold yellow]"))
    if pid:
        console.print(f"  playlist {pid} ({source}): https://www.youtube.com/playlist?list={pid}")
    else:
        console.print(f"  no playlist yet: create {series!r} ({privacy})")
    todo = [u for u in uploaded if u[1] not in present]
    for ep, vid, sess in uploaded:
        console.print(f"  #{ep}  {vid}  {sess}  " + ("add" if (ep, vid, sess) in todo else "[dim]already in it[/dim]"))
    if not uploaded:
        console.print("  [dim]no uploaded episodes yet[/dim]")
    if not go:
        console.print("\n[yellow]Dry run: nothing created or added.[/yellow] Re-run with --go.")
        return

    try:
        if not pid:
            pid = youtube.create_playlist(series, privacy=privacy)
            console.print(f"  [green]✔[/green] created {pid} ({privacy})")
        if source != "series.yaml" or not ser.get("playlist_id"):
            publish.write_playlist_id(vods.SERIES_YAML, series, pid)
            console.print(f"  [green]✔[/green] playlist_id recorded in {vods.SERIES_YAML}")
        for ep, vid, _ in todo:
            youtube.add_to_playlist(vid, pid)
            console.print(f"  [green]✔[/green] #{ep} added")
    except (youtube.YouTubeError, RuntimeError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    console.print(f"  https://www.youtube.com/playlist?list={pid}")


# ---------------------------------------------------------------------------
# stream-schedule-sync
# ---------------------------------------------------------------------------

def stream_schedule_sync(
    go: bool = typer.Option(False, "--go", help="Write the changes. Without --go it only shows them."),
) -> None:
    """Bring the long-form table in sessions/_posting_schedule.md up to date with YouTube: each row
    whose episode is recorded in its session's publish.yaml gets Status Scheduled / Published /
    Uploaded (private), and Date/Time follow the real publish time when it differs from the slot.
    Times are written in the local time zone (WS_TIMEZONE).
    """
    import re

    from . import publish, youtube

    path = sessions.sessions_root() / "_posting_schedule.md"
    text = path.read_text(encoding="utf-8")
    head, sep, rest = text.partition("## Long-form episodes")
    table, sep2, tail = rest.partition("## Shorts")
    aedt = settings.local_tz()
    rows = []  # (line index, cells, video id)
    lines = table.split("\n")
    for i, line in enumerate(lines):
        if not line.startswith("|") or "---" in line or "Status" in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        m = re.search(r"([\w-]+)[\\/]vods[\\/]seg(\d+)", cells[3]) if len(cells) >= 5 else None
        if not m:
            continue
        sdir = sessions.sessions_root() / m.group(1)
        if not (sdir / "publish.yaml").exists():
            continue
        log = publish.load_log(sdir, _load(sdir))
        rec = log.youtube.get(f"seg{m.group(2)}")
        if rec:
            rows.append((i, cells, rec.video_id))
    try:
        status = youtube.video_status([vid for _, _, vid in rows])
    except (youtube.YouTubeError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    changed = 0
    for i, cells, vid in rows:
        st = status.get(vid)
        if not st:
            continue
        new = list(cells)
        if st["publish_at"]:  # follow the real publish time, whatever the state
            when = dt.datetime.fromisoformat(st["publish_at"].replace("Z", "+00:00")).astimezone(aedt)
            new[0], new[1] = when.strftime("%a %d %b"), when.strftime("%H:%M")
        if st["upload"] in ("failed", "rejected", "deleted"):
            new[4] = {"failed": "Upload failed", "rejected": "Rejected", "deleted": "Deleted"}[st["upload"]]
        elif st["upload"] == "uploaded" or st["processing"] == "processing":
            new[4] = "Processing"
        elif st["privacy"] == "public":
            new[4] = "Published"
        elif st["publish_at"]:
            new[4] = "Scheduled"
        else:
            new[4] = "Uploaded (private)"
        if new != cells:
            console.print(f"  {cells[2][:60]}: {cells[0]} {cells[1]} {cells[4]} -> {new[0]} {new[1]} {new[4]}")
            lines[i] = "| " + " | ".join(new) + " |"
            changed += 1
    if not changed:
        console.print("_posting_schedule.md already matches YouTube")
        return
    if go:
        path.write_text(head + sep + "\n".join(lines) + sep2 + tail, encoding="utf-8")
        console.print(f"[green]✔[/green] {changed} row(s) updated in {path}")
    else:
        console.print("[yellow]Dry run.[/yellow] Re-run with --go to write.")


# ---------------------------------------------------------------------------
# stream-upload
# ---------------------------------------------------------------------------

def stream_upload(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id (2026-10-13) or folder."),
    go: bool = typer.Option(False, "--go", help="Actually upload. Without --go it's a dry run."),
    only: Optional[str] = typer.Option(None, "--only", help="'shorts' or 'episodes'."),
    item: Optional[list[str]] = typer.Option(None, "--item", "-i", help="Limit to these ids (c01, seg2; repeatable)."),
    schedule: Optional[list[str]] = typer.Option(None, "--schedule", help="Go public at a time: <id>=<ISO time with offset>, "
                                                 "e.g. seg1=2026-10-14T09:00+11:00 (repeatable). Others stay private."),
) -> None:
    """Upload a session's approved Shorts and episodes to YouTube as PRIVATE, from the same
    title/description/tags as upload-checklist.md. Episodes also get thumbnail pick1.jpg and
    their series playlist. Anything already in publish.yaml, or already on the channel with the
    exact title, is skipped (the latter is recorded). The streamer makes videos public in Studio.
    """
    from . import publish, thumbnail, vods, youtube

    if only not in (None, "shorts", "episodes"):
        raise typer.BadParameter("--only takes 'shorts' or 'episodes'")
    publish_at: dict[str, dt.datetime] = {}
    public_now: set[str] = set()
    for s in schedule or []:
        key, _, when = s.partition("=")
        if when.strip().casefold() == "now":  # public straight away (the streamer asked for it explicitly)
            public_now.add(key.strip())
            continue
        try:
            t = dt.datetime.fromisoformat(when.strip())
        except ValueError:
            raise typer.BadParameter(f"--schedule wants <id>=<ISO time>, got {s!r}")
        if t.tzinfo is None:
            raise typer.BadParameter(f"--schedule {s!r}: give a UTC offset, e.g. +11:00")
        if t <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15):
            raise typer.BadParameter(f"--schedule {s!r} is in the past (or under 15 minutes away)")
        publish_at[key.strip()] = t
    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    try:
        clips_file = sessions.load_clips(session_dir)
    except FileNotFoundError:
        console.print("[red]No clips.yaml.[/red]")
        raise typer.Exit(code=2)
    # Episodes only need Gate 1 (approved segments); Shorts also need Gate 2 and a render.
    if only == "episodes":
        ready = session.at_least("segments_approved")
    else:
        ready = clips_file.status == "approved" and session.at_least("rendered")
    if not ready:
        console.print(f"[red]Not ready: clips.yaml {clips_file.status}, session {session.status}.[/red]")
        raise typer.Exit(code=2)
    log = publish.load_log(session_dir, session)
    plan = publish.build_plan(session_dir, session, clips_file, log)
    registry = vods.load_series()

    todo: list[dict] = []
    if only != "episodes":
        for c in plan.shorts:
            copy = publish.short_copy(session_dir, c.id)
            todo.append({"key": c.id, "file": session_dir / "shorts" / f"{c.id}.mp4",
                         "title": copy.get("title") or c.title, "description": copy.get("description", ""),
                         "tags": [], "thumb": None, "playlist": None, "short": True})
    if only != "shorts":
        for seg in plan.episodes:
            if seg.id in plan.blocked_episodes:
                console.print(f"  [yellow]seg{seg.id}: {plan.blocked_episodes[seg.id]}[/yellow]")
                continue
            meta = publish.vod_meta(session_dir, seg.id)
            todo.append({"key": f"seg{seg.id}", "file": vods.vod_path(session_dir, seg),
                         "title": meta.get("title", ""), "description": meta.get("description", ""),
                         "tags": [t.strip() for t in meta.get("tags", "").split(",") if t.strip()],
                         "thumb": session_dir / "vods" / f"seg{seg.id}_thumbs" / "pick1.jpg",
                         "playlist": registry.get(seg.series or "", {}).get("playlist_id"), "short": False})
    if item:
        todo = [t for t in todo if t["key"] in set(item)]

    try:
        if go and not youtube.has_scopes(*youtube.UPLOAD_SCOPES):
            raise youtube.YouTubeError("the stored login can't upload: run `stream-auth youtube` again "
                                       "and approve the upload permissions")
        on_channel: dict[str, list[str]] = {}
        for u in youtube.uploads():
            on_channel.setdefault(" ".join(u["title"].split()).casefold(), []).append(u["video_id"])
    except (youtube.YouTubeError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)

    console.print(f"stream-upload {session.session}: " + ("[bold green]GO[/bold green] (private)" if go
                  else "[bold yellow]DRY RUN[/bold yellow] (nothing is uploaded)"))
    for t in todo:
        key, title = t["key"], t["title"]
        url_for = (lambda vid: f"https://youtube.com/shorts/{vid}") if t["short"] else (lambda vid: f"https://youtu.be/{vid}")
        if key in log.youtube:
            rec = log.youtube[key]
            if key in public_now or key in publish_at:  # already uploaded: change when it goes public
                change = "public now" if key in public_now else f"public at {publish_at[key].isoformat(timespec='minutes')}"
                console.print(f"  {key}: already uploaded {rec.url}; set {change}")
                if go:
                    try:
                        if key in public_now:
                            youtube.make_public(rec.video_id)
                        else:
                            youtube.schedule_existing(rec.video_id, publish_at[key])
                        console.print(f"    [green]✔[/green] {change}")
                    except youtube.YouTubeError as exc:
                        console.print(f"    [red]{exc}[/red]")
                continue
            console.print(f"  {key}: already recorded {rec.url}")
            continue
        if not title or not t["file"].exists():
            console.print(f"  [yellow]{key}: skipped ({'no title' if not title else 'no file ' + t['file'].name})[/yellow]")
            continue
        existing = on_channel.get(" ".join(title.split()).casefold(), [])
        if existing:
            if len(existing) == 1 and youtube.video_status(existing).get(existing[0], {}).get("upload") != "processed":
                console.print(f"  [yellow]{key}: already on the channel ({existing[0]}) but not processed yet "
                              f"(may still be uploading); not recorded, not uploading[/yellow]")
                continue
            if len(existing) == 1 and go:
                log.youtube[key] = publish.YouTubeRecord(url=url_for(existing[0]), video_id=existing[0],
                                                         recorded_at=publish._now())
                publish.save_log(session_dir, log)
            console.print(f"  {key}: already on the channel ({', '.join(existing)})"
                          + (", recorded" if len(existing) == 1 and go else ", not uploading"))
            continue
        extras = []
        if t["thumb"] is not None:
            extras.append(f"thumbnail {t['thumb'].name}" if t["thumb"].exists() else "[yellow]no pick1.jpg[/yellow]")
        if not t["short"]:
            extras.append(f"playlist {t['playlist']}" if t["playlist"] else "no playlist yet")
        if key in publish_at:
            extras.append(f"public at {publish_at[key].isoformat(timespec='minutes')}")
        if key in public_now:
            extras.append("PUBLIC NOW")
        size_gb = t["file"].stat().st_size / 1e9
        console.print(f"  {key}: {title}  ({t['file'].name}, {size_gb:.2f} GB"
                      + (f"; {'; '.join(extras)}" if extras else "") + ")")
        if not go:
            continue
        try:
            with console.status(f"uploading {key} …") as st:
                vid = youtube.upload_video(
                    t["file"], title, t["description"], t["tags"], publish_at=publish_at.get(key),
                    public_now=key in public_now,
                    progress=lambda done, total, st=st, key=key: st.update(
                        f"uploading {key}: {done / 1e9:.2f} / {total / 1e9:.2f} GB"))
            log.youtube[key] = publish.YouTubeRecord(url=url_for(vid), video_id=vid, recorded_at=publish._now())
            publish.save_log(session_dir, log)  # record before the extras: never upload it twice
            console.print(f"    [green]✔[/green] {url_for(vid)} (" + ("PUBLIC" if key in public_now else "private") + (f", public at {publish_at[key].isoformat(timespec='minutes')}" if key in publish_at else "") + ")")
            if t["thumb"] is not None and t["thumb"].exists():
                youtube.set_thumbnail(vid, t["thumb"])
                thumbnail.mark_pushed(t["thumb"], vid)
                console.print(f"    [green]✔[/green] thumbnail {t['thumb'].name}")
            if t["playlist"]:
                youtube.add_to_playlist(vid, t["playlist"])
                console.print(f"    [green]✔[/green] added to playlist {t['playlist']}")
        except youtube.YouTubeError as exc:
            console.print(f"    [red]{exc}[/red]")
            if "quota" in str(exc).casefold() or "uploadLimitExceeded" in str(exc):
                console.print("[red]Stopping: out of YouTube quota for today. Rerun tomorrow; finished uploads "
                              "are recorded.[/red]")
                break
    publish.write_checklist(publish.build_plan(session_dir, session, clips_file, log))
    if not go:
        console.print("\n[yellow]Dry run: nothing uploaded.[/yellow] Re-run with --go (uploads are private).")


def stream_describe(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id or folder."),
    go: bool = typer.Option(False, "--go", help="Update YouTube. Without --go it only shows what differs."),
    item: Optional[list[str]] = typer.Option(None, "--item", "-i", help="Limit to these ids (r01, seg1; repeatable)."),
) -> None:
    """Push edited descriptions to videos already on YouTube: for each Short and episode recorded in
    publish.yaml, compare shorts/<id>.copy.md or vods/seg<N>.youtube.md with the live description and
    update the ones that differ. Rebuild seg<N>.youtube.md (stream-youtube-md) after editing meta.json."""
    from . import publish, youtube

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    log = publish.load_log(session_dir, session)
    keys = [k for k in log.youtube if not item or k in set(item)]
    console.print(f"stream-describe {session.session}: " + ("[bold green]GO[/bold green]" if go
                  else "[bold yellow]DRY RUN[/bold yellow] (nothing is changed)"))
    for key in keys:
        local = (publish.vod_meta(session_dir, int(key[3:])) if key.startswith("seg")
                 else publish.short_copy(session_dir, key)).get("description", "")
        if not local:
            console.print(f"  [yellow]{key}: no local description[/yellow]")
            continue
        rec = log.youtube[key]
        try:
            live = youtube.video_description(rec.video_id)
            if live is None:
                console.print(f"  [yellow]{key}: {rec.video_id} isn't on the channel[/yellow]")
                continue
            if live.strip() == local.strip():
                console.print(f"  {key}: up to date")
                continue
            console.print(f"  {key}: {rec.url} differs")
            for line in difflib.unified_diff(live.strip().splitlines(), local.strip().splitlines(), "live", "local", n=0, lineterm=""):
                if not line.startswith(("---", "+++", "@@")):
                    console.print(f"    {line}", markup=False, highlight=False)
            if go:
                youtube.update_description(rec.video_id, local.strip())
                console.print("    [green]✔[/green] updated")
        except (youtube.YouTubeError, RuntimeError) as exc:
            console.print(f"    [red]{exc}[/red]")
            if "quota" in str(exc).casefold():
                break
    if not go:
        console.print("\n[yellow]Dry run.[/yellow] Re-run with --go to update YouTube.")


# ---------------------------------------------------------------------------
# stream-social
# ---------------------------------------------------------------------------

def stream_social(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id (2026-10-13) or folder."),
    init: bool = typer.Option(False, "--init", help="Add an empty, unapproved social.yaml entry for every "
                              "approved Short not in it yet (existing entries are kept)."),
    go: bool = typer.Option(False, "--go", help="Actually post. Without --go it's a dry run."),
    item: Optional[list[str]] = typer.Option(None, "--item", "-i", help="Limit to these clip ids (repeatable)."),
) -> None:
    """Post a session's Shorts to Instagram as Reels, from the captions in social.yaml.
    Only entries with `approved: true` post, and they are PUBLIC straight away (Instagram has
    no private stage). Posted Reels are recorded in publish.yaml; reruns skip them, and a Reel
    already on the account with the same caption is recorded instead of posted again.
    """
    from . import instagram, publish, social

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    try:
        clips_file = sessions.load_clips(session_dir)
    except FileNotFoundError:
        console.print("[red]No clips.yaml.[/red]")
        raise typer.Exit(code=2)
    if clips_file.status != "approved" or not session.at_least("rendered"):
        console.print(f"[red]Not ready: clips.yaml {clips_file.status}, session {session.status}.[/red]")
        raise typer.Exit(code=2)
    try:
        sf = social.load_social(session_dir)
    except Exception as exc:  # show the validation error, not a traceback
        console.print(f"[red]social.yaml is invalid: {exc}[/red]")
        raise typer.Exit(code=2)
    if init:
        sf, added = social.add_missing(sf, session.session, clips_file)
        path = social.save_social(session_dir, sf)
        console.print(f"[green]✔[/green] {path}: " + (f"added {', '.join(added)}" if added else "nothing to add"))
    if sf is None:
        console.print(f"[red]No social.yaml.[/red] Draft captions with /stream-social {session.session} "
                      f"(or start one with --init).")
        raise typer.Exit(code=2)

    log = publish.load_log(session_dir, session)
    plan = social.instagram_plan(session_dir, clips_file, sf, log.instagram)
    rows = [r for r in plan.rows if not item or r.key in set(item)]
    console.print(f"stream-social {session.session}: " + ("[bold red]GO[/bold red] (Instagram posts are PUBLIC)"
                  if go else "[bold yellow]DRY RUN[/bold yellow] (nothing is posted)"))
    colour = {"posted": "green", "ready": "bold", "waiting": "yellow", "blocked": "red"}
    for r in rows:
        first = (r.post.caption.strip().splitlines() or [""])[0] if r.post else ""
        console.print(f"  [{colour[r.state]}]{r.key}: {r.state}[/{colour[r.state]}]"
                      + (f" ({r.reason})" if r.reason else "") + (f"  {first[:70]}" if r.state != "posted" else ""))
    todo = [r for r in rows if r.state == "ready"]
    if not go:
        console.print(f"\n[yellow]Dry run.[/yellow] {len(todo)} ready to post. Re-run with --go to post them "
                      f"(public straight away).")
        return
    if not todo:
        console.print("Nothing approved to post.")
        return

    try:
        quota = instagram.publishing_quota()
        recent = instagram.recent_media()
    except (instagram.InstagramError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    left = quota["quota_total"] - quota["quota_used"]
    for r in todo:
        key, caption = r.key, r.post.caption.strip()
        found = [m for m in recent if instagram.same_caption(m.get("caption", ""), caption)]
        if found:
            m = found[0]
            log.instagram[key] = publish.InstagramRecord(media_id=m["id"], permalink=m.get("permalink", ""),
                                                         posted_at=m.get("timestamp") or publish._now())
            publish.save_log(session_dir, log)
            console.print(f"  {key}: already on Instagram ({m.get('permalink', m['id'])}), recorded")
            continue
        if left <= 0:
            console.print(f"[red]Stopping: Instagram's {quota['quota_total']} API posts per 24 h are used up. "
                          f"Rerun later; finished posts are recorded.[/red]")
            break
        try:
            with console.status(f"{key}: posting …") as st:
                if r.video.stat().st_size > instagram.UPLOAD_MAX:
                    st.update(f"{key}: making a smaller copy for Instagram …")
                video = instagram.fit_for_upload(r.video, session_dir / "shorts" / "_instagram")
                res = instagram.publish_reel(video, caption, cover_ms=r.post.cover_ms,
                                             on_stage=lambda s, st=st, key=key: st.update(f"{key}: {s} …"))
            rec = publish.InstagramRecord(media_id=res["media_id"], container_id=res["container_id"],
                                          posted_at=publish._now())
            log.instagram[key] = rec
            publish.save_log(session_dir, log)  # record before anything else: never post it twice
            left -= 1
            try:
                rec.permalink = instagram.permalink(res["media_id"])
                publish.save_log(session_dir, log)
            except instagram.InstagramError:
                pass  # it's live either way
            console.print(f"    [green]✔[/green] {rec.permalink or res['media_id']} (public)")
        except instagram.InstagramError as exc:
            console.print(f"    [red]{key}: {exc}[/red]")
            if exc.stop:
                console.print("[red]Stopping: login or rate-limit problem (see above). "
                              "`stream-auth instagram --check` shows the login state.[/red]")
                break


# ---------------------------------------------------------------------------
# stream-number
# ---------------------------------------------------------------------------

def stream_number(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the numbering without writing series.yaml."),
    force: bool = typer.Option(False, "--force", help="Rewrite numbers of a locked series even if they'd change."),
) -> None:
    """Rebuild each series' episode list in content/stream/series.yaml from every approved
    session.yaml, numbered by stream date. Titles only use the numbers once a series is
    `numbering: locked` (set that by hand when the backlog for that game is segmented).
    """
    from . import publish, vods

    rep = publish.renumber(sessions.sessions_root(), sessions.recordings_root())
    registry = vods.load_series()
    for p in rep.problems:
        console.print(f"[yellow]warning:[/yellow] {p}")
    for name in rep.unknown_series:
        console.print(f"[yellow]series {name!r} is used by a session but missing from series.yaml[/yellow]")
    for name, eps in rep.series.items():
        ser = registry.get(name, {})
        state = ser.get("numbering", "?")
        mark = "[green]changed[/green]" if rep.changed.get(name) else "unchanged"
        console.print(f"[bold]{name}[/bold] (numbering {state}, {mark})")
        for e in eps:
            console.print(f"  #{e['episode']}  {e['session']} segment {e['segment']}")
        if not eps:
            console.print("  (no approved episodes)")
    if rep.unsegmented:
        oldest, newest = rep.unsegmented[0].name, rep.unsegmented[-1].name
        console.print(f"[yellow]{len(rep.unsegmented)} recording(s) aren't in any session yet[/yellow] "
                      f"({oldest} … {newest}). Segment the backlog (/stream-backlog) before locking a series: "
                      f"earlier streams of a game shift its episode numbers.")
    if rep.locked_conflicts and not force:
        console.print(f"[red]Locked series would renumber: {rep.locked_conflicts}. Published numbers must not change; "
                      f"use --force only if nothing is published yet.[/red]")
        raise typer.Exit(code=2)
    if dry_run:
        console.print("[dim]dry run: series.yaml not written[/dim]")
        return
    if any(rep.changed.values()):
        publish.write_episodes(vods.SERIES_YAML, rep.series)
        console.print(f"[green]✔[/green] wrote {vods.SERIES_YAML}")
    else:
        console.print("series.yaml already up to date")


# ---------------------------------------------------------------------------
# stream-publish
# ---------------------------------------------------------------------------

def stream_publish(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id (2026-10-05) or folder."),
    go: bool = typer.Option(False, "--go", help="Actually do it. Without --go every run is a dry run."),
    youtube: Optional[list[str]] = typer.Option(None, "--youtube", help="Record an upload: <clip id or segN>=<YouTube URL> (repeatable)."),
    only: Optional[list[str]] = typer.Option(None, "--only", help="youtube, twitch and/or discord (repeatable)."),
    clip: Optional[list[str]] = typer.Option(None, "--clip", "-c", help="Limit Twitch to these clip ids, Discord to these episodes (seg2)."),
    vod_offset: Optional[float] = typer.Option(None, "--vod-offset", help="Recording seconds at VOD 0:00 (default: from the VOD's created_at)."),
) -> None:
    """Publish an approved session: Studio checklist, YouTube URLs, Twitch clips, Discord posts.

    Writes upload-checklist.md (YouTube uploads stay manual: unaudited API uploads get locked
    private). Everything created goes into publish.yaml and is never redone. Discord goes last:
    one post per long-form episode, linking it on YouTube (Shorts and clips aren't posted to
    Discord). Dry run unless --go.
    """
    from . import publish, twitch

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    try:
        clips_file = sessions.load_clips(session_dir)
    except FileNotFoundError:
        console.print("[red]No clips.yaml.[/red]")
        raise typer.Exit(code=2)
    parts = set(only or ["youtube", "twitch", "discord"])
    unknown = parts - {"youtube", "twitch", "discord"}
    if unknown:
        raise typer.BadParameter(f"--only takes youtube, twitch, discord (got {sorted(unknown)})")
    # Discord posts are episodes only, which need Gate 1; Twitch clips and Shorts also need Gate 2.
    if parts == {"discord"}:
        ready = session.at_least("segments_approved")
    else:
        ready = clips_file.status == "approved" and session.at_least("rendered")
    if not ready:
        console.print(f"[red]Not ready: clips.yaml {clips_file.status}, session {session.status} "
                      f"(needs approved clips and rendered Shorts).[/red]")
        raise typer.Exit(code=2)
    known_ids = {c.id for c in clips_file.clips} | {f"seg{s.id}" for s in session.segments if s.publishable}
    if clip and set(clip) - known_ids:
        raise typer.BadParameter(f"unknown clip id(s): {sorted(set(clip) - known_ids)}")
    log = publish.load_log(session_dir, session)
    mode = "[bold green]GO[/bold green]" if go else "[bold yellow]DRY RUN[/bold yellow] (nothing is created or posted)"
    console.print(f"stream-publish {session.session}: {mode}")

    # -- YouTube URLs pasted by the streamer -------------------------------------------------------
    recorded: dict[str, str] = {}
    valid_keys = {c.id for c in clips_file.clips if c.approved and "short" in c.targets} | {
        f"seg{s.id}" for s in session.segments if s.publishable}
    for item in youtube or []:
        if "=" not in item:
            raise typer.BadParameter(f"--youtube wants <id>=<url>, got {item!r}")
        key, url = (x.strip() for x in item.split("=", 1))
        if key not in valid_keys:
            raise typer.BadParameter(f"{key!r} isn't an approved Short or publishable segment ({sorted(valid_keys)})")
        try:
            vid = publish.youtube_video_id(url)
        except ValueError as exc:
            raise typer.BadParameter(str(exc))
        if key in log.youtube and log.youtube[key].video_id != vid:
            console.print(f"[yellow]{key} already recorded as {log.youtube[key].url}; keeping that[/yellow]")
            continue
        recorded[key] = url
        if go:
            log.youtube[key] = publish.YouTubeRecord(url=url, video_id=vid, recorded_at=publish._now())

    # -- Twitch: VOD and its offset from the recording --------------------------------------
    helix = None
    vod = None
    vod_checked = False
    if "twitch" in parts and session.twitch_vod_id:
        try:
            helix = twitch.Helix()
            vod = helix.video(session.twitch_vod_id)
            vod_checked = True
        except (twitch.TwitchError, RuntimeError) as exc:
            console.print(f"[yellow]Twitch: {exc}[/yellow]")
        if vod is not None:
            rough = (twitch.vod_created_at(vod) - session.recording_start).total_seconds()
            log.twitch_vod_id = session.twitch_vod_id
            if vod_offset is not None:
                log.twitch_vod_start, log.twitch_vod_start_method = round(vod_offset, 1), "manual"
            used = (f"{log.twitch_vod_start:+.1f} s ({log.twitch_vod_start_method})"
                    if log.twitch_vod_start_method in publish.TRUSTED_OFFSET else "not measured yet")
            expires = twitch.vod_created_at(vod) + dt.timedelta(days=14)
            console.print(f"Twitch VOD {session.twitch_vod_id}: created_at says {rough:+.0f} s after the recording "
                          f"(not trusted); offset used: {used}; clippable until about "
                          f"{expires.astimezone(session.recording_start.tzinfo):%Y-%m-%d %H:%M}")

    plan = publish.build_plan(session_dir, session, clips_file, log, vod, vod_checked)
    if helix is None and "twitch" in parts and plan.twitch_todo:
        plan.twitch_todo, plan.twitch_blocked = [], "not logged in to Twitch: run `stream-auth twitch`"

    # -- Show the plan -----------------------------------------------------------------------
    if "youtube" in parts:
        checklist = publish.write_checklist(plan)
        console.print(f"\n[bold]YouTube[/bold] (manual upload in Studio) → checklist {checklist}")
        for c in plan.shorts:
            state = log.youtube[c.id].url if c.id in log.youtube else (
                f"record {recorded[c.id]}" if c.id in recorded else "to upload")
            console.print(f"  Short {c.id}: {state}")
        for s in plan.episodes:
            k = f"seg{s.id}"
            state = log.youtube[k].url if k in log.youtube else (
                f"record {recorded[k]}" if k in recorded else
                f"[yellow]don't upload yet: {plan.blocked_episodes[s.id]}[/yellow]" if s.id in plan.blocked_episodes else "to upload")
            console.print(f"  Episode seg{s.id}: {state}")

    tw = [c for c in plan.twitch_todo if not clip or c.id in clip]
    if "twitch" in parts:
        console.print(f"\n[bold]Twitch clips[/bold] (VOD {session.twitch_vod_id or '-'})")
        if plan.twitch_blocked:
            console.print(f"  [yellow]blocked: {plan.twitch_blocked}[/yellow]")
        table = Table(show_edge=False)
        for col in ("clip", "recording", "VOD start", "VOD end (vod_offset)", "len", "title"):
            table.add_column(col)
        for c in tw:
            end, dur = plan.clip_window(c)
            table.add_row(c.id, f"{fmt_tc(c.start)}-{fmt_tc(c.end)}", fmt_tc(end - dur), f"{fmt_tc(end)} ({end})",
                          f"{dur:.1f}s", c.title[:100])
        if tw:
            console.print(table)
        for cid, rec in log.twitch_clips.items():
            drift = f", drift {rec.drift:+.1f}s" if rec.drift is not None else ""
            console.print(f"  {cid}: done {rec.url or rec.edit_url}{drift}")

    dc = [k for k in plan.discord_todo if not clip or k in clip]
    if "discord" in parts:
        console.print("\n[bold]Discord[/bold] (episodes only, clips channel webhook, posted as Wabi Sanji)")
        for k in dc:
            console.print(f"  post {k}:\n    " + publish.discord_content(plan, k).replace("\n", "\n    "))
        for k, why in plan.discord_waiting.items():
            if not clip or k in clip:
                console.print(f"  {k}: waits for {why}")
        for k in log.discord:
            console.print(f"  {k}: posted")

    if not go:
        console.print("\n[yellow]Dry run: nothing was created, recorded or posted.[/yellow] "
                      "Re-run with --go to do the steps above (add --clip <id> to try one clip first).")
        return

    # -- Do it -----------------------------------------------------------------------------
    publish.save_log(session_dir, log)  # URLs recorded above, VOD offset
    if "twitch" in parts and helix is not None:
        for c in tw:
            with console.status(f"Twitch: creating clip {c.id} …"):
                rec = publish.create_twitch_clip(plan, helix, c)
            publish.save_log(session_dir, log)
            warn = ""
            if rec.drift is not None and abs(rec.drift) > publish.DRIFT_WARN_SECONDS:
                warn = f" [yellow](starts {rec.drift:+.1f}s off: trim it at {rec.edit_url})[/yellow]"
            console.print(f"[green]✔[/green] Twitch clip {c.id}: {rec.url or rec.edit_url}"
                          + (f", drift {rec.drift:+.1f}s" if rec.drift is not None else ", offset not reported yet") + warn)
        for cid, rec in log.twitch_clips.items():  # fill in anything Twitch hadn't reported yet
            if rec.url is None or rec.actual_vod_offset is None:
                publish.refresh_twitch_clip(plan, helix, cid, wait=False)
        publish.save_log(session_dir, log)

    if "discord" in parts:
        plan = publish.build_plan(session_dir, session, clips_file, log, vod, vod_checked)
        todo = [k for k in plan.discord_todo if not clip or k in clip]
        if todo:
            webhook = publish.secrets.require("DISCORD_CLIPS_WEBHOOK")["DISCORD_CLIPS_WEBHOOK"]
            for k in todo:
                content = publish.discord_content(plan, k)
                msg = publish.post_discord(webhook, content)
                log.discord[k] = publish.DiscordRecord(message_id=str(msg["id"]), channel_id=str(msg["channel_id"]),
                                                       content=content, posted_at=publish._now())
                publish.save_log(session_dir, log)
                console.print(f"[green]✔[/green] Discord: posted {k}")
                time.sleep(1.0)

    if "youtube" in parts:
        publish.write_checklist(publish.build_plan(session_dir, session, clips_file, log, vod, vod_checked))
    final = publish.build_plan(session_dir, session, clips_file, log, vod, vod_checked)
    if publish.all_done(final) and session.status != "published":
        sessions.set_status(session_dir, "published")
        console.print(f"[green]✔[/green] {session.session}: status → published")
    console.print(f"publish log: {session_dir / publish.PUBLISH_YAML}")


# ---------------------------------------------------------------------------
# stream-vod-offset
# ---------------------------------------------------------------------------

def stream_vod_offset(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id (2026-10-05) or folder."),
) -> None:
    """Measure where Twitch VOD 0:00 falls in the recording by matching frames, and save it
    to publish.yaml. Needed before stream-publish creates Twitch clips (the VOD's created_at
    was 5 s off on 2026-10-05). Downloads a few low-quality VOD segments to a temp folder.
    """
    from . import publish, twitch, vodsync

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    rec = _recording(session)
    if not session.twitch_vod_id:
        console.print("[red]No twitch_vod_id in session.yaml.[/red]")
        raise typer.Exit(code=2)
    try:
        vod = twitch.Helix().video(session.twitch_vod_id)
    except (twitch.TwitchError, RuntimeError) as exc:
        console.print(f"[red]Twitch: {exc}[/red]")
        raise typer.Exit(code=2)
    if vod is None:
        console.print(f"[red]VOD {session.twitch_vod_id} is gone.[/red]")
        raise typer.Exit(code=2)
    rough = (twitch.vod_created_at(vod) - session.recording_start).total_seconds()
    # Sample where the clips are, then spread across the publishable segments.
    try:
        clip_times = [c.start + 3 for c in sessions.load_clips(session_dir).clips if c.approved and "twitch" in c.targets]
    except FileNotFoundError:
        clip_times = []
    spread = [s.start + f * s.duration for s in session.segments if s.publishable for f in (0.2, 0.5, 0.8)]
    samples = list(dict.fromkeys(round(t) for t in clip_times + spread))
    console.print(f"VOD {session.twitch_vod_id}: created_at suggests {rough:+.1f} s; measuring from frames …")
    try:
        result = vodsync.measure(rec, session.twitch_vod_id, samples, rough, say=console.print)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    log = publish.load_log(session_dir, session)
    log.twitch_vod_id = session.twitch_vod_id
    log.twitch_vod_start = result["offset"]
    log.twitch_vod_start_method = "frames"
    log.twitch_vod_start_samples = result["samples"]
    publish.save_log(session_dir, log)
    console.print(f"[green]✔[/green] VOD 0:00 = recording {result['offset']:+.1f} s "
                  f"(created_at was {rough - result['offset']:+.1f} s off) → {session_dir / publish.PUBLISH_YAML}")


# ---------------------------------------------------------------------------
# stream-archive
# ---------------------------------------------------------------------------

REGENERABLE = ("frames", "fine", "at", "audio", "shorts/_preview", "vods/seg*_thumbs/candidates")


def stream_archive(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id (2026-10-05) or folder."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would move and be removed."),
    to: Optional[Path] = typer.Option(None, "--to", help="Move the raw recording here instead of <session>/raw/."),
    force: bool = typer.Option(False, "--force", help="Archive even if the session isn't published."),
) -> None:
    """Tidy a published session: move the raw recording into the session folder (or --to) and
    remove regenerable intermediates (frames, fine, at, audio, previews, thumbnail candidates).
    Never deletes the recording, session.yaml, clips.yaml, publish.yaml, transcripts, Shorts or VODs.
    """
    import shutil

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    rec = Path(session.recording)
    if session.status != "published" and not force:
        msg = f"{session.session} is {session.status}, not published"
        if not dry_run:
            console.print(f"[red]{msg}. Use --force if {settings.streamer_name()} says to archive anyway.[/red]")
            raise typer.Exit(code=2)
        console.print(f"[yellow]{msg}: a real run would refuse without --force.[/yellow]")

    def size(p: Path) -> int:
        return p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file())

    dest_dir = to or (session_dir / "raw")
    moves = [(rec, dest_dir / rec.name)] if rec.exists() and rec.parent.resolve() != dest_dir.resolve() else []
    removals = [p for pat in REGENERABLE for p in session_dir.glob(pat) if p.exists()]
    freed = sum(size(p) for p in removals)
    console.print(f"[bold]{'Dry run: ' if dry_run else ''}archive {session.session}[/bold]")
    for src, dst in moves:
        console.print(f"  move   {src} → {dst} ({src.stat().st_size / 1e9:.1f} GB)")
    if not moves:
        console.print(f"  recording stays at {rec}" + ("" if rec.exists() else " [red](missing!)[/red]"))
    for p in removals:
        console.print(f"  remove {p.relative_to(session_dir)} ({size(p) / 1e6:.0f} MB)")
    console.print(f"  keeps  session.yaml, clips.yaml, publish.yaml, transcript/, signals/, sheets/, shorts/*.mp4, vods/")
    console.print(f"  frees  {freed / 1e9:.2f} GB in the session folder")
    if dry_run:
        return
    for src, dst in moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        sessions.set_recording(session_dir, dst)
    for p in removals:
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    sessions.set_status(session_dir, "archived")
    console.print(f"[green]✔[/green] archived; freed {freed / 1e9:.2f} GB")


# ---------------------------------------------------------------------------
# stream-inventory / stream-status
# ---------------------------------------------------------------------------

def stream_inventory(
    no_twitch: bool = typer.Option(False, "--no-twitch", help="Skip the Twitch VOD lookup (classify by size/length only)."),
) -> None:
    """Classify every recording in the recordings root (root + WabiSanji\\, nothing else) as stream,
    local_test or false_start, using Twitch VODs, size and length. Writes sessions/_backlog.yaml
    for the streamer to confirm. Read-only on the recordings.
    """
    from collections import Counter

    from . import backlog, twitch

    vods: list[dict] = []
    if not no_twitch:
        try:
            h = twitch.Helix()
            cursor = None
            while True:
                params = {"user_id": h.user_id, "first": 100, "type": "all"}
                if cursor:
                    params["after"] = cursor
                r = h._call("GET", "/videos", params=params)
                r.raise_for_status()
                body = r.json()
                vods += body.get("data", [])
                cursor = (body.get("pagination") or {}).get("cursor")
                if not cursor:
                    break
            console.print(f"Twitch: {len(vods)} videos ({sum(v['type'] == 'archive' for v in vods)} past broadcasts)")
        except Exception as exc:
            console.print(f"[yellow]Twitch lookup failed ({exc}); classifying by size and length only[/yellow]")
    root = sessions.sessions_root()
    items = backlog.inventory(sessions.recordings_root(), root, vods)
    path = root / backlog.BACKLOG_YAML
    items = backlog.merge_previous(items, path)
    backlog.write_backlog(path, items)

    table = Table(title=f"{len(items)} recordings")
    for col in ("start", "file", "len", "GB", "class", "VOD title / reason", "session"):
        table.add_column(col, overflow="fold")
    colour = {"stream": "green", "local_test": "yellow", "false_start": "dim", "in_session": "cyan", "excluded": "dim"}
    for it in items:
        start = (it["start"] or "")[:16].replace("T", " ")
        what = (it.get("vod") or {}).get("title") or it["reason"]
        if it.get("split_from"):
            what = f"[magenta]split from {it['split_from']}?[/magenta] {what}"
        if it.get("vocab_captures"):
            what += f"  [cyan]({', '.join(f'{k}: {v}' for k, v in it['vocab_captures'].items())} captures)[/cyan]"
        c = colour.get(it["class"], "white")
        table.add_row(start, Path(it["file"]).name, it["duration"], f"{it['size_gb']:.1f}",
                      f"[{c}]{it['class']}[/{c}]", what, it.get("session_id") or "")
    console.print(table)
    counts = Counter(it["class"] for it in items)
    console.print("  " + ", ".join(f"{k}: {v}" for k, v in counts.items()))
    console.print(f"[green]✔[/green] {path} (set confirmed: true per recording, or tell Claude it's all right)")


STREAMER = settings.streamer_name()  # who the human gates wait on, in stream-status
_WAITING = {
    "draft": (STREAMER, "approve segments (Gate 1)", "/stream-segment {s}"),
    "segments_approved": ("GPU", "transcribe", "stream-transcribe {s}"),
    "transcribed": ("Claude", "signals + moments", "/stream-moments {s}"),
    "moments_proposed": (STREAMER, "approve clips (Gate 2)", "edit clips.yaml"),
    "clips_approved": ("GPU", "render Shorts", "/stream-shorts {s}"),
    "rendered": ("Claude", "cut VODs + metadata", "/stream-vods {s}"),
    "packaged": ("publish", "upload / clips / Discord", "stream-publish {s}"),
    "published": (STREAMER, "archive when ready", "stream-archive {s} --dry-run"),
    "archived": ("-", "done", ""),
}


def stream_status(
    session_ref: Optional[str] = typer.Argument(None, metavar="[SESSION]", help="One session, or all."),
) -> None:
    """Board of stream sessions: status, publishable segments, clips, publishing and what each waits on."""
    import yaml as _yaml

    from . import backlog, publish, vods

    root = sessions.sessions_root()
    dirs = [_session_dir(session_ref)] if session_ref else sorted(p.parent for p in root.glob(f"*/{sessions.SESSION_YAML}"))
    if not dirs:
        console.print(f"No sessions yet in {root}.\nRecordings are read from {sessions.recordings_root()} "
                      "(set WS_RECORDINGS_ROOT / WS_SESSIONS_ROOT in .env). Start one with "
                      "`stream-init <recording>` or /stream-post.")
        return
    registry = vods.load_series()
    rows = []
    for d in dirs:
        try:
            s = sessions.load_session(d)
        except Exception as exc:
            rows.append((0, d.name, "[red]invalid[/red]", str(exc).splitlines()[0], "", "", STREAMER, "fix session.yaml", ""))
            continue
        games = ", ".join(f"{g.game}" for g in s.segments if g.publishable) or "-"
        clips_txt = "-"
        if (d / sessions.CLIPS_YAML).exists():
            c = sessions.load_clips(d)
            clips_txt = f"{sum(x.approved for x in c.clips)}/{len(c.clips)} ({c.status})"
        log = publish.load_log(d, s) if (d / publish.PUBLISH_YAML).exists() else None
        pub = "-" if not log else f"YT {len(log.youtube)}, Twitch {len(log.twitch_clips)}, Discord {len(log.discord)}"
        who, what, nxt = _WAITING.get(s.status, ("?", "?", ""))
        if s.status == "packaged":
            open_series = sorted({g.series for g in s.segments if g.publishable and
                                  registry.get(g.series or "", {}).get("numbering") != "locked"})
            if open_series:
                what = f"episodes wait for numbering ({', '.join(open_series)})"
                who, nxt = "backlog", "/stream-backlog"
        order = {STREAMER: 0, "GPU": 1, "Claude": 2, "publish": 3, "backlog": 4, "-": 5}.get(who, 6)
        rows.append((order, s.session, s.status, games, clips_txt, pub, who, what, nxt.format(s=s.session)))
    table = Table(title="Stream sessions")
    for col in ("session", "status", "publishable", "clips", "published", "waiting on", "next"):
        table.add_column(col, overflow="fold")
    for _, sid, status, games, clips_txt, pub, who, what, nxt in sorted(rows):
        table.add_row(sid, status, games, clips_txt, pub, f"{who}: {what}", nxt)
    console.print(table)
    bl = root / backlog.BACKLOG_YAML
    if bl.exists() and not session_ref:
        recs = (_yaml.safe_load(bl.read_text(encoding="utf-8")) or {}).get("recordings", [])
        streams = [r for r in recs if r["class"] == "stream"
                   and not (root / str(r.get("session_id") or "")).joinpath(sessions.SESSION_YAML).exists()]
        unconfirmed = sum(not r.get("confirmed") for r in recs if r["class"] not in ("in_session", "excluded"))
        console.print(f"Backlog: {len(streams)} streamed recordings without a session, "
                      f"{unconfirmed} classification(s) waiting on {STREAMER} → {bl}")


# ---------------------------------------------------------------------------
# stream-vod-tail
# ---------------------------------------------------------------------------

def stream_vod_tail(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id or folder."),
    go: bool = typer.Option(False, "--go", help="Download and join (GPU encode). Without it: show the plan."),
) -> None:
    """When the OBS recording stopped before the stream ended, download the rest of the
    Twitch VOD (source quality) and join it on, as <session>/source/<recording> + VOD tail.mp4.
    session.yaml then points at the joined file; the original recording is not touched.
    Needs `stream-vod-offset` first. NVENC encode: not while OBS is live.
    """
    import re as _re

    from . import publish, twitch, vodsync

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    rec = _recording(session)
    log = publish.load_log(session_dir, session)
    if log.twitch_vod_start_method not in publish.TRUSTED_OFFSET:
        console.print(f"[red]Run stream-vod-offset {session.session} first.[/red]")
        raise typer.Exit(code=2)
    vod = twitch.Helix().video(session.twitch_vod_id) if session.twitch_vod_id else None
    if vod is None:
        console.print("[red]VOD not available.[/red]")
        raise typer.Exit(code=2)
    from .backlog import parse_duration

    rec_dur = media.probe_duration(rec)
    vod_dur = parse_duration(vod["duration"])
    tail_from = rec_dur - log.twitch_vod_start  # VOD time where the recording ends
    missing = vod_dur - tail_from
    console.print(f"recording {fmt_short(rec_dur)}, VOD {fmt_short(vod_dur)}, VOD 0:00 = recording {log.twitch_vod_start:+.1f} s")
    if missing < 60:
        console.print("Nothing missing (under a minute).")
        return
    out = session_dir / "source" / f"{rec.stem} + VOD tail.mp4"
    console.print(f"missing: VOD {fmt_tc(tail_from)}-{fmt_tc(vod_dur)} ({fmt_short(missing)}) → {out}")
    if not go:
        console.print("[yellow]Dry run.[/yellow] Re-run with --go to download and join (NVENC re-encode).")
        return
    tail_ts = session_dir / "source" / "vod_tail.ts"
    t0 = time.monotonic()
    tail_ts, tail_vod_start = vodsync.download_vod_range(session.twitch_vod_id, tail_from, None, tail_ts, say=console.print)
    console.print(f"  downloaded in {time.monotonic() - t0:.0f}s; joining on NVENC …")
    t1 = time.monotonic()
    vodsync.join_with_tail(rec, tail_ts, tail_from - tail_vod_start, out)
    tail_ts.unlink(missing_ok=True)
    new_dur = media.probe_duration(out)
    console.print(f"[green]✔[/green] joined {fmt_short(new_dur)} in {time.monotonic() - t1:.0f}s → {out}")

    # Point the session at the joined file; extend whatever segment ran to the old end.
    old_end = session.duration
    path = session_dir / sessions.SESSION_YAML
    text = path.read_bytes().decode("utf-8")
    text = _re.sub(r'^(duration:[ \t]*)"?[0-9:.]+"?', lambda m: f'{m.group(1)}"{fmt_tc(round(new_dur))}"', text, count=1, flags=_re.M)
    last = max(session.segments, key=lambda s: s.end, default=None)
    if last is not None and abs(last.end - old_end) < 1.5:
        text = _re.sub(rf'^(\s*end:[ \t]*)"{_re.escape(fmt_tc(last.end))}"', lambda m: f'{m.group(1)}"{fmt_tc(round(new_dur))}"',
                       text, count=1, flags=_re.M)
        console.print(f"  segment {last.id} ({last.game or last.label}) now runs to {fmt_tc(round(new_dur))}: "
                      f"check the stream's ending with stream-frames/stream-sheets and add an ending skip")
    from .models import Session as _S
    import yaml as _yaml
    _S.model_validate(_yaml.safe_load(text))
    path.write_bytes(text.encode("utf-8"))
    sessions.set_recording(session_dir, out)
    console.print(f"[green]✔[/green] session.yaml now uses {out.name}")


def stream_youtube_md(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id or folder."),
    segment: Optional[list[int]] = typer.Option(None, "--segment", "-s", help="Segment id (repeatable). Default: every segment with a meta.json."),
) -> None:
    """Build vods/seg<N>.youtube.md (title with episode number, description with links, chapters
    mapped through cuts and the Japanese word list, tags, thumbnail picks) from vods/seg<N>.meta.json,
    the copy Claude writes from the transcript (/stream-vods)."""
    from . import vods

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    ids = segment or [s.id for s in session.segments if (session_dir / "vods" / f"seg{s.id}.meta.json").exists()]
    if not ids:
        console.print("[yellow]No vods/seg<N>.meta.json yet.[/yellow]")
        raise typer.Exit(code=1)
    for i in ids:
        try:
            out = vods.build_youtube_md(session, session.segment(i), session_dir)
        except (RuntimeError, ValueError, KeyError, FileNotFoundError) as exc:
            console.print(f"[red]segment {i}: {exc}[/red]")
            continue
        console.print(f"[green]✔[/green] {out}")


def stream_series_checklist(
    series: str = typer.Argument(..., help='Series name as in series.yaml, e.g. "Alchemy Factory".'),
) -> None:
    """One upload checklist for a whole series, in episode order, from each episode's
    vods/seg<N>.youtube.md: sessions/_upload_<series>.md. Episodes already recorded in
    publish.yaml are marked uploaded; ones not ready yet say what's missing."""
    import re as _re

    from . import publish, vods

    registry = vods.load_series()
    if series not in registry:
        console.print(f"[red]No series {series!r} in series.yaml ({', '.join(registry)}).[/red]")
        raise typer.Exit(code=2)
    ser = registry[series]
    root = sessions.sessions_root()
    out = root / f"_upload_{vods.slugify(series)}.md"
    lines = [f"# Upload checklist: {series}", "",
             f"Numbering: {ser.get('numbering')}. Upload in this order in YouTube Studio as Private, then set public or "
             f"scheduled. Record each URL with `stream-publish <session> --youtube seg<N>=<url> --go`, or ask Claude to "
             f"pull them from the playlist.", ""]
    ready = done = 0
    for ep in ser.get("episodes") or []:
        sid, seg_id, n = str(ep["session"]), int(ep["segment"]), ep["episode"]
        d = root / sid
        session = sessions.load_session(d)
        log = publish.load_log(d, session) if (d / publish.PUBLISH_YAML).exists() else None
        rec = log.youtube.get(f"seg{seg_id}") if log else None
        page = d / "vods" / f"seg{seg_id}.youtube.md"
        head = f"## #{n} · {sid} segment {seg_id}"
        if rec:
            lines += [head + "  ✓ uploaded", "", f"- [x] {rec.url}", ""]
            done += 1
            continue
        if not page.exists():
            missing = [x for x, ok in (("episode file (stream-cut-vods)", vods.cut_info(d, seg_id)),
                                       ("description (/stream-vods meta.json)", (d / "vods" / f"seg{seg_id}.meta.json").exists()))
                       if not ok] or ["youtube.md (stream-youtube-md)"]
            lines += [head + "  (not ready)", "", f"Missing: {', '.join(missing)}.", ""]
            continue
        body = page.read_text(encoding="utf-8").split("\n", 1)[1]  # drop the page's own heading
        body = _re.sub(r"^## ", "### ", body, flags=_re.M)
        body = body.replace("`vods/", f"`{d / 'vods'}\\")  # full paths: the checklist lives outside the session
        lines += [head, "", "- [ ] uploaded", body.strip(), ""]
        ready += 1
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    console.print(f"[green]✔[/green] {out}: {done} uploaded, {ready} ready, "
                  f"{len(ser.get('episodes') or []) - done - ready} not ready")


# ---------------------------------------------------------------------------
# stream-metrics
# ---------------------------------------------------------------------------

def stream_metrics(
    twitch_csv: Optional[list[Path]] = typer.Option(None, "--twitch-csv", help="Twitch Analytics export(s), "
                                                    "repeatable. Default: every .csv in sessions/_metrics/."),
    public_only: bool = typer.Option(False, "--public-only", help="Skip YouTube Analytics and Instagram "
                                     "insights: public counts only."),
) -> None:
    """Pull YouTube, Instagram and Twitch numbers for every published post and write
    sessions/_metrics_report.md: what performs where, by content type, game and platform, plus YouTube
    thumbnail impressions and CTR (before and after each thumbnail change). Read-only on every platform,
    except that the first run sets up a YouTube Reporting API job for the daily reach reports."""
    import json

    from . import metrics

    root = sessions.sessions_root()
    now = dt.datetime.now(dt.timezone.utc)
    posts, notes = metrics.collect_posts(root)
    console.print(f"{len(posts)} published posts in {len({p.session for p in posts})} sessions")
    metrics.fetch_youtube(posts, notes, analytics=not public_only)
    if not public_only:
        metrics.fetch_reach(posts, metrics.load_reach(metrics.sync_reach(root, notes)))
    metrics.fetch_instagram(posts, notes, insights=not public_only)
    metrics.fetch_twitch(posts, notes)
    from . import viewers

    twitch_days: dict = {}
    csv_paths = list(twitch_csv or []) or metrics.twitch_csvs(root)
    if csv_paths:
        twitch_days, csv_notes = metrics.merge_twitch_csvs(csv_paths)
        notes += [f"Twitch streams from {', '.join(p.name for p in csv_paths)}."] + csv_notes
    follows = metrics.fetch_twitch_follows(notes)
    days = metrics.stream_days(root)
    if follows:
        metrics.add_follows(twitch_days, follows)
        first = min(follows)
        for day in days:  # the followers list covers every day: a stream day with no follows had 0
            if day >= first:
                twitch_days.setdefault(day, {}).setdefault("follows", 0.0)
        notes.append("Twitch follows per day from the followers list (people who unfollowed since aren't counted).")
    from . import recorder

    dates = [d for ss in days.values() for s in ss
             for d in recorder.utc_dates(s.recording_start, s.recording_start + dt.timedelta(seconds=s.duration))]
    try:
        n = recorder.pull_viewers(root / viewers.VIEWERS_DIR, dates)
        if n:
            notes.append(f"{n} viewer samples from the chat recorder.")
    except recorder.RecorderError as exc:
        notes.append(f"Recorder viewer log skipped: {exc}")
    samples = viewers.load_samples(root)
    viewer_rows = viewers.segment_viewers([s for ss in days.values() for s in ss], samples) if samples else None
    if not samples:
        notes.append("No live viewer log yet: run `stream-viewers` during streams for viewers per game.")
    chat_rows = []
    for ss in days.values():
        for s in ss:
            p = root / s.session / "signals" / "chat.json"
            data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            if data.get("status") == "ok":
                hours = {g.id: g.duration / 3600 for g in s.segments}
                chat_rows += [{**r, "session": s.session, "hours": hours.get(r["segment"], 0.0)}
                              for r in data["per_segment"]]
    snap = metrics.save_snapshot(root, posts, notes, now)
    out = root / metrics.REPORT_MD
    out.write_text(metrics.build_report(posts, notes, now=now, twitch_days=twitch_days or None,
                                        days_sessions=days, viewer_rows=viewer_rows, chat_rows=chat_rows or None),
                   encoding="utf-8")
    for n in notes:
        console.print(f"  [yellow]{n}[/yellow]")
    live = sum(1 for p in posts if p.public)
    console.print(f"[green]✔[/green] {out} ({live} live posts; raw numbers in {snap.name})")


# ---------------------------------------------------------------------------
# stream-viewers
# ---------------------------------------------------------------------------

def stream_viewers(
    poll: float = typer.Option(60.0, "--poll", help="Seconds between samples."),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for the stream to start (default) or stop "
                              "at once if offline."),
    forever: bool = typer.Option(False, "--forever", help="Keep running across streams (for a startup task)."),
) -> None:
    """Log live viewers once a minute to sessions/_viewers/, for viewers per game in stream-metrics.
    One Twitch API call a minute, no GPU: safe to run while OBS is live."""
    from . import twitch, viewers

    try:
        helix = twitch.Helix()
    except (twitch.TwitchError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    console.print(f"Watching twitch.tv/{helix.login} every {poll:.0f} s" + (" (forever)" if forever else ""))
    written = viewers.record(sessions.sessions_root(), helix, poll=poll, wait=wait, forever=forever,
                             say=console.print)
    for p in written:
        console.print(f"[green]✔[/green] {p}")


# stream-memes

def stream_memes(
    session_ref: str = typer.Argument(..., metavar="SESSION", help="Session id (2026-10-05) or folder."),
    segment: Optional[list[int]] = typer.Option(None, "--segment", "-s", help="Segment id (repeatable)."),
    library: bool = typer.Option(False, "--library", help="Print the meme library compactly (for picking)."),
) -> None:
    """Episode memes: the library, ARIA's live memes in this recording, and which picks in
    memes.yaml pass the rules (gap, cap, cuts, family). Nothing is rendered here.
    """
    from . import memes

    session_dir = _session_dir(session_ref)
    session = _load(session_dir)
    lib = memes.load_library()
    if library:
        for m in lib.values():
            flags = ", ".join(x for x in (m.kind, m.category, f"{m.risk} risk", "" if m.family else "NOT family") if x)
            cap = f' "{m.caption}"' if m.caption else ""
            console.print(f"{m.id} ({flags}){cap}: {m.about}", highlight=False, soft_wrap=True)
        console.print(f"\n{len(lib)} memes in {memes.LIBRARY_YAML}")
        return
    live = memes.live_memes(session)
    has_file = (session_dir / memes.SESSION_MEMES).exists()
    if not has_file:
        console.print(f"[yellow]No {memes.SESSION_MEMES} yet: run /stream-memes to pick them.[/yellow]")
    for seg in _publishable(session, segment):
        seg_live = [(t, mid) for t, mid, _ in live if seg.contains(t)]
        cap = max(1, int(seg.duration // memes.SECONDS_PER_MEME))
        kids = " [yellow]family is in this segment: family-only memes[/yellow]" if memes.kids_in(seg, session_dir) else ""
        console.print(f"[bold]segment {seg.id}[/bold] {seg.series or seg.type} ({fmt_short(seg.duration)}, "
                      f"cap {cap}){kids}")
        for t, mid in seg_live:
            console.print(f"  live   {fmt_tc(t)}  {mid}")
        if has_file:
            kept, dropped = memes.plan(session, seg, session_dir, library=lib, live=live)
            for p in kept:
                console.print(f"  [green]add[/green]    {fmt_tc(p.insert.at)}  {p.meme.id} ({p.duration:.1f}s, "
                              f"{p.meme.risk} risk)  {p.insert.line}", highlight=False)
            for ins, why in dropped:
                console.print(f"  [red]drop[/red]   {fmt_tc(ins.at)}  {ins.id}: {why}", highlight=False)


def register(app: typer.Typer) -> None:
    app.command(name="stream-memes")(stream_memes)
    app.command(name="stream-viewers")(stream_viewers)
    app.command(name="stream-metrics")(stream_metrics)
    app.command(name="stream-series-checklist")(stream_series_checklist)
    app.command(name="stream-youtube-md")(stream_youtube_md)
    app.command(name="stream-vod-tail")(stream_vod_tail)
    app.command(name="stream-inventory")(stream_inventory)
    app.command(name="stream-status")(stream_status)
    app.command(name="stream-auth")(stream_auth)
    app.command(name="stream-youtube-sync")(stream_youtube_sync)
    app.command(name="stream-playlist")(stream_playlist)
    app.command(name="stream-upload")(stream_upload)
    app.command(name="stream-describe")(stream_describe)
    app.command(name="stream-social")(stream_social)
    app.command(name="stream-schedule-sync")(stream_schedule_sync)
    app.command(name="stream-number")(stream_number)
    app.command(name="stream-publish")(stream_publish)
    app.command(name="stream-vod-offset")(stream_vod_offset)
    app.command(name="stream-archive")(stream_archive)
    app.command(name="stream-cut-vods")(stream_cut_vods)
    app.command(name="stream-thumbs")(stream_thumbs)
    app.command(name="stream-thumbnail")(stream_thumbnail)
    app.command(name="stream-thumbnail-push")(stream_thumbnail_push)
    app.command(name="stream-render-shorts")(stream_render_shorts)
    app.command(name="stream-signals")(stream_signals)
    app.command(name="stream-init")(stream_init)
    app.command(name="stream-frames")(stream_frames)
    app.command(name="stream-sheets")(stream_sheets)
    app.command(name="stream-validate")(stream_validate)
    app.command(name="stream-transcribe")(stream_transcribe)


def main() -> None:
    """Console entry point: ``wabisanji-post stream-<cmd> ...`` (or ``python pipeline.py ...``)."""
    import sys

    # Windows' default cp1252 can't print the arrows and ticks rich uses.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    app = typer.Typer(no_args_is_help=True, add_completion=False,
                      help="Wabi Sanji post-stream pipeline: recordings → episodes, Shorts, clips.")
    register(app)
    app()


if __name__ == "__main__":
    main()
