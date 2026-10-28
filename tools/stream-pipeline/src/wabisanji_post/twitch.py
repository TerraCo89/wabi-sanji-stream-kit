"""Twitch Helix client for the post-stream pipeline: login, VOD lookup, clips, markers.

Clip From VOD (open beta, checked against the API reference on 2026-10-05):
``POST https://api.twitch.tv/helix/videos/clips`` with query parameters
``editor_id``, ``broadcaster_id``, ``vod_id``, ``vod_offset`` (integer, where
the clip ENDS), ``duration`` (5-60 s, 0.1 s precision) and ``title``
(required, AutoMod-checked). Needs a user token with ``channel:manage:clips``.
Returns 202: the clip is made asynchronously, so we poll Get Clips for its URL
and real ``vod_offset`` (where it STARTS). Beta clips have been reported to land
a few seconds late, so the caller records the drift.

Login is the authorization-code flow with a one-shot local server on
``http://localhost:17563/callback`` (registered on the Twitch app). The token
pair is kept in ``twitch.token.json`` at the repo root (gitignored, ``*.token.json``).
"""
from __future__ import annotations

import datetime as dt
import http.server
import json
import secrets as pysecrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Callable, Optional

import requests

from . import secrets


HELIX = "https://api.twitch.tv/helix"
ID_URL = "https://id.twitch.tv/oauth2"
REDIRECT_PORT = 17563
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
CORE_SCOPES = ("channel:manage:clips", "user:read:broadcast")  # clips and markers: Helix() needs these
FOLLOWERS_SCOPE = "moderator:read:followers"  # stream-metrics: follows per day (Get Channel Followers)
SCOPES = (*CORE_SCOPES, FOLLOWERS_SCOPE)  # what `stream-auth twitch` asks for
TOKEN_FILE = secrets.REPO_ROOT / "twitch.token.json"


class TwitchError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def login(open_browser: bool = True, timeout: float = 300.0, say: Callable[[str], None] = print) -> dict:
    """Authorization-code flow. Blocks until the streamer approves in the browser (or timeout)."""
    creds = secrets.require("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET")
    state = pysecrets.token_urlsafe(16)
    url = f"{ID_URL}/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": creds["TWITCH_CLIENT_ID"], "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES), "state": state, "force_verify": "true",
    })
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] != state:
                result["error"] = "state mismatch"
            elif "error" in q:
                result["error"] = q.get("error_description", q["error"])[0]
            elif "code" in q:
                result["code"] = q["code"][0]
            ok = "code" in result
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Wabi Sanji pipeline: Twitch login done. You can close this tab." if ok else \
                f"Twitch login failed: {result.get('error', 'no code')}"
            self.wfile.write(f"<html><body style='font-family:sans-serif'><h2>{msg}</h2></body></html>".encode())

        def log_message(self, *args):  # keep the console clean
            pass

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), Handler)
    server.timeout = 1.0
    say(f"Opening Twitch to approve: {', '.join(SCOPES)}")
    say(f"If the browser doesn't open, visit: {url}")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    deadline = time.monotonic() + timeout
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "code" not in result:
        raise TwitchError(f"login did not complete: {result.get('error', 'timed out')}")

    r = requests.post(f"{ID_URL}/token", data={
        "client_id": creds["TWITCH_CLIENT_ID"], "client_secret": creds["TWITCH_CLIENT_SECRET"],
        "code": result["code"], "grant_type": "authorization_code", "redirect_uri": REDIRECT_URI,
    }, timeout=30)
    if not r.ok:
        raise TwitchError(f"token exchange failed ({r.status_code}): {r.json().get('message', '')}")
    _save_token(r.json())
    return validate()


def _save_token(data: dict) -> None:
    data = {k: data[k] for k in ("access_token", "refresh_token", "scope", "expires_in") if k in data}
    data["obtained_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    TOKEN_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")


def _load_token() -> Optional[dict]:
    return json.loads(TOKEN_FILE.read_text(encoding="utf-8")) if TOKEN_FILE.exists() else None


def _refresh(tok: dict) -> dict:
    creds = secrets.require("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET")
    r = requests.post(f"{ID_URL}/token", data={
        "client_id": creds["TWITCH_CLIENT_ID"], "client_secret": creds["TWITCH_CLIENT_SECRET"],
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
    }, timeout=30)
    if not r.ok:
        raise TwitchError("refresh failed: run `stream-auth twitch` again")
    _save_token(r.json())
    return _load_token()


def validate() -> dict:
    """``{login, user_id, scopes, expires_in}`` for the stored token, refreshing it if needed."""
    tok = _load_token()
    if not tok:
        raise TwitchError("not logged in: run `uv run python pipeline.py stream-auth twitch`")
    for attempt in (0, 1):
        r = requests.get(f"{ID_URL}/validate", headers={"Authorization": f"OAuth {tok['access_token']}"}, timeout=20)
        if r.ok:
            return r.json()
        if attempt == 0 and r.status_code == 401 and tok.get("refresh_token"):
            tok = _refresh(tok)
    raise TwitchError("stored Twitch token is invalid: run `stream-auth twitch` again")


# ---------------------------------------------------------------------------
# Helix
# ---------------------------------------------------------------------------

class Helix:
    """Helix calls as the logged-in broadcaster (user token), refreshing on 401."""

    def __init__(self) -> None:
        self.client_id = secrets.require("TWITCH_CLIENT_ID")["TWITCH_CLIENT_ID"]
        info = validate()
        self.scopes = set(info.get("scopes", []))
        missing = [s for s in CORE_SCOPES if s not in self.scopes]
        if missing:
            raise TwitchError(f"token lacks {missing}: run `stream-auth twitch` again")
        self.user_id = info["user_id"]
        self.login = info["login"]
        expected = secrets.get("TWITCH_BROADCASTER_ID")
        if expected and expected != self.user_id:
            raise TwitchError(f"logged in as {self.login} ({self.user_id}), not broadcaster {expected}")

    def _call(self, method: str, path: str, **kw) -> requests.Response:
        for attempt in (0, 1):
            tok = _load_token()
            headers = {"Client-Id": self.client_id, "Authorization": f"Bearer {tok['access_token']}"}
            r = requests.request(method, f"{HELIX}{path}", headers=headers, timeout=30, **kw)
            if r.status_code == 401 and attempt == 0:
                _refresh(tok)
                continue
            if r.status_code == 429:
                reset = float(r.headers.get("Ratelimit-Reset", time.time() + 5))
                time.sleep(max(1.0, reset - time.time()))
                continue
            return r
        return r

    def video(self, vod_id: str) -> Optional[dict]:
        r = self._call("GET", "/videos", params={"id": vod_id})
        if r.status_code == 404:
            return None
        if not r.ok:
            raise TwitchError(f"Get Videos failed ({r.status_code}): {r.text[:200]}")
        data = r.json().get("data", [])
        return data[0] if data else None

    def create_clip_from_vod(self, vod_id: str, vod_offset_end: int, duration: float, title: str) -> dict:
        r = self._call("POST", "/videos/clips", params={
            "editor_id": self.user_id, "broadcaster_id": self.user_id, "vod_id": vod_id,
            "vod_offset": vod_offset_end, "duration": f"{duration:.1f}", "title": title,
        })
        if r.status_code not in (200, 202):
            raise TwitchError(f"Create Clip From VOD failed ({r.status_code}): {r.text[:300]}")
        data = r.json().get("data", [])
        if not data:
            raise TwitchError(f"Create Clip From VOD returned no clip: {r.text[:200]}")
        return data[0]  # {id, edit_url}

    def clip(self, clip_id: str) -> Optional[dict]:
        r = self._call("GET", "/clips", params={"id": clip_id})
        if not r.ok:
            raise TwitchError(f"Get Clips failed ({r.status_code}): {r.text[:200]}")
        data = r.json().get("data", [])
        return data[0] if data else None

    def clips(self, clip_ids: list[str]) -> dict[str, dict]:
        """``{id: clip}`` (Get Clips, up to 100 ids a call); deleted clips are left out."""
        out: dict[str, dict] = {}
        for i in range(0, len(clip_ids), 100):
            r = self._call("GET", "/clips", params=[("id", c) for c in clip_ids[i:i + 100]])
            if not r.ok:
                raise TwitchError(f"Get Clips failed ({r.status_code}): {r.text[:200]}")
            out.update({c["id"]: c for c in r.json().get("data", [])})
        return out

    def wait_for_clip(self, clip_id: str, timeout: float = 90.0) -> Optional[dict]:
        """Poll Get Clips until the asynchronously created clip shows up."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            c = self.clip(clip_id)
            if c:
                return c
            time.sleep(4)
        return None

    def followers(self) -> list[dict]:
        """Every current follower, newest first: ``[{user_id, user_login, user_name, followed_at}]``.
        Needs ``moderator:read:followers``; people who have since unfollowed aren't listed."""
        if FOLLOWERS_SCOPE not in self.scopes:
            raise TwitchError(f"token lacks {FOLLOWERS_SCOPE}: run `stream-auth twitch` again")
        out: list[dict] = []
        cursor: Optional[str] = None
        while True:
            params = {"broadcaster_id": self.user_id, "first": 100}
            if cursor:
                params["after"] = cursor
            r = self._call("GET", "/channels/followers", params=params)
            if not r.ok:
                raise TwitchError(f"Get Channel Followers failed ({r.status_code}): {r.text[:200]}")
            body = r.json()
            out += body.get("data", [])
            cursor = (body.get("pagination") or {}).get("cursor")
            if not cursor or not body.get("data"):
                return out

    def stream(self) -> Optional[dict]:
        """The live stream (``viewer_count``, ``game_name``, ``title``, ``started_at``), or None when offline."""
        r = self._call("GET", "/streams", params={"user_id": self.user_id})
        if not r.ok:
            raise TwitchError(f"Get Streams failed ({r.status_code}): {r.text[:200]}")
        data = r.json().get("data", [])
        return data[0] if data else None

    def markers(self, vod_id: str) -> list[dict]:
        r = self._call("GET", "/streams/markers", params={"video_id": vod_id, "first": 100})
        if r.status_code == 404:
            return []
        if not r.ok:
            raise TwitchError(f"Get Stream Markers failed ({r.status_code}): {r.text[:200]}")
        out = []
        for d in r.json().get("data", []):
            for v in d.get("videos", []):
                out += v.get("markers", [])
        return out


def vod_created_at(video: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(video["created_at"].replace("Z", "+00:00"))
