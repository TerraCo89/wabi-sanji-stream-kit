"""Measure where Twitch VOD 0:00 falls in the OBS recording, by matching frames.

The VOD's ``created_at`` is NOT its first frame: on 2026-10-05 it was ~5 s
late (created_at said +13.0 s, frames said +7.8 s), which put the first Clip
From VOD 5 s early. So clip times are only trusted after this measurement.

How: read the VOD's HLS playlist (yt-dlp, lowest quality). ``#EXTINF``
durations give each segment's exact VOD start time. For a few sample moments
we download the segments around the expected VOD time, extract frames at
10 fps, and find each frame's best match in the recording (also 10 fps).
Only sharp matches count (the best frame clearly beats every frame more than
0.5 s away), because static scenes match everywhere. Two samples have to agree.
"""
from __future__ import annotations

import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import requests
from PIL import Image

FPS = 10
SIZE = (240, 136)
SEARCH = 20.0  # seconds either side of the expected match


def parse_playlist(text: str, base: str) -> tuple[list[tuple[float, float, str]], Optional[str]]:
    """``([(vod_start, duration, url)], init_url)`` from an HLS media playlist.

    ``init_url`` is the ``#EXT-X-MAP`` init segment of a fragmented-MP4 playlist (2K VODs from
    2026-10-18_2 on): every media segment needs it in front before ffmpeg can read it.
    """
    segs, t, dur, init = [], 0.0, None, None
    for line in text.splitlines():
        if line.startswith("#EXT-X-MAP:"):
            uri = line.split("URI=", 1)[1].split(",")[0].strip('"')
            init = urllib.parse.urljoin(base, uri)
        elif line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and dur is not None:
            segs.append((round(t, 3), dur, urllib.parse.urljoin(base, line)))
            t += dur
            dur = None
    return segs, init


def playlist(vod_id: str, fmt: str = "worst[height>=160]/worst") -> tuple[list[tuple[float, float, str]], Optional[str]]:
    """Every HLS segment of the VOD plus its init segment, if any (see ``parse_playlist``; ``fmt``: yt-dlp format)."""
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format": fmt}) as ydl:
        info = ydl.extract_info(f"https://www.twitch.tv/videos/{vod_id}", download=False)
    m3u8 = info["url"]
    return parse_playlist(requests.get(m3u8, timeout=30).text, m3u8.rsplit("/", 1)[0] + "/")


def _frames(src: Path, out_dir: Path, start: Optional[float] = None, length: Optional[float] = None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    seek = ["-ss", f"{start:.3f}", "-t", f"{length:.3f}"] if start is not None else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", *seek, "-i", str(src), "-an",
                    "-vf", f"fps={FPS},scale={SIZE[0]}:{SIZE[1]}", "-start_number", "0", str(out_dir / "%05d.png")],
                   check=True)
    return sorted(out_dir.glob("*.png"))


def _load(p: Path) -> np.ndarray:
    return np.asarray(Image.open(p).convert("L"), dtype=np.float32)


def sharp_offsets(vod_frames: list[tuple[float, np.ndarray]], rec_frames: list[tuple[float, np.ndarray]],
                  max_mse: float = 30.0) -> list[float]:
    """``recording_t - vod_t`` for every VOD frame with an unambiguous best match."""
    rs = np.stack([a for _, a in rec_frames])
    rt = np.array([t for t, _ in rec_frames])
    out = []
    for v, a in vod_frames:
        mse = ((rs - a) ** 2).mean(axis=(1, 2))
        i = int(mse.argmin())
        far = mse[np.abs(rt - rt[i]) > 0.5]
        if far.size and mse[i] < max_mse and far.min() > 3 * mse[i] + 20:
            out.append(float(rt[i] - v))
    return out


def measure(recording: Path, vod_id: str, sample_times: list[float], prior: float,
            say: Callable[[str], None] = print, need: int = 2) -> dict:
    """Offset (recording seconds at VOD 0:00) from frame matches around ``sample_times``.

    ``prior`` is the rough offset (from created_at) used to find the right VOD
    stretch. Stops once ``need`` samples agree within 0.3 s.
    """
    segs, init_url = playlist(vod_id)
    init = requests.get(init_url, timeout=60).content if init_url else b""
    samples: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="vodsync_") as tmp:
        tmp = Path(tmp)
        for k, r in enumerate(sample_times):
            v = r - prior
            chosen = [s for s in segs if s[0] + s[1] > v - 6 and s[0] < v + 6]
            if not chosen:
                continue
            vod_frames: list[tuple[float, np.ndarray]] = []
            for st, _dur, url in chosen:
                ts = tmp / f"s{k}_{st:.3f}.ts"
                ts.write_bytes(init + requests.get(url, timeout=60).content)
                for i, p in enumerate(_frames(ts, tmp / f"v{k}_{st:.3f}")):
                    vod_frames.append((st + i / FPS, _load(p)))
            r0 = max(0.0, chosen[0][0] + prior - SEARCH)
            r_len = chosen[-1][0] + chosen[-1][1] - chosen[0][0] + 2 * SEARCH
            rec_frames = [(r0 + i / FPS, _load(p)) for i, p in enumerate(_frames(recording, tmp / f"r{k}", r0, r_len))]
            offs = sharp_offsets(vod_frames, rec_frames)
            if len(offs) >= 3 and np.ptp(offs) <= 0.3:
                med = round(float(np.median(offs)), 2)
                samples.append({"recording_t": round(r, 1), "offset": med, "matches": len(offs)})
                say(f"  sample at recording {r:.0f}s: offset {med:+.2f}s ({len(offs)} sharp matches)")
            else:
                say(f"  sample at recording {r:.0f}s: no clear match (static scene), trying another")
            agree = [s for s in samples if abs(s["offset"] - samples[-1]["offset"]) <= 0.3] if samples else []
            if len(agree) >= need:
                return {"offset": round(float(np.median([s["offset"] for s in agree])), 1), "samples": samples,
                        "method": "frames"}
    raise RuntimeError(f"couldn't measure the VOD offset: {len(samples)} usable sample(s) {samples}")


# ---------------------------------------------------------------------------
# Recording stopped before the stream ended: add the rest from the VOD
# ---------------------------------------------------------------------------

def download_vod_range(vod_id: str, start: float, end: Optional[float], out: Path,
                       say: Callable[[str], None] = print) -> tuple[Path, float]:
    """Source-quality HLS segments covering VOD ``start``..``end`` joined into one .ts.

    Returns ``(file, vod_time_of_file_start)``; trim the exact start when encoding.
    """
    all_segs, init_url = playlist(vod_id, "best")
    segs = [s for s in all_segs if s[0] + s[1] > start and (end is None or s[0] < end)]
    if not segs:
        raise RuntimeError(f"no VOD segments after {start:.1f} s")
    out.parent.mkdir(parents=True, exist_ok=True)
    total = len(segs)
    with out.open("wb") as fh:
        if init_url:   # fragmented MP4: one init segment, then the fragments (the file is MP4, whatever its name)
            fh.write(requests.get(init_url, timeout=60).content)
        for i, (_st, _dur, url) in enumerate(segs, 1):
            for attempt in range(3):
                try:
                    r = requests.get(url, timeout=60)
                    r.raise_for_status()
                    fh.write(r.content)
                    break
                except requests.RequestException:
                    if attempt == 2:
                        raise
            if i % 50 == 0 or i == total:
                say(f"  downloaded {i}/{total} segments")
    return out, segs[0][0]


def join_with_tail(recording: Path, tail: Path, tail_skip: float, out: Path) -> Path:
    """Re-encode recording + tail (from ``tail_skip`` s into it) as one H.264/AAC file on NVENC.

    Re-encoded rather than stream-copied: the two halves come from different
    encoders, and a stream-copy join can break playback at the seam. Keyframes
    every 2 s, like OBS, so later stream-copy cuts stay accurate. The result keeps
    the recording's size (2560x1440 from 2026-10-18_2); the Twitch tail is scaled to it.
    """
    from .media import probe_video_size

    w, h = probe_video_size(recording)
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(recording), "-ss", f"{tail_skip:.3f}", "-i", str(tail),
         "-filter_complex",
         f"[0:v:0]scale={w}:{h},fps=60,setsar=1,format=yuv420p[v0];"
         f"[1:v:0]scale={w}:{h}:flags=lanczos,fps=60,setsar=1,format=yuv420p[v1];"
         "[0:a:0]aresample=48000[a0];[1:a:0]aresample=48000[a1];"
         "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]",
         "-map", "[v]", "-map", "[a]",
         "-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0", "-maxrate", "40M",
         "-bufsize", "80M", "-profile:v", "high", "-g", "120",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)],
        check=True,
    )
    return out
