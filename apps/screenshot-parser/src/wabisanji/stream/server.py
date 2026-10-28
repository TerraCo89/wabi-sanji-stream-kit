"""FastAPI app for stream mode: overlay pages and JSON API.

The server only reads the capture log. Each request opens its own read-only
connection, so `wabisanji ingest` in another process can keep writing.

ARIA's widgets (/memes-overlay, /aria) are driven by POSTs from `wabisanji
listen` and `wabisanji live ...`; that state lives in memory (LiveState).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import session
from ..config import LiveConfig
from ..db import connect_readonly, recent_words, today_counts
from ..live.memes import load_index
from .live import LiveState
from .state import StreamState

STATIC_DIR = Path(__file__).parent / "static"
NO_STORE = {"Cache-Control": "no-store"}


class MemeIn(BaseModel):
    id: str
    via: str = "phrase"  # phrase (listener) | claude | manual


class CardIn(BaseModel):
    id: str
    heard: str = ""


class AnswerIn(BaseModel):
    id: str
    hint: str
    steps: list[str] = Field(default_factory=list)
    source_name: str = ""
    source_url: str = ""
    heard: str | None = None


def create_app(
    db_path: str | Path,
    state: StreamState | None = None,
    live_config: LiveConfig | None = None,
    live: LiveState | None = None,
) -> FastAPI:
    state = state or StreamState()
    live_config = live_config or LiveConfig()
    live = live or LiveState()
    app = FastAPI(title="Wabi Sanji stream", docs_url=None, redoc_url=None)
    app.state.stream = state
    app.state.live = live

    def current_source(override: str | None) -> str | None:
        """?source= wins, then --source, then the session's current game."""
        return override or state.source or session.load().get("source")

    def read(query, *args):
        try:
            conn = connect_readonly(db_path)
        except sqlite3.OperationalError as e:
            raise HTTPException(503, f"capture log unavailable: {e}")
        try:
            return query(conn, *args)
        finally:
            conn.close()

    @app.get("/health")
    def health() -> dict:
        try:
            read(lambda conn: conn.execute("SELECT 1 FROM screenshots LIMIT 1").fetchall())
            ok = True
        except HTTPException:
            ok = False
        return {"ok": ok, **state.status(), "source": current_source(None)}

    @app.get("/api/words")
    def words(limit: int = Query(8, ge=1, le=50), source: str | None = None) -> dict:
        src = current_source(source)
        return {"source": src, "words": read(recent_words, limit, src)}

    @app.get("/api/today")
    def today(source: str | None = None) -> dict:
        return read(today_counts, current_source(source))

    @app.get("/api/state")
    def overlay_state() -> dict:
        return {"overlay_hidden": state.status()["overlay_hidden"]}

    @app.get("/overlay", include_in_schema=False)
    def overlay() -> FileResponse:
        return FileResponse(STATIC_DIR / "overlay.html", headers=NO_STORE)

    # --- ARIA: memes ------------------------------------------------------

    def meme_file(name: str) -> Path:
        root = live_config.memes_path.resolve()
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, f"no meme file {name!r} in {root}")
        return path

    @app.post("/api/live/meme")
    def show_meme(body: MemeIn) -> dict:
        meme = load_index(live_config.index_path).get(body.id)
        if not meme:
            raise HTTPException(404, f"unknown meme id {body.id!r}")
        meme_file(meme.file)
        return live.push_meme(id=meme.id, url=f"/memes/{quote(meme.file)}", via=body.via,
                              kind=meme.kind, caption=meme.caption)

    @app.get("/api/live/memes")
    def memes_since(since: int | None = None) -> dict:
        return live.memes_since(since)

    @app.get("/memes/{name:path}", include_in_schema=False)
    def meme_image(name: str) -> FileResponse:
        return FileResponse(meme_file(name))

    @app.get("/memes-overlay", include_in_schema=False)
    def memes_overlay() -> FileResponse:
        return FileResponse(STATIC_DIR / "memes.html", headers=NO_STORE)

    # --- ARIA: answer card ------------------------------------------------

    @app.get("/api/live/card")
    def card() -> dict:
        return live.card()

    @app.post("/api/live/listening")
    def listening(body: CardIn) -> dict:
        return live.set_card("listening", id=body.id, heard="")

    @app.post("/api/live/thinking")
    def thinking(body: CardIn) -> dict:
        return live.set_card("thinking", id=body.id, heard=body.heard)

    @app.post("/api/live/answer")
    def answer(body: AnswerIn) -> dict:
        fields = body.model_dump(exclude_none=True)
        fields["steps"] = [s for s in fields["steps"] if s.strip()]
        return live.set_card("hint", **fields)

    @app.post("/api/live/expand")
    def expand() -> dict:
        expanded = live.expand()
        if expanded is None:
            raise HTTPException(409, "no answer with steps on screen")
        return expanded

    @app.post("/api/live/clear")
    def clear() -> dict:
        live.clear()
        return {"ok": True}

    @app.get("/aria", include_in_schema=False)
    def aria() -> FileResponse:
        return FileResponse(STATIC_DIR / "aria.html", headers=NO_STORE)

    return app
