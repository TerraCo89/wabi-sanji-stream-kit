"""ffprobe / ffmpeg helpers for stream recordings: probing, frame sampling, audio cuts."""
from __future__ import annotations

import functools
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import logging

logger = logging.getLogger(__name__)


def _probe_dims(src: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(src)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {src}: {out.stderr[-300:]}")
    w, h = out.stdout.strip().split("x")
    return int(w), int(h)


def probe_duration(src: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(src)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {src}: {out.stderr[-300:]}")
    return float(out.stdout.strip())


def _run_ffmpeg(cmd: list[str], what: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        logger.error("ffmpeg %s failed: %s", what, (result.stderr or "")[-800:])
        raise RuntimeError(f"ffmpeg failed: {what}")


def _escape_filter_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", r"\:")


def extract_frame(src: Path, at: float, target: Path, *, width: int | None = None) -> Path:
    """Single JPEG at ``at`` seconds. ``width`` scales it down (aspect kept)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    scale = ["-vf", f"scale={width}:-2"] if width else []
    _run_ffmpeg(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", str(src), "-an", "-sn",
         *scale, "-frames:v", "1", "-q:v", "2", "-update", "1", str(target)],
        f"frame @ {at:.1f}s",
    )
    return target


SHEET_FRAME_WIDTH = 480  # frames/ and fine/ (contact-sheet tiles)
AT_FRAME_WIDTH = 960  # --at stills, big enough to read on-screen text
FINE_STEP = 10.0


@dataclass
class AudioTrack:
    index: int  # ffmpeg a:<index>
    codec: str
    channels: int
    sample_rate: int
    title: str


def probe_audio_tracks(src: Path) -> list[AudioTrack]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_name,channels,sample_rate:stream_tags=title",
         "-of", "json", str(src)],
        capture_output=True, text=True, encoding="utf-8",
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {src}: {out.stderr[-300:]}")
    streams = json.loads(out.stdout).get("streams", [])
    return [
        AudioTrack(
            index=i,
            codec=s.get("codec_name", "?"),
            channels=int(s.get("channels", 0)),
            sample_rate=int(s.get("sample_rate", 0)),
            title=(s.get("tags") or {}).get("title", ""),
        )
        for i, s in enumerate(streams)
    ]


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

# Layout crops (layouts.yaml), blur boxes (session.yaml) and face-cam crops are in 1920x1080
# reference pixels whatever size the recording is (OBS records 2560x1440 from 2026-10-18_2).
# ``to_source`` scales them to the real frame just before ffmpeg uses them.
REF_W, REF_H = 1920, 1080


@functools.lru_cache(maxsize=64)
def probe_video_size(src: Path) -> tuple[int, int]:
    """``(width, height)`` of the first video stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", str(src)],
        capture_output=True, text=True,
    )
    first = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    if out.returncode != 0 or "x" not in first:
        raise RuntimeError(f"ffprobe size failed for {src}: {out.stderr[-300:]}")
    w, h = first.split("x")[:2]
    return int(w), int(h)


def _even(v: float) -> int:
    return int(round(v / 2)) * 2


def to_source(box, size: tuple[int, int]) -> tuple[int, int, int, int]:
    """``[x, y, w, h]`` in reference pixels as even pixels of a ``size`` frame, kept inside it.

    A 1920x1080 source gets the box back unchanged.
    """
    x, y, w, h = (int(v) for v in box)
    sw, sh = size
    if (sw, sh) == (REF_W, REF_H):
        return x, y, w, h
    kx, ky = sw / REF_W, sh / REF_H
    w2, h2 = min(sw, max(2, _even(w * kx))), min(sh, max(2, _even(h * ky)))
    return max(0, min(sw - w2, _even(x * kx))), max(0, min(sh - h2, _even(y * ky))), w2, h2


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------

def frame_name(t: float) -> str:
    """``f_00090.jpg`` for whole seconds, ``f_02472.5.jpg`` otherwise (sorts by time)."""
    return f"f_{int(t):05d}.jpg" if float(t).is_integer() else f"f_{int(t):05d}.{int(round(t * 10)) % 10}.jpg"


def frame_time(path: Path) -> float:
    return float(path.stem[2:])


def per_minute_times(duration: float) -> list[float]:
    """t = 30, 90, 150, … (mid-minute, so a tile stands for its whole minute)."""
    return [float(t) for t in range(30, int(duration), 60)]


def fine_times(a: float, b: float, step: float = FINE_STEP) -> list[float]:
    if b < a:
        a, b = b, a
    n = int((b - a) // step)
    return [round(a + i * step, 1) for i in range(n + 1)]


def extract_frames(
    src: Path,
    times: Iterable[float],
    out_dir: Path,
    *,
    width: int = SHEET_FRAME_WIDTH,
    force: bool = False,
    workers: int = 6,
    on_done: Optional[Callable[[Path], None]] = None,
) -> list[Path]:
    """One JPEG per time via input seeking, several ffmpeg processes in parallel.

    Existing frames are kept unless ``force``, so re-runs are cheap.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = [(t, out_dir / frame_name(t)) for t in times]

    def one(job: tuple[float, Path]) -> Path:
        t, target = job
        if force or not target.exists():
            extract_frame(src, t, target, width=width)
        if on_done:
            on_done(target)
        return target

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, jobs))


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def cut_audio_wav(src: Path, start: float, end: float, audio_track: int, target: Path) -> Path:
    """16 kHz mono WAV of ``[start, end)`` from audio stream ``a:<audio_track>``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
         "-i", str(src), "-map", f"0:a:{audio_track}", "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(target)],
        f"audio cut {start:.1f}-{end:.1f}s",
    )
    return target


__all__ = [
    "AudioTrack", "probe_audio_tracks", "probe_duration", "frame_name", "frame_time",
    "per_minute_times", "fine_times", "extract_frames", "cut_audio_wav",
    "REF_W", "REF_H", "probe_video_size", "to_source",
    "SHEET_FRAME_WIDTH", "AT_FRAME_WIDTH", "FINE_STEP",
]
