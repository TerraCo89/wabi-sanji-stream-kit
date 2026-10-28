"""`wabisanji listen`: mic -> voice activity -> Whisper (CPU) -> ARIA.

Runs on the CPU on purpose: the GPU belongs to OBS while streaming, the same
reason Ollama stays off. Only the mic is captured, so viewer TTS and game
audio can't wake ARIA (unless they bleed through speakers).

Per utterance:
- "Hey ARIA, <question>" -> question event for Claude + "thinking" card
- "ARIA, more / never mind / repeat / memes off" -> handled here, no Claude
- anything else -> instant meme phrase match, then the transcript Claude reads
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from . import wake as wakemod
from .client import LiveClient
from .memes import MemeMatcher
from .store import LiveStore

log = logging.getLogger(__name__)

RATE = 16000
FRAME = 512  # samples per VAD frame (32 ms), what Silero expects at 16 kHz
FRAME_S = FRAME / RATE

# Whisper's favourite inventions on breath and keyboard noise
HALLUCINATIONS = {
    "you", "thank you", "thanks for watching", "thank you for watching", "bye",
    "subtitles by the amaraorg community", "so", "uh", "um", "hmm", "",
}

AWAIT_QUESTION_S = 10  # after a bare "Hey ARIA", the next utterance is the question
SEARCH_WINDOW_S = 90  # after a question, a bare "stop that" cancels it


# --- voice activity ---------------------------------------------------------


class StreamingVad:
    """Silero VAD (bundled with faster-whisper) fed one 512-sample frame at a time.

    faster-whisper's own wrapper resets the model state on every call, which is
    fine for whole files but useless for a live stream, so this keeps h/c and
    the 64-sample context between frames.
    """

    CONTEXT = 64

    def __init__(self):
        from faster_whisper.vad import get_vad_model

        self.session = get_vad_model().session
        self.reset()

    def reset(self) -> None:
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.context = np.zeros(self.CONTEXT, dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        x = np.concatenate([self.context, frame]).astype(np.float32)[None, :]
        out, self.h, self.c = self.session.run(None, {"input": x, "h": self.h, "c": self.c})
        self.context = frame[-self.CONTEXT:]
        return float(np.asarray(out).reshape(-1)[0])


class Segmenter:
    """Turns per-frame speech probabilities into whole utterances."""

    def __init__(
        self,
        on: float = 0.5,
        off: float = 0.3,
        min_silence_s: float = 0.9,
        min_speech_s: float = 0.3,
        max_speech_s: float = 15.0,
        preroll_s: float = 0.3,
    ):
        self.on, self.off = on, off
        self.min_silence = round(min_silence_s / FRAME_S)
        self.min_speech = round(min_speech_s / FRAME_S)
        self.max_speech = round(max_speech_s / FRAME_S)
        self.preroll: list[np.ndarray] = []
        self.preroll_n = round(preroll_s / FRAME_S)
        self.frames: list[np.ndarray] = []
        self.speaking = False
        self.silent = 0
        self.voiced = 0

    def feed(self, frame: np.ndarray, prob: float) -> np.ndarray | None:
        """Returns a finished utterance's audio, or None."""
        if not self.speaking:
            self.preroll = (self.preroll + [frame])[-self.preroll_n:]
            if prob >= self.on:
                self.speaking = True
                self.frames = list(self.preroll)
                self.silent = 0
                self.voiced = 1
            return None

        self.frames.append(frame)
        if prob >= self.off:
            self.silent = 0
            self.voiced += 1
        else:
            self.silent += 1
        if self.silent >= self.min_silence or len(self.frames) >= self.max_speech:
            return self._finish()
        return None

    def flush(self) -> np.ndarray | None:
        return self._finish() if self.speaking else None

    def _finish(self) -> np.ndarray | None:
        audio = np.concatenate(self.frames) if self.voiced >= self.min_speech else None
        self.speaking = False
        self.frames, self.preroll = [], []
        self.silent = self.voiced = 0
        return audio


# --- speech to text ---------------------------------------------------------


class Transcriber:
    def __init__(self, model: str = "base.en", cpu_threads: int = 4):
        from faster_whisper import WhisperModel

        # device="cpu" always: never touch the GPU OBS is encoding on
        self.model = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=cpu_threads)

    def __call__(self, audio: np.ndarray, hotwords: str = "") -> str:
        segments, _ = self.model.transcribe(
            audio,
            language="en",
            beam_size=1,
            vad_filter=False,  # already cut by our VAD
            condition_on_previous_text=False,
            without_timestamps=True,
            hotwords=hotwords or None,
        )
        return " ".join(s.text.strip() for s in segments).strip()


def is_noise(text: str) -> bool:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split()) in HALLUCINATIONS


# --- what to do with each utterance -----------------------------------------


class Listener:
    def __init__(
        self,
        store: LiveStore,
        client: LiveClient | None,
        matcher: MemeMatcher,
        wake_words: list[str],
        speak: Callable[[str], None] | None,
        game: Callable[[], str | None],
        set_game: Callable[[str], str] | None = None,
        dry_run: bool = False,
        clock: Callable[[], float] = time.monotonic,
        out: Callable[[str], None] = print,
    ):
        self.store = store
        self.client = None if dry_run else client
        self.matcher = matcher
        self.pattern = wakemod.build_pattern(wake_words)
        self.speak_fn = None if dry_run else speak
        self.game = game
        self.set_game = None if dry_run else set_game
        self.dry_run = dry_run
        self.clock = clock
        self.out = out
        self.awaiting_until = float("-inf")
        self.searching_until = float("-inf")
        self.memes_on = True  # dry runs keep their own flags instead of state.json
        self.family = False

    def handle(self, text: str, at: str = "") -> None:
        text = text.strip()
        if is_noise(text):
            return
        if not self.dry_run:
            self.store.add_utterance(text)
        self.out(f"{at}  {text}")

        hit = wakemod.parse(text, self.pattern)
        if hit is None and self.clock() < self.awaiting_until:
            hit = wakemod.classify(text)  # "Hey ARIA" ... pause ... the question
        if hit is None and self.clock() < self.searching_until and wakemod.is_bare_stop(text):
            hit = wakemod.Wake("clear")
        if hit is not None:
            self.awaiting_until = float("-inf")
            self.on_wake(hit)
            return

        if self.memes_enabled():
            meme = self.matcher.match(text, family_mode=self.family_mode())
            if meme:
                self.matcher.fired(meme.id)
                self.out(f"{at}  -> meme {meme.id}")
                if self.client and self.client.post("/api/live/meme", {"id": meme.id}):
                    self.store.add_event("meme", id=meme.id, via="phrase", heard=text)

    def on_wake(self, hit: wakemod.Wake) -> None:
        if hit.command == "listen":
            self.awaiting_until = self.clock() + AWAIT_QUESTION_S
            self.out("   ARIA listening...")
            if self.client:
                self.client.post("/api/live/listening", {"id": _new_id()})
        elif hit.command == "question":
            qid = _new_id()
            game = self.game()
            self.searching_until = self.clock() + SEARCH_WINDOW_S
            self.out(f"   ARIA question {qid} ({game or 'game not set'}): {hit.text}")
            if not self.dry_run:
                self.store.add_event("question", id=qid, text=hit.text, game=game)
            if self.client:
                self.client.post("/api/live/thinking", {"id": qid, "heard": hit.text})
        elif hit.command == "more":
            self.out("   ARIA: more")
            card = self.client.post("/api/live/expand") if self.client else None
            if card:
                self.store.add_event("expand", id=card["id"])
                self.say(_spoken_steps(card.get("steps") or []))
        elif hit.command == "repeat":
            self.out("   ARIA: repeat")
            card = self.client.card() if self.client else None
            if card and card["status"] == "expanded":
                self.say(_spoken_steps(card.get("steps") or []))
            elif card and card.get("hint"):
                self.say(card["hint"])
        elif hit.command == "clear":
            self.out("   ARIA: clear")
            self.searching_until = float("-inf")
            card =self.client.card() if self.client else None
            if self.client:
                self.client.post("/api/live/clear")
                self.store.add_event("clear", id=card["id"] if card else None)
        elif hit.command in ("memes_off", "memes_on"):
            on = hit.command == "memes_on"
            self.out(f"   ARIA: memes {'on' if on else 'off'}")
            self.memes_on = on
            if not self.dry_run:
                self.store.set_state(memes=on)
            self.say("Memes on." if on else "Memes off.")
        elif hit.command == "game":
            game = self.set_game(hit.text) if self.set_game else hit.text
            self.out(f"   ARIA: game is now {game}")
            if not self.dry_run:
                self.store.add_event("game", game=game, heard=hit.text)
            self.say(f"Okay, {game}.")
        elif hit.command in ("family_on", "family_off"):
            on = hit.command == "family_on"
            self.out(f"   ARIA: family mode {'on' if on else 'off'}")
            self.family = on
            if not self.dry_run:
                self.store.set_state(family=on)
            self.say("Family mode on." if on else "Family mode off.")

    def family_mode(self) -> bool:
        return self.family if self.dry_run else self.store.family_mode()

    def memes_enabled(self) -> bool:
        return self.memes_on if self.dry_run else self.store.memes_enabled()

    def say(self, text: str) -> None:
        if not text or not self.speak_fn:
            return
        try:
            self.speak_fn(text)
        except Exception as e:  # a dead Speaker.bot must never stop the listener
            log.warning("ARIA couldn't speak: %s", e)


def _new_id() -> str:
    return datetime.now().strftime("q%m%d-%H%M%S")


def _spoken_steps(steps: list[str]) -> str:
    return " ".join(f"{i}. {s.rstrip('.')}." for i, s in enumerate(steps, 1))


# --- audio sources ----------------------------------------------------------


def find_device(mic: str | int | None):
    """sounddevice index for a name substring or index; None = system default input."""
    import sounddevice as sd

    if mic is None or isinstance(mic, int) or str(mic).isdigit():
        return None if mic is None else int(mic)
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and str(mic).lower() in dev["name"].lower():
            return i
    raise ValueError(f"no input device matching {mic!r} (see `wabisanji listen --list-devices`)")


def list_devices() -> str:
    import sounddevice as sd

    default = sd.default.device[0]
    lines = []
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            api = sd.query_hostapis(dev["hostapi"])["name"]
            mark = "*" if i == default else " "
            lines.append(f"{mark}{i:>3}  {dev['name']}  [{api}, {int(dev['default_samplerate'])} Hz]")
    return "\n".join(lines)


def to_16k(block: np.ndarray, rate: int) -> np.ndarray:
    if rate == RATE:
        return block
    if rate % RATE == 0:
        # Integer ratio (48k -> 16k): average groups, a cheap low-pass + decimate
        n = rate // RATE
        usable = len(block) - len(block) % n
        return block[:usable].reshape(-1, n).mean(axis=1)
    out_len = int(round(len(block) * RATE / rate))
    return np.interp(np.linspace(0, len(block), out_len, endpoint=False), np.arange(len(block)), block)


def frames_from(chunks: Iterator[np.ndarray]) -> Iterator[np.ndarray]:
    """Re-cut arbitrary 16 kHz chunks into 512-sample frames."""
    buf = np.zeros(0, dtype=np.float32)
    for chunk in chunks:
        buf = np.concatenate([buf, chunk.astype(np.float32)])
        while len(buf) >= FRAME:
            yield buf[:FRAME]
            buf = buf[FRAME:]


def mic_chunks(device, stop: threading.Event) -> Iterator[np.ndarray]:
    import sounddevice as sd

    rate = int(sd.query_devices(device, "input")["default_samplerate"])
    q: queue.Queue[np.ndarray] = queue.Queue()

    def callback(indata, frames, time_info, status):
        if status:
            log.debug("audio: %s", status)
        q.put(indata[:, 0].copy())

    with sd.InputStream(
        device=device, channels=1, samplerate=rate, dtype="float32",
        blocksize=FRAME * rate // RATE, callback=callback,
    ):
        while not stop.is_set():
            try:
                yield to_16k(q.get(timeout=0.5), rate)
            except queue.Empty:
                continue


def file_chunks(path: str | Path, track: int = 0, start: float = 0.0,
                duration: float | None = None) -> Iterator[np.ndarray]:
    """Decode one audio track of a recording to 16 kHz mono, from `start` seconds."""
    import av

    end = start + duration if duration else None
    with av.open(str(path)) as container:
        stream = container.streams.audio[track]
        resampler = av.AudioResampler(format="flt", layout="mono", rate=RATE)
        if start:
            container.seek(int(start / stream.time_base), stream=stream)
        for frame in container.decode(stream):
            if frame.time is not None:
                if frame.time < start:
                    continue
                if end is not None and frame.time > end:
                    break
            for out in resampler.resample(frame):
                yield out.to_ndarray().reshape(-1)
        for out in resampler.resample(None):
            yield out.to_ndarray().reshape(-1)


# --- main loops -------------------------------------------------------------


def run(
    listener: Listener,
    transcriber: Transcriber,
    chunks: Iterator[np.ndarray],
    live: bool,
    hotwords: Callable[[], str],
    offset_s: float = 0.0,
    is_speaking: Callable[[], bool] = lambda: False,
) -> None:
    """VAD on this thread; Whisper on a worker so the mic never backs up.

    Offline runs (live=False) transcribe inline so output follows the file.
    """
    vad, seg = StreamingVad(), Segmenter()
    work: queue.Queue = queue.Queue()

    def transcribe(audio: np.ndarray, at: str) -> None:
        started = time.monotonic()
        text = transcriber(audio, hotwords())
        took = time.monotonic() - started
        log.debug("%.1fs of audio -> text in %.2fs", len(audio) / RATE, took)
        listener.handle(text, at)

    def worker() -> None:
        while (item := work.get()) is not None:
            try:
                transcribe(*item)
            except Exception:
                log.exception("transcription failed")

    thread = threading.Thread(target=worker, daemon=True) if live else None
    if thread:
        thread.start()

    def dispatch(audio: np.ndarray | None, n: int) -> None:
        if audio is None:
            return
        if live:
            if is_speaking():
                log.debug("dropped an utterance while ARIA was talking")
                return
            work.put((audio, datetime.now().strftime("%H:%M:%S")))
        else:
            t = offset_s + n * FRAME_S - len(audio) / RATE
            transcribe(audio, f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:04.1f}")

    n = 0
    try:
        for frame in frames_from(chunks):
            n += 1
            dispatch(seg.feed(frame, vad(frame)), n)
        dispatch(seg.flush(), n)
    finally:
        if thread:
            work.put(None)
            thread.join(timeout=30)
