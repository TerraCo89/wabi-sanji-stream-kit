"""Per-segment transcription with faster-whisper (spec: "Transcription").

Each non-skip segment is cut to 16 kHz mono WAV from the session's
``audio_track`` and transcribed separately. Output, with times as recording
offsets (segment start added):

  transcript/seg<N>.words.json   [{"w", "s", "e"}]  (same word format as shorts.py)
  transcript/seg<N>.txt          readable "[HH:MM:SS] line" transcript
  transcript/seg<N>.meta.json    model, settings, timing

Language handling (tested 2026-10-05, see the spec): every segment, Japanese
ones included, runs with ``language="en"``. The streamer speaks English and reads
Japanese lines aloud; forced English keeps those readings as romaji
("Ashita, that's tomorrow"), while ``language="ja"`` translates the English
into Japanese and ``multilingual=True`` detects English for every 30 s window
anyway.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .captions import Word
from . import settings
from .media import cut_audio_wav
from .models import Segment, Session, fmt_short


logger = logging.getLogger(__name__)

DEFAULT_MODEL = "large-v3-turbo"


def _add_cuda_dll_dirs() -> None:
    """Windows: let CTranslate2 find cuBLAS/cuDNN from the nvidia-* wheels (the `gpu` extra)."""
    import os
    import sys

    if sys.platform != "win32":
        return
    try:
        import nvidia  # namespace package from the nvidia-*-cu12 wheels
    except ImportError:
        return
    for base in getattr(nvidia, "__path__", []):
        for bin_dir in Path(base).glob("*/bin"):
            try:
                os.add_dll_directory(str(bin_dir))
            except OSError:
                pass
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def load_whisper(model: str = DEFAULT_MODEL):
    """faster-whisper on CUDA float16, falling back to CPU int8.

    ``WS_WHISPER_DEVICE`` forces ``cpu`` or ``cuda`` (default ``auto``).
    """
    _add_cuda_dll_dirs()
    from faster_whisper import WhisperModel

    device = settings.whisper_device()
    if device != "cpu":
        logger.info("Loading faster-whisper %r (CUDA float16) …", model)
        try:
            return WhisperModel(model, device="cuda", compute_type="float16")
        except Exception as exc:  # CUDA build missing or incompatible
            if device == "cuda":
                raise
            logger.warning("CUDA load failed (%s); falling back to CPU int8.", exc)
    return WhisperModel(model, device="cpu", compute_type="int8")


@dataclass
class TranscribeOptions:
    model: str = DEFAULT_MODEL
    language: Optional[str] = "en"
    beam_size: int = 5
    vad_filter: bool = True
    # Conditioning on previous text stays on: turning it off lost read-aloud
    # Japanese lines and duplicated another in the 2026-10-05 test.
    condition_on_previous_text: bool = True
    initial_prompt: Optional[str] = None


@dataclass
class SegmentResult:
    segment: int
    words: int
    audio_seconds: float
    elapsed_seconds: float
    language: str
    language_probability: float
    options: dict = field(default_factory=dict)

    @property
    def speed(self) -> float:
        return self.audio_seconds / self.elapsed_seconds if self.elapsed_seconds else 0.0


def default_prompt(seg: Segment) -> str:
    """Names Whisper otherwise mangles. Kept short: a long prompt can be echoed back."""
    names = [n for n in (seg.game, seg.series) if n]
    base = ", ".join(dict.fromkeys(names))
    return (base + ". " if base else "") + f"{settings.channel_name()} stream, Twitch chat."


def paths(session_dir: Path, seg_id: int) -> dict[str, Path]:
    t = session_dir / "transcript"
    return {
        "words": t / f"seg{seg_id}.words.json",
        "txt": t / f"seg{seg_id}.txt",
        "meta": t / f"seg{seg_id}.meta.json",
        "wav": session_dir / "audio" / f"seg{seg_id}.wav",
    }


def is_done(session_dir: Path, seg_id: int) -> bool:
    p = paths(session_dir, seg_id)
    return p["words"].exists() and p["txt"].exists()


# ---------------------------------------------------------------------------
# Readable transcript
# ---------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?。！？]$")


def readable_lines(
    words: list[Word], *, min_words: int = 8, max_words: int = 25, pause: float = 2.5
) -> list[str]:
    """``[HH:MM:SS] text`` lines for reading whole sessions.

    A line ends at a pause longer than ``pause`` seconds, at a sentence end
    once it has ``min_words``, or at ``max_words``. Turbo punctuates nearly
    every short phrase, so breaking at every full stop gave one-word lines.
    """
    lines: list[str] = []
    cur: list[Word] = []

    def flush() -> None:
        if cur:
            text = " ".join(w["w"] for w in cur)
            lines.append(f"[{_hms(cur[0]['s'])}] {text}")
            cur.clear()

    for w in words:
        if cur and w["s"] - cur[-1]["e"] > pause:
            flush()
        cur.append(w)
        if len(cur) >= max_words or (len(cur) >= min_words and _SENTENCE_END.search(w["w"])):
            flush()
    flush()
    return lines


def _hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

def transcribe_segment(
    wm,
    session: Session,
    seg: Segment,
    session_dir: Path,
    opts: TranscribeOptions,
    *,
    keep_audio: bool = False,
    progress: Optional[Callable[[float], None]] = None,
) -> SegmentResult:
    p = paths(session_dir, seg.id)
    prompt = opts.initial_prompt if opts.initial_prompt is not None else default_prompt(seg)
    kwargs = dict(
        language=opts.language,
        beam_size=opts.beam_size,
        word_timestamps=True,
        vad_filter=opts.vad_filter,
        condition_on_previous_text=opts.condition_on_previous_text,
        initial_prompt=prompt or None,
    )
    t0 = time.monotonic()
    words: list[Word] = []
    done = 0.0
    info = None
    # One pass per kept part, so cut stretches (breaks, trailers, other games) aren't transcribed.
    for i, (a, b) in enumerate(seg.parts):
        wav = p["wav"].with_name(f"seg{seg.id}_p{i}.wav") if len(seg.parts) > 1 else p["wav"]
        cut_audio_wav(Path(session.recording), a, b, session.audio_track, wav)
        segments, part_info = wm.transcribe(str(wav), **kwargs)
        info = info or part_info
        for s in segments:  # lazy generator: decoding happens here
            for w in s.words or []:
                text = w.word.strip()
                if text:
                    words.append({"w": text, "s": round(a + w.start, 2), "e": round(a + w.end, 2)})
            if progress:
                progress(done + s.end)
        done += b - a
        if not keep_audio:
            wav.unlink(missing_ok=True)
    elapsed = time.monotonic() - t0

    p["words"].parent.mkdir(parents=True, exist_ok=True)
    p["words"].write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
    header = (
        f"# {session.session} segment {seg.id} ({seg.type}) {seg.game or seg.label or ''}  "
        f"{_hms(seg.start)}-{_hms(seg.end)}  model {opts.model}, language {opts.language or 'auto'}\n"
        f"# Times are recording offsets. Japanese read aloud appears as romaji.\n"
    )
    p["txt"].write_text(header + "\n".join(readable_lines(words)) + "\n", encoding="utf-8")

    result = SegmentResult(
        segment=seg.id,
        words=len(words),
        audio_seconds=round(seg.duration, 1),
        elapsed_seconds=round(elapsed, 1),
        language=info.language,
        language_probability=round(info.language_probability, 3),
        options={**asdict(opts), "initial_prompt": prompt, "audio_track": session.audio_track},
    )
    p["meta"].write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
    if not keep_audio:
        try:
            p["wav"].parent.rmdir()
        except OSError:
            pass
    logger.info("segment %d: %d words, %s audio in %.0fs", seg.id, len(words), fmt_short(seg.duration), elapsed)
    return result


__all__ = [
    "DEFAULT_MODEL", "TranscribeOptions", "SegmentResult", "default_prompt", "paths",
    "is_done", "readable_lines", "transcribe_segment", "load_whisper",
]
