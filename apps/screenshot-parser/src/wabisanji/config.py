"""Configuration loading for the Wabi Sanji screenshot parser."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

APP_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = APP_ROOT / "config.yaml"


@dataclass
class OllamaConfig:
    url: str = "http://localhost:11434"
    model: str = "qwen3-vl:8b"  # must be vision-capable
    timeout: int = 300


@dataclass
class AnkiConfig:
    url: str = "http://127.0.0.1:8765"
    deck: str = "WabiSanji"  # base deck; cards land in category subdecks
    note_type: str = "WabiSanji Vocab"
    auto_sync: bool = True
    tag: str = "wabisanji"


@dataclass
class ObsConfig:
    host: str = "localhost"
    port: int = 4455
    password_env: str = "OBS_WS_PASSWORD"  # env var name; the password never goes in yaml
    source: str = "Game Capture"
    only_scenes: list[str] = field(default_factory=lambda: ["Gameplay"])  # [] = always


@dataclass
class CaptureConfig:
    enabled: bool = True
    interval_ms: int = 500
    settle_ms: int = 350
    change_threshold: float = 6.0
    dup_hash_distance: int = 6
    min_ink: float = 0.005
    max_ink: float = 0.25
    max_per_minute: int = 20
    prefix: str = "obs-"
    region: list[float] = field(default_factory=lambda: [0.0, 0.70, 1.0, 0.30])  # x, y, w, h
    regions: dict[str, list[float]] = field(default_factory=dict)  # per-game, keyed by source

    def region_for(self, source: str | None) -> list[float]:
        return self.regions.get(source or "", self.region)


@dataclass
class HotkeysConfig:
    scan: str = "<ctrl>+<alt>+s"
    toggle_overlay: str = "<ctrl>+<alt>+o"


@dataclass
class LiveConfig:
    """ARIA: `wabisanji listen` (mic -> CPU Whisper) and the meme/answer widgets."""

    mic: str | int | None = None  # sounddevice name substring or index; None = default input
    model: str = "base.en"  # faster-whisper model, always on CPU (the GPU belongs to OBS)
    cpu_threads: int = 4
    wake_words: list[str] = field(
        default_factory=lambda: ["aria", "arya", "ariya", "ariah", "aaria", "area"]
    )
    memes_dir: str = ""  # images; WABISANJI_MEMES overrides
    memes_index: str = "../../content/stream/memes.yaml"  # relative to the app folder
    meme_gap_s: int = 20  # minimum gap between any two memes
    claude_meme_gap_s: int = 60  # Claude's picks wait this long after any meme
    transcript_batch_s: int = 45  # how often `live watch` hands Claude new speech
    speaker_bot: str = "ws://127.0.0.1:7580/"
    voice: str = "ARIA"  # Speaker.bot voice alias

    @property
    def memes_path(self) -> Path:
        return Path(os.environ.get("WABISANJI_MEMES") or self.memes_dir)

    @property
    def index_path(self) -> Path:
        p = Path(self.memes_index)
        return p if p.is_absolute() else (APP_ROOT / p).resolve()


@dataclass
class StreamConfig:
    port: int = 8770  # AnkiConnect owns 8765
    obs: ObsConfig = field(default_factory=ObsConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    live_ocr: str = "manga-ocr"  # manga-ocr | none
    hotkeys: HotkeysConfig = field(default_factory=HotkeysConfig)
    live: LiveConfig = field(default_factory=LiveConfig)


@dataclass
class Config:
    screenshot_dir: str = ""
    # More folders `pending` scans (e.g. OBS's screenshot folder for PC games)
    extra_screenshot_dirs: list[str] = field(default_factory=list)
    file_patterns: list[str] = field(default_factory=lambda: ["*.png", "*.jpg", "*.jpeg"])
    database_path: str = "data/wabisanji.db"
    ocr_method: str = "hybrid"
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    anki: AnkiConfig = field(default_factory=AnkiConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)

    @property
    def db_path(self) -> Path:
        p = Path(self.database_path)
        return p if p.is_absolute() else APP_ROOT / p


def load_config(path: str | Path | None = None) -> Config:
    """Load config.yaml, falling back to defaults for missing keys."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    anki_data = data.get("anki", {}) or {}
    ollama_data = data.get("ollama", {}) or {}
    return Config(
        screenshot_dir=data.get("screenshot_dir", ""),
        extra_screenshot_dirs=data.get("extra_screenshot_dirs") or [],
        file_patterns=data.get("file_patterns", ["*.png", "*.jpg", "*.jpeg"]),
        database_path=data.get("database_path", "data/wabisanji.db"),
        ocr_method=data.get("ocr_method", "hybrid"),
        ollama=OllamaConfig(
            url=ollama_data.get("url", "http://localhost:11434"),
            model=ollama_data.get("model", "qwen3-vl:8b"),
            timeout=ollama_data.get("timeout", 300),
        ),
        anki=AnkiConfig(
            url=anki_data.get("url", "http://127.0.0.1:8765"),
            deck=anki_data.get("deck", "WabiSanji"),
            note_type=anki_data.get("note_type", "WabiSanji Vocab"),
            auto_sync=anki_data.get("auto_sync", True),
            tag=anki_data.get("tag", "wabisanji"),
        ),
        stream=_load_stream(data.get("stream", {}) or {}),
    )


def _pick(cls, data: dict):
    """Build a flat dataclass from `data`, ignoring unknown keys; missing keys use defaults."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def _load_stream(data: dict) -> StreamConfig:
    defaults = StreamConfig()
    return StreamConfig(
        port=data.get("port", defaults.port),
        obs=_pick(ObsConfig, data.get("obs", {}) or {}),
        capture=_pick(CaptureConfig, data.get("capture", {}) or {}),
        live_ocr=data.get("live_ocr", defaults.live_ocr),
        hotkeys=_pick(HotkeysConfig, data.get("hotkeys", {}) or {}),
        live=_pick(LiveConfig, data.get("live", {}) or {}),
    )
