"""Segment VODs (spec: "Segment VODs"): one YouTube episode file per publishable segment.

Cuts are stream copies, so they start on a keyframe. We find the keyframe at
or before the segment start ourselves and cut exactly there, then record it
in ``vods/seg<N>.cut.json`` so chapter times can be made relative to the real
start of the file rather than the requested one (OBS keyframes are ~2 s apart).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

import yaml

from . import settings
from .media import _run_ffmpeg, extract_frame, probe_duration
from .media import to_source
from .memes import SESSION_MEMES
from .memes import plan as meme_plan
from .models import Segment, Session, fmt_short, fmt_tc


SERIES_YAML = settings.content_root() / "stream" / "series.yaml"


# ---------------------------------------------------------------------------
# Series registry
# ---------------------------------------------------------------------------

def load_series(path: Path = SERIES_YAML) -> dict[str, dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {s["name"]: s for s in raw.get("series", [])}


def episode_label(series: dict, session_id: str, segment_id: int) -> str:
    """``#7`` once the series is ``numbering: locked``, otherwise ``#?`` (don't publish)."""
    if series.get("numbering") != "locked":
        return "#?"
    for ep in series.get("episodes") or []:
        if str(ep.get("session")) == session_id and int(ep.get("segment", -1)) == segment_id:
            return f"#{ep['episode']}"
    return "#?"


# ---------------------------------------------------------------------------
# Cutting
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def vod_path(session_dir: Path, seg: Segment) -> Path:
    return session_dir / "vods" / f"seg{seg.id}_{slugify(seg.series or seg.game or seg.type)}.mp4"


def _keyframes(rec: Path, a: float, b: float) -> list[float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
         "-read_intervals", f"{max(0.0, a):.3f}%{b:.3f}",
         "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(rec)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe keyframes failed: {out.stderr[-300:]}")
    times = []
    for line in out.stdout.split():  # some containers add empty csv fields: "0.000000,"
        field = line.split(",")[0].strip()
        if field and field != "N/A":
            times.append(float(field))
    return times


def keyframe_at_or_before(rec: Path, t: float) -> float:
    before = [k for k in _keyframes(rec, t - 15, t + 0.001) if k <= t + 0.001]
    if not before and t < 1.0:
        # Files whose first frame isn't at 0 (downloaded VODs start at 0.048 s): use the first keyframe.
        first = _keyframes(rec, 0.0, 2.0)
        if first:
            return min(first)
    if not before:
        raise RuntimeError(f"no keyframe found before {fmt_tc(t)}")
    return max(before)


def keyframe_at_or_after(rec: Path, t: float) -> float:
    after = [k for k in _keyframes(rec, t - 0.001, t + 15) if k >= t - 0.001]
    if not after:
        raise RuntimeError(f"no keyframe found after {fmt_tc(t)}")
    return min(after)


def next_keyframe(rec: Path, kf: float) -> float:
    after = [k for k in _keyframes(rec, kf, kf + 15) if k > kf + 0.001]
    return min(after) if after else kf + 1.0


def _copy_range(rec: Path, start: float, end: float, audio_track: int, target: Path) -> None:
    """Stream-copy from keyframe ``start`` to ``end``.

    With stream copy, ffmpeg's input seek lands on the keyframe *before* the
    target when the target is the keyframe itself (checked 2026-10-05 on MKV
    and MP4: a seek to 40.0 started at 38.0). Seeking to the middle of the
    keyframe interval lands exactly on ``start``; the output then runs from
    ``start``, so the length is measured from the seek point.
    """
    seek = (start + next_keyframe(rec, start)) / 2
    _run_ffmpeg(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{seek:.6f}", "-i", str(rec), "-t", f"{end - seek:.3f}",
         "-map", "0:v:0", "-map", f"0:a:{audio_track}", "-c", "copy",
         "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(target)],
        f"cut {fmt_tc(start)}-{fmt_tc(end)}",
    )


# ---------------------------------------------------------------------------
# Banners (a notice across the top for a stretch, e.g. the mic was muted)
# ---------------------------------------------------------------------------

def _video_props(src: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate,bit_rate,color_range,color_space,color_transfer,color_primaries",
         "-of", "json", str(src)], capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {src}: {out.stderr[-300:]}")
    return json.loads(out.stdout)["streams"][0]


def _audio_props(src: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                          "stream=bit_rate,sample_rate", "-of", "json", str(src)], capture_output=True, text=True)
    streams = json.loads(out.stdout or "{}").get("streams") or [{}]
    return streams[0]


def banner_png(text: str, video_height: int, target: Path) -> Path:
    """The banner image: cream Noto Sans JP Bold on an indigo box with an amber edge (the Shorts palette)."""
    from PIL import Image, ImageDraw, ImageFont

    from .render import ensure_fonts

    s = video_height / 1080
    font = ImageFont.truetype(str(ensure_fonts() / "NotoSansJP-Bold.otf"), max(10, round(34 * s)))
    pad_x, pad_y, edge = round(26 * s), round(12 * s), max(1, round(3 * s))
    left, top, right, bottom = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), text, font=font)
    w, h = right - left + 2 * pad_x, bottom - top + 2 * pad_y
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=round(10 * s), fill=(0x2B, 0x2F, 0x55, 225),
                        outline=(0xE8, 0xA0, 0x4A, 255), width=edge)
    d.text((pad_x - left, pad_y - top), text, font=font, fill=(0xF2, 0xEC, 0xDC, 255))
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target)
    return target


BANNER_BITRATE_FACTOR = 1.3  # of the source video bitrate: enough to survive a second generation


def blur_radius(w: int, h: int) -> int:
    """boxblur radius for a ``w`` x ``h`` box: strong, but within the half-size chroma planes' limit."""
    return max(1, min(24, min(w, h) // 4))


def _has_audio(path: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(out.stdout.strip())


def meme_filters(memes: list, first_input: int, height: int, work: Path,
                 last: str) -> tuple[list[str], list[str], str, list[tuple[float, float, str]]]:
    """ffmpeg inputs and filter steps that pop each meme over the picture (on top of everything else).

    ``memes`` are ``memes.Placed`` with ``file_at`` (file time) set. Returns the extra inputs, the
    video filter steps, the last video label and ``(start, end, audio label)`` for each meme with sound.
    """
    from .memes import caption_png

    s = height / 1080
    box_w, box_h, edge = round(560 * s), round(460 * s), max(2, round(4 * s))
    inputs: list[str] = []
    chain: list[str] = []
    sounds: list[tuple[float, float, str]] = []
    idx = first_input
    for i, p in enumerate(memes, start=1):
        a, dur, kind = p.file_at, p.duration, p.meme.kind
        b = a + dur
        picture = None
        if kind == "clip":
            inputs += ["-i", str(p.path)]
            picture = (f"[{idx}:v]trim=duration={dur:.3f},scale={box_w}:{box_h}:force_original_aspect_ratio=decrease,"
                       f"pad=iw+{2 * edge}:ih+{2 * edge}:{edge}:{edge}:color=0xE8A04A")
            if _has_audio(p.path):
                sounds.append((a, b, f"{idx}:a"))
            idx += 1
        elif kind == "image":
            loop = ["-ignore_loop", "0"] if p.path.suffix.lower() == ".gif" else ["-loop", "1"]
            inputs += [*loop, "-t", f"{dur:.3f}", "-i", str(p.path)]
            picture = (f"[{idx}:v]scale={box_w}:{box_h}:force_original_aspect_ratio=decrease,"
                       f"pad=iw+{2 * edge}:ih+{2 * edge}:{edge}:{edge}:color=0xE8A04A")
            idx += 1
        else:  # sound: the caption pill, if it has a line
            if p.meme.caption:
                png = caption_png(p.meme.caption, height, work / f"meme{i}.png")
                inputs += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(png)]
                picture = f"[{idx}:v]null"
                idx += 1
            inputs += ["-i", str(p.path)]
            sounds.append((a, b, f"{idx}:a"))
            idx += 1
        if picture:
            fade_out = max(0.0, dur - 0.3)
            chain += [f"{picture},format=rgba,fade=t=in:st=0:d=0.15:alpha=1,"
                      f"fade=t=out:st={fade_out:.3f}:d=0.3:alpha=1,setpts=PTS-STARTPTS+{a:.3f}/TB[mp{i}]",
                      f"[{last}][mp{i}]overlay=x=(main_w-overlay_w)/2:y=main_h*0.42-overlay_h/2:"
                      f"eof_action=pass:enable='between(t,{a:.3f},{b:.3f})'[mv{i}]"]
            last = f"mv{i}"
    return inputs, chain, last, sounds


def meme_audio(sounds: list[tuple[float, float, str]]) -> list[str]:
    """Duck the episode audio under each meme's sound and mix the sounds in: ``[aout]``."""
    duck = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b, _ in sounds)
    chain = [f"[0:a]volume='if({duck},{MEME_DUCK},1)':eval=frame[duck]"]
    for i, (a, b, label) in enumerate(sounds, start=1):
        ms = round(a * 1000)
        chain.append(f"[{label}]atrim=duration={b - a:.3f},aresample=48000,aformat=channel_layouts=stereo,"
                     f"adelay={ms}:all=1[ma{i}]")
    mix = "".join(f"[ma{i}]" for i in range(1, len(sounds) + 1))
    chain.append(f"[duck]{mix}amix=inputs={len(sounds) + 1}:normalize=0:duration=first[aout]")
    return chain


MEME_DUCK = 0.35  # episode audio volume while a meme plays its sound


def burn_banners(path: Path, windows: list[tuple[float, float, str]], work: Path, *,
                 blurs: list[tuple[float, float, tuple[int, int, int, int]]] = (),
                 memes: list = (),
                 encoder: str = "h264_nvenc", hwaccel: bool = True) -> None:
    """Re-encode the episode file once with each banner over its ``(start, end, text)`` window (file time).

    ``blurs`` are ``(start, end, (x, y, w, h))`` boxes blurred out for their window (file time,
    1920x1080 reference pixels, scaled to the file's size), drawn before the banners.

    ``memes`` (``memes.Placed`` with ``file_at``) pop over everything, centred a little above
    the middle like ARIA's live layer. Their sound is mixed in with the episode audio ducked
    underneath, so with memes the audio is re-encoded instead of copied.

    Re-encoding only the bannered stretch and joining it to stream copies overlapped frames at the
    joins (copied pieces lead with B-frame delay and run a few frames past their end keyframe;
    tried 2026-10-10), so the whole file is encoded. The audio is copied untouched and the
    timeline stays the same, so chapters and cut.json still hold.

    The video is encoded at about 1.3x the source bitrate. CQ 19 (the first version) came out
    at 16 Mbps against OBS's 4.3 Mbps on 2026-10-10: a 17 GB episode for no visible gain,
    since YouTube re-encodes anyway. Decoding runs on the GPU when it can (``hwaccel``).
    """
    props = _video_props(path)
    height = int(props.get("height", 1080))
    size = (int(props.get("width", round(height * 16 / 9))), height)
    fps = props.get("r_frame_rate", "60/1")
    num, _, den = fps.partition("/")
    gop = max(1, round(2 * float(num) / float(den or 1)))  # a keyframe every 2 s, like OBS
    color = []
    for key, opt in (("color_range", "-color_range"), ("color_space", "-colorspace"),
                     ("color_transfer", "-color_trc"), ("color_primaries", "-color_primaries")):
        if props.get(key) and props[key] != "unknown":
            color += [opt, props[key]]
    src_rate = int(props.get("bit_rate") or 0) or 6_000_000
    rate = max(2_000_000, round(src_rate * BANNER_BITRATE_FACTOR))
    if encoder == "h264_nvenc":
        venc = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-multipass", "qres",
                "-b:v", str(rate), "-maxrate", str(2 * rate), "-bufsize", str(4 * rate), "-profile:v", "high"]
    else:
        venc = ["-c:v", encoder, "-preset", "fast", "-b:v", str(rate), "-maxrate", str(2 * rate),
                "-bufsize", str(4 * rate), "-profile:v", "high"]
    hw = ["-hwaccel", "cuda"] if encoder == "h264_nvenc" and hwaccel else []
    work.mkdir(parents=True, exist_ok=True)
    inputs, chain, last = [], [], "0:v"
    if blurs:  # CUDA decode hands over nv12; boxblur on its interleaved chroma turned half the box green (2026-10-18)
        chain.append("[0:v]format=yuv420p[f0]")
        last = "f0"
    for i, (a, b, box) in enumerate(blurs, start=1):
        bx, by, bw, bh = to_source(box, size)
        chain += [f"[{last}]split[m{i}][s{i}]",
                  f"[s{i}]crop={bw}:{bh}:{bx}:{by},boxblur={blur_radius(bw, bh)}:3[bl{i}]",
                  f"[m{i}][bl{i}]overlay=x={bx}:y={by}:enable='between(t,{a:.3f},{b:.3f})'[u{i}]"]
        last = f"u{i}"
    y = round(24 * height / 1080)
    for i, (a, b, text) in enumerate(windows, start=1):
        inputs += ["-loop", "1", "-i", str(banner_png(text, height, work / f"banner{i}.png"))]
        out = f"v{i}"
        chain.append(f"[{last}][{i}:v]overlay=x=(main_w-overlay_w)/2:y={y}:shortest=1:"
                     f"enable='between(t,{a:.3f},{b:.3f})'[{out}]")
        last = out
    audio_map, aenc = ["-map", "0:a"], ["-c:a", "copy"]
    if memes:
        m_inputs, m_chain, last, sounds = meme_filters(memes, 1 + len(windows), height, work, last)
        inputs += m_inputs
        chain += m_chain
        if sounds:
            chain += meme_audio(sounds)
            a_rate = int(_audio_props(path).get("bit_rate") or 0) or 192_000
            audio_map, aenc = ["-map", "[aout]"], ["-c:a", "aac", "-b:a", str(max(160_000, a_rate))]
    chain.append(f"[{last}]format=yuv420p[vout]")
    tmp = path.with_name(path.stem + ".banner.mp4")
    try:
        _run_ffmpeg(["ffmpeg", "-v", "error", "-y", *hw, "-i", str(path), *inputs, "-filter_complex", ";".join(chain),
                     "-map", "[vout]", *audio_map, *venc, "-g", str(gop), *color, *aenc,
                     "-movflags", "+faststart", str(tmp)], f"banners on {path.name}")
    except RuntimeError:
        tmp.unlink(missing_ok=True)
        if encoder == "libx264":
            raise
        if hw:  # GPU decode failed: same encoder, CPU decode
            return burn_banners(path, windows, work, blurs=blurs, memes=memes, encoder=encoder, hwaccel=False)
        return burn_banners(path, windows, work, blurs=blurs, memes=memes, encoder="libx264")
    if abs(probe_duration(tmp) - probe_duration(path)) > 0.5:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"banner pass changed the length of {path.name}")
    tmp.replace(path)


def cut_segment(session: Session, seg: Segment, session_dir: Path, *,
                encoder: str = "h264_nvenc") -> dict:
    """Stream-copy ``seg`` (video + ``audio_track``) to vods/, starting on keyframes.

    A segment with cuts is copied part by part and the parts are joined with the
    concat demuxer (no re-encode). The first part starts on the keyframe at or
    before its start; later parts on the keyframe at or after theirs, so nothing
    of a cut stretch (a BRB card) leaks in. ``parts`` in the cut info maps each
    kept recording range to its position in the file, for chapters.

    Banners and blurs are then burned in with one re-encode of the file (``burn_banners``).
    """
    import shutil

    rec = Path(session.recording)
    target = vod_path(session_dir, seg)
    target.parent.mkdir(parents=True, exist_ok=True)
    ranges = seg.parts
    parts_info: list[dict] = []
    if len(ranges) == 1:
        a, b = ranges[0]
        kf = keyframe_at_or_before(rec, a)
        _copy_range(rec, kf, b, session.audio_track, target)
        parts_info.append({"rec_start": round(kf, 3), "rec_end": b, "file_start": 0.0})
    else:
        work = session_dir / "vods" / f"_parts_seg{seg.id}"
        work.mkdir(parents=True, exist_ok=True)
        files, offset = [], 0.0
        for i, (a, b) in enumerate(ranges):
            kf = keyframe_at_or_before(rec, a) if i == 0 else keyframe_at_or_after(rec, a)
            if kf >= b:
                continue
            f = work / f"p{i}.mp4"
            _copy_range(rec, kf, b, session.audio_track, f)
            parts_info.append({"rec_start": round(kf, 3), "rec_end": b, "file_start": round(offset, 3)})
            offset += probe_duration(f)
            files.append(f)
        listing = work / "concat.txt"
        listing.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), encoding="utf-8")
        _run_ffmpeg(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                     "-c", "copy", "-movflags", "+faststart", str(target)], f"join segment {seg.id}")
        shutil.rmtree(work, ignore_errors=True)
    info = {
        "segment": seg.id,
        "file": target.name,
        "requested_start": seg.start,
        "start": parts_info[0]["rec_start"],  # recording offset of 00:00 in the file
        "end": seg.end,
        "parts": parts_info,
        "cuts": [{"start": c.start, "end": c.end, "reason": c.reason} for c in seg.cuts],
        "duration": round(probe_duration(target), 2),
    }
    placed = []
    if (session_dir / SESSION_MEMES).exists():
        placed, dropped = meme_plan(session, seg, session_dir)
        for p in placed:
            p.file_at = file_time(info, p.insert.at)
        if placed or dropped:
            info["memes"] = [{"id": p.meme.id, "rec_at": p.insert.at, "file_at": round(p.file_at, 3),
                              "duration": round(p.duration, 2), "risk": p.meme.risk, "line": p.insert.line}
                             for p in placed]
            info["memes_dropped"] = [{"id": i.id, "rec_at": i.at, "why": why} for i, why in dropped]
    if seg.banners or seg.blurs or placed:
        windows = [(file_time(info, bn.start), file_time(info, bn.end - 0.001), bn.text) for bn in seg.banners]
        blurs = [(file_time(info, bl.start), file_time(info, bl.end - 0.001), bl.box) for bl in seg.blurs]
        work = session_dir / "vods" / f"_banners_seg{seg.id}"
        burn_banners(target, windows, work, blurs=blurs, memes=placed, encoder=encoder)
        shutil.rmtree(work, ignore_errors=True)
        if seg.banners:
            info["banners"] = [{"text": t, "rec_start": bn.start, "rec_end": bn.end,
                                "file_start": round(a, 3), "file_end": round(b, 3)}
                               for (a, b, t), bn in zip(windows, seg.banners)]
        if seg.blurs:
            info["blurs"] = [{"box": list(bl.box), "reason": bl.reason, "rec_start": bl.start, "rec_end": bl.end,
                              "file_start": round(a, 3), "file_end": round(b, 3)}
                             for (a, b, _), bl in zip(blurs, seg.blurs)]
    (session_dir / "vods" / f"seg{seg.id}.cut.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    return info


def file_time(cut: dict, t: float) -> Optional[float]:
    """Recording offset → position in the episode file (None if ``t`` falls in a cut)."""
    for p in cut.get("parts") or [{"rec_start": cut["start"], "rec_end": float("inf"), "file_start": 0.0}]:
        if p["rec_start"] - 3 <= t < p["rec_end"]:
            return max(p["file_start"], p["file_start"] + t - p["rec_start"])  # up to 3 s early: the part's first frame
    return None


def cut_info(session_dir: Path, seg_id: int) -> Optional[dict]:
    p = session_dir / "vods" / f"seg{seg_id}.cut.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def youtube_chapters(seg: Segment, cut, extra: list[tuple[float, str]] = ()) -> list[str]:
    """``m:ss Title`` lines relative to the episode file; the first is always 0:00.

    ``cut`` is the segment's cut info (``vods/seg<N>.cut.json``), or just the
    recording offset of the file's 00:00 for a segment without cuts.
    """
    info = cut if isinstance(cut, dict) else {"start": float(cut)}
    items = sorted([(c.at, c.title) for c in seg.chapters] + list(extra))
    lines = []
    for i, (at, title) in enumerate(items):
        rel = 0.0 if i == 0 else file_time(info, at)
        if rel is None:
            continue  # chapter inside a cut
        rel = int(rel)
        h, rem = divmod(rel, 3600)
        stamp = f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"
        lines.append(f"{stamp} {title}")
    return lines


# ---------------------------------------------------------------------------
# Thumbnail candidates
# ---------------------------------------------------------------------------

def thumb_scores(seg: Segment, session_dir: Path) -> list[tuple[float, float]]:
    """``(score, t)`` for moments likely to make a thumbnail: loudness peaks (reactions,
    scored by dB above the median), the times the streamer says a captured word (12), and just
    after each chapter starts (10-11)."""
    cands: list[tuple[float, float]] = []
    loud = session_dir / "signals" / "loudness.json"
    if loud.exists():
        for s in json.loads(loud.read_text(encoding="utf-8"))["segments"]:
            if s["segment"] == seg.id:
                cands += [(p["above_median"], p["t"] + 0.5) for p in s["peaks"]]
    vocab = session_dir / "signals" / "vocab.json"
    if vocab.exists():
        for cap in json.loads(vocab.read_text(encoding="utf-8")).get("captures", []):
            if cap.get("segment") == seg.id:
                for v in cap["vocab"]:
                    cands += [(12.0, h["t"] + 0.3) for h in v.get("heard", [])]
    cands += [(10.0, c.at + 30) for c in seg.chapters]
    meta = session_dir / "vods" / f"seg{seg.id}.meta.json"
    if meta.exists():  # transcript-based chapters from /stream-vods
        from .models import parse_tc
        cands += [(11.0, parse_tc(c["at"]) + 30) for c in json.loads(meta.read_text(encoding="utf-8")).get("chapters", [])]
    return cands


def thumb_times(seg: Segment, session_dir: Path, count: int = 16, spacing: float = 45.0) -> list[float]:
    """The best-scored ``thumb_scores`` moments, ``spacing`` apart; topped up evenly."""
    picked: list[float] = []
    for _, t in sorted(thumb_scores(seg, session_dir), key=lambda c: -c[0]):
        if seg.contains(t) and seg.start + 5 <= t <= seg.end - 5 and all(abs(t - p) >= spacing for p in picked):
            picked.append(round(t, 1))
        if len(picked) >= count:
            break
    step = seg.duration / (count + 1)
    for i in range(1, count + 1):  # even spread to fill gaps
        if len(picked) >= count:
            break
        t = round(seg.start + i * step, 1)
        if seg.contains(t) and all(abs(t - p) >= spacing for p in picked):
            picked.append(t)
    return sorted(picked)


def thumbs_dir(session_dir: Path, seg_id: int) -> Path:
    return session_dir / "vods" / f"seg{seg_id}_thumbs"


def extract_thumbs(session: Session, seg: Segment, session_dir: Path, count: int = 16) -> list[Path]:
    from concurrent.futures import ThreadPoolExecutor

    from .media import frame_name

    out = thumbs_dir(session_dir, seg.id) / "candidates"
    out.mkdir(parents=True, exist_ok=True)
    rec = Path(session.recording)
    times = thumb_times(seg, session_dir, count)
    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(lambda t: extract_frame(rec, t, out / frame_name(t)), times))


# ---------------------------------------------------------------------------
# seg<N>.youtube.md from seg<N>.meta.json
# ---------------------------------------------------------------------------

TWITCH_URL = f"https://www.twitch.tv/{settings.twitch_login()}"
DISCORD_URL = settings.discord_url()
YOUTUBE_DESC_MAX = 5000


def _short_meaning(m: str) -> str:
    m = (m or "").split(";")[0].strip()
    return m[: m.rfind("(")].strip() if m.count("(") > m.count(")") else m


def episode_words(session_dir: Path, seg: Segment) -> list[str]:
    """"word (reading) meaning" for vocab captured inside the segment's kept parts."""
    p = session_dir / "signals" / "vocab.json"
    if not p.exists():
        return []
    seen: dict[str, dict] = {}
    for cap in json.loads(p.read_text(encoding="utf-8")).get("captures", []):
        if seg.contains(cap["t"]):
            for w in cap["vocab"]:
                if w.get("category") != "pokemon":
                    seen.setdefault(w["word"], w)
    return [f"{w['word']} ({w['reading']}) {_short_meaning(w['meaning'])}" if w.get("reading") and w["reading"] != w["word"]
            else f"{w['word']} {_short_meaning(w['meaning'])}" for w in seen.values()]


def build_youtube_md(session: Session, seg: Segment, session_dir: Path) -> Path:
    """Write ``vods/seg<N>.youtube.md`` from ``vods/seg<N>.meta.json`` (Claude-written copy)."""
    from .models import fmt_short

    meta_path = session_dir / "vods" / f"seg{seg.id}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    cut = cut_info(session_dir, seg.id)
    if cut is None:
        raise RuntimeError(f"no vods/seg{seg.id}.cut.json yet: run stream-cut-vods")
    registry = load_series()
    ser = registry.get(seg.series or "", {})
    label = episode_label(ser, session.session, seg.episode_segment) if ser else "#?"
    if seg.full_of is not None:
        label += " (Full Version)"
    sep = ser.get("title_sep", " | ")  # e.g. ": " for "Rough Edges #1: <what was built>"
    title = f"{seg.series} {label}{sep}{meta['hook']}" if meta.get("hook") else f"{seg.series} {label}"
    if len(title) > 100:
        raise ValueError(f"title over 100 characters: {title}")
    chapters_src = [(parse_tc_safe(c["at"]), c["title"]) for c in meta.get("chapters", [])]
    chapter_seg = seg.model_copy(update={"chapters": []}) if chapters_src else seg
    chapters = youtube_chapters(chapter_seg, cut, chapters_src)
    playlist = f"https://www.youtube.com/playlist?list={ser['playlist_id']}" if ser.get("playlist_id") else None
    links = [f"Live on Twitch: {TWITCH_URL}", f"Discord: {DISCORD_URL}"]
    if playlist:
        links.append(f"More episodes: {playlist}")
    # Coding streams have no game: tag the series instead (Rough Edges).
    tag_game = re.sub(r"[^a-z0-9]", "", (seg.game or seg.series or "").lower().replace("(jp)", ""))
    hashtags = " ".join(h for h in dict.fromkeys([f"#{settings.twitch_login().lower()}", f"#{tag_game}" if tag_game else "",
                                                   "#learnjapanese" if seg.type == "japanese" else "",
                                                   "#coding" if seg.type == "coding" else ""]) if h)
    desc = "\n\n".join(meta.get("about", [])) + "\n\n" + "\n".join(links)
    if len(chapters) >= 3:
        desc += "\n\nChapters\n" + "\n".join(chapters)
    words = episode_words(session_dir, seg) if seg.type == "japanese" else []
    if words:
        # YouTube caps descriptions at 5,000 characters: list as many words as fit, then point to the deck.
        head = f"\n\nWords from this episode ({len(words)})\n"
        room = YOUTUBE_DESC_MAX - len(desc) - len(head) - len("\n\n" + hashtags) - 60
        shown, used = [], 0
        for w in words:
            if used + len(w) + 1 > room:
                break
            shown.append(w)
            used += len(w) + 1
        if len(shown) < len(words):
            shown.append(f"...and {len(words) - len(shown)} more in the Anki deck on Discord")
        desc += head + "\n".join(shown)
    desc += "\n\n" + hashtags
    game = (seg.game or "").replace(" (JP)", "")
    if game:
        base_tags = [game.lower(), f"{game.lower()} gameplay", settings.channel_name().lower(), "twitch stream", "let's play"]
    else:
        base_tags = [(seg.series or "").lower(), settings.channel_name().lower(), "twitch stream"]
        if seg.type == "coding":
            base_tags.append("live coding")
    if seg.type == "japanese":
        base_tags += ["learn japanese", "learning japanese with games", "japanese immersion", "anki"]
    tags = ", ".join(dict.fromkeys([t for t in base_tags + list(meta.get("tags_extra", [])) if t]))
    thumbs = session_dir / "vods" / f"seg{seg.id}_thumbs"
    picks = sorted(p.name for p in thumbs.glob("pick*.jpg")) if thumbs.exists() else []
    numbering = ser.get("numbering", "?")
    md = [f"# Segment {seg.id}: {seg.series} {label}", "",
          f"- **File:** `vods/{cut['file']}` ({fmt_short(cut['duration'])})",
          f"- **Playlist:** {seg.series}" + (f" ({playlist})" if playlist else " (no playlist yet: create it in Studio)"),
          f"- **Episode number:** {label}" + ("" if numbering == "locked" else " (series numbering is open: don't publish yet)"),
          "- **Visibility:** Private (set public or scheduled in Studio)",
          f"- **Thumbnail picks:** {', '.join(f'`vods/seg{seg.id}_thumbs/{p}`' for p in picks) or 'none yet (stream-thumbs)'}",
          "", f"## Title ({len(title)} chars)", "", title, "",
          f"## Description ({len(desc)} chars)", "", "```", desc, "```", ""]
    if len(chapters) < 3:
        md += ["Fewer than 3 chapters, so YouTube won't show them; left out of the description.", ""]
    if [c for c in cut.get("cuts", [])]:
        md += ["Cut out of this episode: " + "; ".join(f"{fmt_short(c['start'])}-{fmt_short(c['end'])} {c['reason']}"
                                                    for c in cut["cuts"]) + ".", ""]
    md += [f"## Tags ({len(tags)} chars)", "", tags, ""]
    meme_notes = episode_meme_lines(session, seg, cut)
    if meta.get("check") or meme_notes:
        md += ["## Check before publishing", "", "Times are positions in the episode file.", ""]
        if meta.get("check"):
            md += [episode_times(meta["check"], cut, seg), ""]
        if meme_notes:
            md += ["Memes with someone else's audio (Content ID can match these; med and high risk "
                   "are TV, film and game clips):", "", *[f"- {line}" for line in meme_notes], ""]
    out = session_dir / "vods" / f"seg{seg.id}.youtube.md"
    out.write_text("\n".join(md), encoding="utf-8")
    return out


def episode_meme_lines(session: Session, seg: Segment, cut: dict) -> list[str]:
    """Live (ARIA) and added memes in this episode, in file time, with their Content ID risk."""
    from .memes import live_memes, load_library

    library = load_library()
    rows = []
    for t, mid, _via in live_memes(session):
        ft = file_time(cut, t) if seg.contains(t) else None
        if ft is not None:
            m = library.get(mid)
            rows.append((ft, f"{fmt_short(ft)} {mid} (live, {m.risk if m else 'unknown'} risk)"))
    for m in cut.get("memes", []):
        rows.append((m["file_at"], f"{fmt_short(m['file_at'])} {m['id']} (added, {m['risk']} risk)"))
    return [text for _, text in sorted(rows)]


def parse_tc_safe(v) -> float:
    from .models import parse_tc
    return parse_tc(v)


_TIME_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])")


def episode_times(text: str, cut: dict, seg: Segment) -> str:
    """Rewrite recording times in free text ("02:41", "01:12:30") as episode-file times.

    HH:MM means hours and minutes (how the transcript notes are written). Times
    that fall in a cut or outside the segment are left as they are.
    """
    def repl(m: re.Match) -> str:
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        t = h * 3600 + mi * 60 + s
        if not (seg.start - 60 <= t <= seg.end + 60):
            return m.group(0)
        ft = file_time(cut, max(t, seg.start))
        if ft is None:  # inside a cut
            parts = cut.get("parts", [])
            prev = [p for p in parts if 0 <= t - p["rec_end"] <= 1]  # the cut's own start: end of the part before
            nxt = [p for p in parts if t < p["rec_start"] <= t + 90]  # otherwise where the episode resumes
            if prev:
                ft = prev[0]["file_start"] + prev[0]["rec_end"] - prev[0]["rec_start"]
            elif nxt:
                ft = nxt[0]["file_start"]
            else:
                return m.group(0)
        ft = int(ft)
        return f"{ft // 3600}:{ft % 3600 // 60:02d}:{ft % 60:02d}" if m.group(3) else f"{ft // 3600}:{ft % 3600 // 60:02d}"
    return _TIME_RE.sub(repl, text)
