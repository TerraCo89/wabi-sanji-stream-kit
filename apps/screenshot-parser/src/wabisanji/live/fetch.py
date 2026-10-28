"""`wabisanji live fetch`: get a meme into the library in one step.

Sources:
- a YouTube (or other yt-dlp) URL: the --from/--to section, or the whole video if
  it's a short meme upload (under 90 s)
- a myinstants.com sound page: its mp3
- a GIPHY clip page or id: its giphy480p.mp4 rendition (plain giphy.mp4 has no
  audio). GIPHY's API terms don't allow keeping copies without their approval,
  so prefer the original source
- a direct .mp4/.webm/.mp3/.gif/.png URL, or a local file

`--expect "oh no anyway"` has Whisper find that line and trims tight around it,
so a 40 s upload becomes the 1.5 s bite and a wrong video is caught early.

Clips are scaled to at most 1280 wide and saved as <id>.mp4, sounds as <id>.mp3,
both levelled to -20 LUFS so every meme plays at the same loudness. Images and
GIFs are copied as they are. The entry is appended to memes.yaml.

Needs ffmpeg/ffprobe on PATH, yt-dlp for video sites, and the [listen] extra
for --expect.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

import requests
import yaml

LOUDNESS = "loudnorm=I=-20:TP=-2:LRA=11"
MAX_CLIP_S = 12  # the overlay stops clips at 12 s anyway
MAX_WHOLE_S = 90  # longest video downloaded whole when no --from/--to is given
PAD_BEFORE_S, PAD_AFTER_S = 0.15, 0.45  # around an --expect match
GIPHY_ID = re.compile(r"(?:giphy\.com/(?:clips|gifs)/(?:[\w-]*-)?|^)([A-Za-z0-9]{10,24})/?$")
IMAGE_EXT = {".gif", ".png", ".jpg", ".jpeg", ".webp"}
UA = {"User-Agent": "Mozilla/5.0"}


class FetchError(RuntimeError):
    pass


def giphy_clip_url(source: str) -> str | None:
    m = GIPHY_ID.search(source.split("?")[0])
    if not m or ("giphy" not in source and not re.fullmatch(r"[A-Za-z0-9]{10,24}", source)):
        return None
    return f"https://media.giphy.com/media/{m.group(1)}/giphy480p.mp4"


def fetch(source: str, dest_dir: Path, meme_id: str, start: str | None = None,
          end: str | None = None, audio_only: bool = False, expect: str | None = None,
          log=print) -> tuple[Path, str]:
    """Download/convert `source` into dest_dir/<meme_id>.<ext>.

    Returns (path, the source actually used): for `ytsearch:<query>` that's the
    first short result in which Whisper hears `expect`.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", meme_id):
        raise FetchError(f"id {meme_id!r}: use lowercase letters, digits and dashes")
    if source.startswith("ytsearch:"):
        if not expect:
            raise FetchError("ytsearch: needs --expect, to check the result is the right meme")
        tried = []
        for url, length, title in youtube_search(source.removeprefix("ytsearch:")):
            log(f"trying {url} ({length:.0f}s) {title}")
            try:
                return fetch(url, dest_dir, meme_id, audio_only=audio_only, expect=expect,
                             log=log)
            except FetchError as e:
                tried.append(f"{url}: {e}")
        raise FetchError("no short search result had the line:\n  " + "\n  ".join(tried))
    path = _fetch_one(source, dest_dir, meme_id, start, end, audio_only, expect, log)
    return path, source


def youtube_search(query: str, n: int = 8) -> list[tuple[str, float, str]]:
    """Short results first: meme uploads, not compilations."""
    out = _run(["yt-dlp", "--no-warnings", "--flat-playlist", "--print",
                "%(id)s\t%(duration)s\t%(title)s", f"ytsearch{n}:{query}"])
    hits = []
    for line in out.splitlines():
        vid, length, title = (line.split("\t") + ["", ""])[:3]
        try:
            seconds = float(length)
        except ValueError:
            continue
        if 0 < seconds <= MAX_WHOLE_S:
            hits.append((f"https://www.youtube.com/watch?v={vid}", seconds, title))
    return sorted(hits, key=lambda h: h[1])[:4]


def _fetch_one(source: str, dest_dir: Path, meme_id: str, start: str | None, end: str | None,
               audio_only: bool, expect: str | None, log) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="meme-") as tmp:
        raw = _download(source, Path(tmp), start, end)
        if _is_ytdlp(source):
            start = end = None  # already cut by yt-dlp
        if raw.suffix.lower() in IMAGE_EXT and not (start or end or expect):
            out = dest_dir / f"{meme_id}{raw.suffix.lower()}"
            shutil.copyfile(raw, out)
            return out
        if expect:
            a, b, heard = find_phrase(raw, expect, offset=_seconds(start))
            log(f"found {expect!r} at {a:.2f}-{b:.2f}s (heard: {heard!r})")
            start, end = f"{max(0.0, a - PAD_BEFORE_S):.2f}", f"{b + PAD_AFTER_S:.2f}"
        sound = audio_only or not _has_stream(raw, "v") or raw.suffix.lower() in IMAGE_EXT
        out = dest_dir / f"{meme_id}.{'mp3' if sound else 'mp4'}"
        _level(raw, out, start, end, sound)
        return out


def _is_ytdlp(source: str) -> bool:
    return bool(re.match(
        r"https?://(www\.|m\.)?(youtube\.com|youtu\.be|twitch\.tv|tiktok\.com|instagram\.com)",
        source))


def _seconds(t: str | None) -> float:
    if not t:
        return 0.0
    total = 0.0
    for part in t.split(":"):
        total = total * 60 + float(part)
    return total


def _download(source: str, tmp: Path, start: str | None, end: str | None) -> Path:
    local = Path(source)
    if local.is_file():
        return local
    if _is_ytdlp(source):
        return _ytdlp(source, tmp, start, end)
    if source.startswith("myinstants:"):
        source = _myinstants_search(source.removeprefix("myinstants:"))
    if "myinstants.com" in source and not source.endswith(".mp3"):
        source = _myinstants_mp3(source)
    url = giphy_clip_url(source) or source
    if not url.startswith("http"):
        raise FetchError(f"not a file, URL, GIPHY clip or myinstants page: {source}")
    suffix = Path(url.split("?")[0]).suffix.lower() or ".mp4"
    r = requests.get(url, headers=UA, timeout=30)
    if not r.ok:
        raise FetchError(f"download failed: {r.status_code} {url}")
    raw = tmp / f"raw{suffix}"
    raw.write_bytes(r.content)
    return raw


def _ytdlp(url: str, tmp: Path, start: str | None, end: str | None) -> Path:
    if not shutil.which("yt-dlp"):
        raise FetchError("yt-dlp isn't on PATH")
    cmd = ["yt-dlp", "-q", "--no-warnings", "--no-playlist",
           "-f", "bv*[height<=720]+ba/b[height<=720]/b", "--merge-output-format", "mp4",
           "-o", str(tmp / "raw.%(ext)s")]
    if start and end:
        cmd += ["--download-sections", f"*{start}-{end}", "--force-keyframes-at-cuts"]
    else:
        length = _run(["yt-dlp", "--no-warnings", "--no-playlist", "--print", "duration", url])
        if float(length.strip() or "inf") > MAX_WHOLE_S:
            raise FetchError(f"video is {float(length):.0f}s long: give --from and --to")
    _run([*cmd, url])
    files = list(tmp.glob("raw.*"))
    if not files:
        raise FetchError("yt-dlp produced no file")
    return files[0]


def _myinstants_search(query: str) -> str:
    """The top myinstants result for `query` (their search ranks by popularity)."""
    r = requests.get("https://www.myinstants.com/en/search/", params={"name": query},
                     headers=UA, timeout=30)
    if not r.ok:
        raise FetchError(f"myinstants search failed: {r.status_code}")
    m = re.search(r"/en/instant/[\w-]+/", r.text)
    if not m:
        raise FetchError(f"myinstants has nothing for {query!r}")
    return "https://www.myinstants.com" + m.group(0)


def _myinstants_mp3(page: str) -> str:
    r = requests.get(page, headers=UA, timeout=30)
    if not r.ok:
        raise FetchError(f"myinstants page failed: {r.status_code}")
    m = re.search(r"/media/sounds/[^'\"\s)]+\.mp3", html.unescape(r.text))
    if not m:
        raise FetchError("no mp3 on that myinstants page")
    return "https://www.myinstants.com" + m.group(0)


def find_phrase(media: Path, phrase: str, offset: float = 0.0) -> tuple[float, float, str]:
    """(start, end, heard) of `phrase` in the media's speech, via Whisper word timestamps.

    `offset` skips audio before it (a --from given with --expect).
    """
    import numpy as np
    from faster_whisper import WhisperModel

    from .listen import file_chunks

    audio = np.concatenate(list(file_chunks(media, start=offset)) or [np.zeros(1600)])
    model = WhisperModel("base.en", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(audio.astype("float32"), language="en",
                                   word_timestamps=True, condition_on_previous_text=False)
    words = [w for s in segments for w in (s.words or [])]
    want = _norm(phrase)
    n = len(want.split())
    best = (0.0, None)
    for size in {max(1, n - 1), n, n + 1}:
        for i in range(len(words) - size + 1):
            window = words[i:i + size]
            ratio = SequenceMatcher(None, want, _norm(" ".join(w.word for w in window))).ratio()
            if ratio > best[0]:
                best = (ratio, window)
    ratio, window = best
    heard = " ".join(w.word.strip() for w in words)
    if not window or ratio < 0.75:
        raise FetchError(f"didn't hear {phrase!r} (best match {ratio:.2f}); heard: {heard!r}")
    return (offset + window[0].start, offset + window[-1].end,
            " ".join(w.word.strip() for w in window))


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower().replace("'", "")).split())


def _has_stream(path: Path, kind: str) -> bool:
    out = _run(["ffprobe", "-v", "error", "-select_streams", kind, "-show_entries",
                "stream=codec_type", "-of", "csv=p=0", str(path)])
    # Cover art in an mp3 shows up as a one-frame "video" stream
    return bool(out.strip()) and not (kind == "v" and path.suffix.lower() in (".mp3", ".m4a"))


def _level(src: Path, out: Path, start: str | None, end: str | None, sound: bool) -> None:
    trim = (["-ss", start] if start else []) + (["-to", end] if end else [])
    has_audio = _has_stream(src, "a")
    if sound and not has_audio:
        raise FetchError("that source has no audio")
    audio = ["-af", LOUDNESS, "-ar", "48000"] if has_audio else ["-an"]
    if sound:
        enc = ["-vn", *audio, "-c:a", "libmp3lame", "-b:a", "160k"]
    else:
        enc = ["-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-preset", "slow",
               "-crf", "22", "-pix_fmt", "yuv420p", *audio,
               *(["-c:a", "aac", "-b:a", "128k"] if has_audio else []), "-movflags", "+faststart"]
    _run(["ffmpeg", "-v", "error", "-y", *trim, "-i", str(src), "-t", str(MAX_CLIP_S),
          *enc, str(out)])


def probe(path: Path) -> dict:
    """Duration and whether it has sound, for the index comment."""
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                "-of", "default=nw=1", str(path)])
    duration = re.search(r"duration=([\d.]+)", out)
    return {"duration": float(duration.group(1)) if duration else None,
            "audio": "codec_type=audio" in out}


def check_new(index: Path, meme_id: str) -> None:
    data = yaml.safe_load(index.read_text(encoding="utf-8")) if index.exists() else {}
    if any(m.get("id") == meme_id for m in (data or {}).get("memes") or []):
        raise FetchError(f"{meme_id} is already in {index.name}; edit it there")


def append_entry(index: Path, meme_id: str, file: str, phrases: list[str], about: str,
                 note: str = "", cooldown_s: int | None = None, **extra) -> None:
    """Add an entry to memes.yaml as text, so the file's comments survive.

    `extra` holds the optional fields (caption, category, risk, family) in that order.
    """
    check_new(index, meme_id)
    text = index.read_text(encoding="utf-8") if index.exists() else "memes:\n"
    text = re.sub(r"^memes:\s*\[\]\s*$", "memes:", text, flags=re.M)
    entry = {"id": meme_id, "file": file}
    entry.update({k: v for k, v in extra.items() if k == "caption" and v})
    entry.update({"phrases": phrases, "about": about})
    entry.update({k: v for k, v in extra.items() if k != "caption" and v is not None})
    if cooldown_s:
        entry["cooldown_s"] = cooldown_s
    block = yaml.safe_dump([entry], allow_unicode=True, sort_keys=False, width=88,
                           default_flow_style=None)
    block = "\n".join("  " + line if line else line for line in block.splitlines())
    if note:
        block = block.replace(f"file: {file}", f"file: {file}  # {note}", 1)
    index.write_text(text.rstrip("\n") + "\n\n" + block + "\n", encoding="utf-8")
    yaml.safe_load(index.read_text(encoding="utf-8"))  # still valid


def _run(cmd: list[str]) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False)
    except FileNotFoundError as e:
        raise FetchError(f"{cmd[0]} isn't on PATH") from e
    if done.returncode:
        raise FetchError(f"{cmd[0]} failed: {done.stderr.strip()[-400:]}")
    return done.stdout
