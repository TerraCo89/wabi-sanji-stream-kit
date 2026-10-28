"""Folder watcher: analyze new game screenshots as they appear."""

from __future__ import annotations

import fnmatch
import logging
import time
from pathlib import Path

from watchdog.events import FileCreatedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .analyzer import analyze
from .anki import AnkiClient, AnkiConnectError, sync_vocabulary
from .config import Config
from .db import Database

logger = logging.getLogger(__name__)


def process_file(path: str, config: Config, db: Database, source: str | None = None) -> dict:
    """Analyze one screenshot, save it, and optionally push new vocab to Anki."""
    if db.screenshot_seen(path):
        logger.info("Skipping %s (already processed)", path)
        return {"skipped": True, "file_path": path}

    result = analyze(path, method=config.ocr_method, ollama=config.ollama)
    if result.get("error"):
        logger.error("Analysis failed for %s: %s", path, result["error"])
        return result

    db.save_analysis(result, source_name=source)
    logger.info(
        "Saved %s: %d text segment(s), %d vocab word(s)",
        Path(path).name,
        len(result.get("extracted_text", [])),
        len(result.get("vocabulary", [])),
    )

    if config.anki.auto_sync:
        try:
            synced, failed = sync_vocabulary(db, AnkiClient(config.anki))
            if synced or failed:
                logger.info("Anki sync: %d pushed, %d failed", synced, failed)
        except AnkiConnectError as e:
            logger.warning("Anki sync skipped: %s", e)

    return result


def pending_screenshots(config: Config, db: Database) -> list[str]:
    """Screenshots in config.screenshot_dir (and extra_screenshot_dirs) not yet
    in the capture log, oldest first."""
    dirs = [Path(d) for d in [config.screenshot_dir, *config.extra_screenshot_dirs] if d]
    files = [
        p for d in dirs if d.is_dir() for p in d.iterdir()
        if p.is_file() and any(fnmatch.fnmatch(p.name.lower(), pat.lower()) for pat in config.file_patterns)
    ]
    files.sort(key=lambda p: p.stat().st_mtime)
    return [str(p) for p in files if not db.screenshot_seen(p)]


class ScreenshotHandler(FileSystemEventHandler):
    def __init__(self, config: Config, db: Database, source: str | None = None):
        self.config = config
        self.db = db
        self.source = source
        self.processed: set[str] = set()

    def matches(self, filename: str) -> bool:
        return any(fnmatch.fnmatch(filename.lower(), p.lower()) for p in self.config.file_patterns)

    def on_created(self, event: FileCreatedEvent) -> None:
        if event.is_directory:
            return
        path = str(event.src_path)
        if not self.matches(Path(path).name) or path in self.processed:
            return
        self.processed.add(path)
        time.sleep(0.5)  # let the file finish writing
        try:
            result = process_file(path, self.config, self.db, source=self.source)
            _print_summary(result)
        except Exception:
            logger.exception("Error processing %s", path)


def _print_summary(result: dict) -> None:
    if result.get("skipped"):
        return
    if result.get("error"):
        print(f"  error: {result['error']}")
        return
    for seg in result.get("extracted_text", []):
        print(f"  {seg.get('text', '')}")
    if result.get("translation"):
        print(f"  -> {result['translation']}")
    vocab = result.get("vocabulary", [])
    if vocab:
        words = ", ".join(v.get("word", "") for v in vocab)
        print(f"  vocab: {words}")


def watch(config: Config, source: str | None = None) -> None:
    """Block and process screenshots as they land in config.screenshot_dir."""
    watch_dir = Path(config.screenshot_dir)
    if not watch_dir.is_dir():
        raise FileNotFoundError(
            f"Screenshot directory not found: {watch_dir} (set screenshot_dir in config.yaml)"
        )

    db = Database(config.db_path)
    handler = ScreenshotHandler(config, db, source=source)
    observer = Observer()
    observer.schedule(handler, str(watch_dir), recursive=False)
    observer.start()

    print(f"Watching {watch_dir} (method: {config.ocr_method}, Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping.")
        observer.stop()
    observer.join()
    db.close()
