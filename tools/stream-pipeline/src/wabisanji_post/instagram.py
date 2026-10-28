"""Instagram Graph API client ("Instagram API with Facebook Login"): post approved Shorts as Reels.

The Instagram account is a Creator account linked to the Wabi Sanji Facebook Page, so every
call goes through graph.facebook.com with the Page access token. Reels use Meta's resumable
upload (``upload_type=resumable`` + ``rupload.facebook.com``): the mp4 goes straight from disk,
nothing is hosted publicly. Instagram has no private or scheduled state, so a Reel is public
the moment ``media_publish`` returns. Only ``stream-social --go`` calls :func:`publish_reel`.

Login is Facebook Login with a one-shot loopback server on ``http://localhost:17565/``
(``META_APP_ID`` / ``META_APP_SECRET`` in ``.env``; ``META_LOGIN_CONFIG_ID`` if the app uses
Facebook Login for Business). The short-lived user token is swapped for a long-lived one, and
the Page token taken from that never expires. It is kept with the Page and Instagram IDs in
``instagram.token.json`` at the repo root (gitignored, ``*.token.json``).
"""
from __future__ import annotations

import datetime as dt
import http.server
import json
import secrets as pysecrets
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Callable, Optional

import requests

from . import secrets


VERSION = "v26.0"
GRAPH = f"https://graph.facebook.com/{VERSION}"
RUPLOAD = f"https://rupload.facebook.com/ig-api-upload/{VERSION}"
DIALOG = f"https://www.facebook.com/{VERSION}/dialog/oauth"
REDIRECT_PORT = 17565
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/"
SCOPES = ("instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement",
          "business_management",  # business_management: Pages that sit in a Business portfolio
          "instagram_manage_insights")  # stream-metrics: views, reach, saves, shares per Reel
PUBLISH_SCOPES = ("instagram_basic", "instagram_content_publish")
INSIGHTS_SCOPE = "instagram_manage_insights"
# Reel insights for stream-metrics. "plays" was retired for "views" in Graph v22.
REEL_INSIGHTS = ("views", "reach", "saved", "shares", "total_interactions", "ig_reels_avg_watch_time")
TOKEN_FILE = secrets.REPO_ROOT / "instagram.token.json"
# Meta documents 300 MB for Reels, but on 2026-10-14 the resumable upload failed with
# ProcessingFailedError on every Short over 110 MB, and on 85-88 MB copies sent in 32 MB chunks
# (at byte 64 or 96 MiB). Every file up to 82 MB sent in one request worked. So: one request,
# and Shorts over UPLOAD_MAX get a smaller copy first.
UPLOAD_MAX = 80_000_000
COPY_TARGET = 60_000_000
POLL_SECONDS = 10.0
POLL_LIMIT = 60  # 10 minutes; Meta transcodes a 30 s Reel in well under that


# Graph error codes where carrying on with the next Reel is pointless: bad or expired login,
# missing permission, or a rate limit (9 is the 24 h publishing limit).
STOP_CODES = frozenset({4, 9, 10, 17, 32, 190, 200, 613})


class InstagramError(RuntimeError):
    def __init__(self, msg: str, code: Optional[int] = None):
        super().__init__(msg)
        self.code = code

    @property
    def stop(self) -> bool:
        return self.code in STOP_CODES


def _app() -> dict:
    return secrets.require("META_APP_ID", "META_APP_SECRET")


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

def _error(r: requests.Response) -> str:
    try:
        err = r.json().get("error", {})
    except ValueError:
        return r.text[:200]
    if not isinstance(err, dict):
        return str(err)
    msg = err.get("error_user_msg") or err.get("message", "")
    code = err.get("code")
    sub = err.get("error_subcode")
    return msg + (f" [code {code}" + (f"/{sub}" if sub else "") + "]" if code else "")


def _code(r: requests.Response) -> Optional[int]:
    try:
        return r.json().get("error", {}).get("code")
    except (ValueError, AttributeError):
        return None


def _call(method: str, path: str, token: str, timeout: float = 60, **params) -> dict:
    r = requests.request(method, f"{GRAPH}/{path.lstrip('/')}", timeout=timeout,
                         params={**params, "access_token": token})
    if not r.ok:
        raise InstagramError(f"{path} failed ({r.status_code}): {_error(r)}", _code(r))
    return r.json()


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def _wait_for_code(url: str, state: str, open_browser: bool, timeout: float, say: Callable[[str], None]) -> str:
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not q:  # favicon and friends
                self.send_response(204)
                self.end_headers()
                return
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
            msg = "Wabi Sanji pipeline: Instagram login done. You can close this tab." if ok else \
                f"Instagram login failed: {result.get('error', 'no code')}"
            self.wfile.write(f"<html><body style='font-family:sans-serif'><h2>{msg}</h2></body></html>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), Handler)
    server.timeout = 1.0
    say("Opening Facebook to approve Instagram publishing for the Wabi Sanji Page")
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
        raise InstagramError(f"login did not complete: {result.get('error', 'timed out')}")
    return result["code"]


def login(user_token: Optional[str] = None, open_browser: bool = True, timeout: float = 300.0,
          say: Callable[[str], None] = print) -> dict:
    """Facebook Login, or ``user_token`` pasted from Graph API Explorer. Saves the Page token
    and IDs. Returns :func:`whoami`."""
    app = _app()
    if user_token is None:
        state = pysecrets.token_urlsafe(16)
        q = {"client_id": app["META_APP_ID"], "redirect_uri": REDIRECT_URI, "state": state,
             "response_type": "code"}
        config_id = secrets.get("META_LOGIN_CONFIG_ID")
        if config_id:  # Facebook Login for Business: permissions come from the configuration
            q["config_id"] = config_id
        else:
            q["scope"] = ",".join(SCOPES)
        code = _wait_for_code(DIALOG + "?" + urllib.parse.urlencode(q), state, open_browser, timeout, say)
        r = requests.get(f"{GRAPH}/oauth/access_token", timeout=30, params={
            "client_id": app["META_APP_ID"], "client_secret": app["META_APP_SECRET"],
            "redirect_uri": REDIRECT_URI, "code": code})
        if not r.ok:
            raise InstagramError(f"token exchange failed ({r.status_code}): {_error(r)}")
        user_token = r.json()["access_token"]

    r = requests.get(f"{GRAPH}/oauth/access_token", timeout=30, params={
        "grant_type": "fb_exchange_token", "client_id": app["META_APP_ID"],
        "client_secret": app["META_APP_SECRET"], "fb_exchange_token": user_token})
    if not r.ok:
        raise InstagramError(f"long-lived token exchange failed ({r.status_code}): {_error(r)}")
    long_user = r.json()["access_token"]

    pages = _call("GET", "me/accounts", long_user,
                  fields="id,name,access_token,instagram_business_account{id,username}").get("data", [])
    page = _pick_page(pages, secrets.get("INSTAGRAM_PAGE"))
    ig = page["instagram_business_account"]
    TOKEN_FILE.write_text(json.dumps({
        "page_id": page["id"], "page_name": page["name"], "page_token": page["access_token"],
        "ig_user_id": ig["id"], "ig_username": ig.get("username", ""),
        "obtained_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }, indent=1), encoding="utf-8")
    return whoami()


def _pick_page(pages: list[dict], want: str = "") -> dict:
    """The Page with a linked Instagram account. ``want`` (INSTAGRAM_PAGE: name or id) picks
    between several."""
    linked = [p for p in pages if p.get("instagram_business_account")]
    if want:
        linked = [p for p in linked if want in (p["id"], p["name"])]
    if len(linked) == 1:
        return linked[0]
    names = ", ".join(f"{p['name']} ({p['id']})" for p in pages) or "none"
    if not linked:
        raise InstagramError(f"no Page with a linked Instagram account{f' matching {want!r}' if want else ''} "
                             f"was granted. Pages granted: {names}")
    raise InstagramError(f"several Pages have Instagram linked ({names}): set INSTAGRAM_PAGE in .env "
                         "to the Wabi Sanji Page's name or id")


def _load_token() -> Optional[dict]:
    return json.loads(TOKEN_FILE.read_text(encoding="utf-8")) if TOKEN_FILE.exists() else None


def _token() -> dict:
    tok = _load_token()
    if not tok:
        raise InstagramError("not logged in: run `uv run python pipeline.py stream-auth instagram`")
    return tok


def whoami() -> dict:
    tok = _token()
    me = _call("GET", tok["ig_user_id"], tok["page_token"], fields="id,username")
    return {"ig_user_id": me["id"], "username": me.get("username", tok.get("ig_username", "")),
            "page_id": tok["page_id"], "page_name": tok["page_name"]}


def check() -> dict:
    """Read-only health check: ``{whoami..., valid, expires_at, data_access_expires_at, scopes,
    missing_scopes, quota_used, quota_total, problems}``."""
    tok = _token()
    app = _app()
    info = whoami()
    dbg = _call("GET", "debug_token", f"{app['META_APP_ID']}|{app['META_APP_SECRET']}",
                input_token=tok["page_token"]).get("data", {})
    scopes = dbg.get("scopes", [])
    out = {**info, "valid": bool(dbg.get("is_valid")), "expires_at": dbg.get("expires_at", 0),
           "data_access_expires_at": dbg.get("data_access_expires_at", 0), "scopes": scopes,
           "missing_scopes": [s for s in PUBLISH_SCOPES if s not in scopes],
           "insights": INSIGHTS_SCOPE in scopes}
    out.update(publishing_quota())
    problems = []
    if not out["valid"]:
        problems.append("token is not valid: run `stream-auth instagram` again")
    if out["expires_at"]:
        problems.append("the Page token expires (it came from a short-lived user token): log in again")
    if out["missing_scopes"]:
        problems.append(f"missing permissions {', '.join(out['missing_scopes'])}: log in again and approve them")
    dax = out["data_access_expires_at"]
    if dax and dax - time.time() < 14 * 86400:
        problems.append("Meta's data access window ends within 14 days: run `stream-auth instagram` again")
    out["problems"] = problems
    return out


def publishing_quota() -> dict:
    """``{quota_used, quota_total}`` over Instagram's rolling 24 hours (API-published posts)."""
    tok = _token()
    data = _call("GET", f"{tok['ig_user_id']}/content_publishing_limit", tok["page_token"],
                 fields="config,quota_usage").get("data", [{}])
    d = data[0] if data else {}
    return {"quota_used": int(d.get("quota_usage", 0)),
            "quota_total": int((d.get("config") or {}).get("quota_total", 100))}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def recent_media(limit: int = 50) -> list[dict]:
    """Newest first: ``[{id, caption, permalink, timestamp, media_product_type}]``."""
    tok = _token()
    data = _call("GET", f"{tok['ig_user_id']}/media", tok["page_token"],
                 fields="id,caption,permalink,timestamp,media_product_type", limit=limit)
    return data.get("data", [])


def media_metrics(media_id: str, insights: bool = True) -> dict:
    """``{timestamp, likes, comments}`` plus, when ``insights``, the :data:`REEL_INSIGHTS` that Meta
    returns (``ig_reels_avg_watch_time`` is in ms). A missing permission raises with its Graph code
    (10 or 200); a metric Meta rejects for this media (code 100) is skipped."""
    tok = _token()
    m = _call("GET", media_id, tok["page_token"], fields="timestamp,like_count,comments_count")
    out = {"timestamp": m.get("timestamp"), "likes": m.get("like_count"), "comments": m.get("comments_count")}
    if not insights:
        return out

    def read(metrics) -> dict:
        data = _call("GET", f"{media_id}/insights", tok["page_token"], metric=",".join(metrics)).get("data", [])
        return {d["name"]: (d.get("values") or [{}])[0].get("value") for d in data}

    try:
        out.update(read(REEL_INSIGHTS))
    except InstagramError as exc:
        if exc.code != 100:
            raise
        for metric in REEL_INSIGHTS:  # one at a time: keep the ones Meta has for this Reel
            try:
                out.update(read([metric]))
            except InstagramError as one:
                if one.code != 100:
                    raise
    return out


def same_caption(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


# ---------------------------------------------------------------------------
# Publish (public straight away)
# ---------------------------------------------------------------------------

def _upload(container_id: str, path: Path, token: str, size: int) -> None:
    for attempt in range(3):  # on a dropped connection or 5xx, send the whole file again
        try:
            with path.open("rb") as fh:
                r = requests.post(f"{RUPLOAD}/{container_id}", data=fh, timeout=(30, 300),
                                  headers={"Authorization": f"OAuth {token}", "offset": "0",
                                           "file_size": str(size), "Content-Type": "application/octet-stream"})
        except requests.RequestException as exc:
            err = f"upload of {path.name} dropped ({exc.__class__.__name__})"
        else:
            body = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
            if r.ok and body.get("success", True):
                return
            err = f"upload of {path.name} failed ({r.status_code}): {_error(r) or body}"
            if r.status_code < 500 and r.status_code != 429:
                raise InstagramError(err, _code(r))
        time.sleep(5 * (attempt + 1))
    raise InstagramError(err + "; rerun to try again")


def _wait(container_id: str, token: str, sleep: Callable[[float], None] = time.sleep) -> None:
    for _ in range(POLL_LIMIT):
        st = _call("GET", container_id, token, fields="status_code,status")
        code = st.get("status_code")
        if code in ("FINISHED", "PUBLISHED"):
            return
        if code in ("ERROR", "EXPIRED"):
            raise InstagramError(f"Instagram couldn't process the video ({code}): {st.get('status', '')}")
        sleep(POLL_SECONDS)
    raise InstagramError("Instagram is still processing the video after 10 minutes; rerun later "
                         "(the unpublished container expires on its own)")


def fit_for_upload(path: Path, out_dir: Path) -> Path:
    """``path`` if it's small enough to upload, otherwise a re-encoded copy in ``out_dir`` (reused
    if it's already there and newer). The copy keeps the audio and is sized for COPY_TARGET;
    Instagram re-encodes Reels far below these bitrates anyway. CPU (libx264), not NVENC, so it
    never competes with OBS for the GPU."""
    if path.stat().st_size <= UPLOAD_MAX:
        return path
    out = out_dir / path.name
    if out.exists() and out.stat().st_mtime >= path.stat().st_mtime and out.stat().st_size <= UPLOAD_MAX:
        return out
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(path)], capture_output=True, text=True, check=True).stdout.strip())
    kbps = int(COPY_TARGET * 8 / dur / 1000) - 256  # leave room for the audio and container
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
                    "-c:v", "libx264", "-preset", "medium", "-profile:v", "high", "-pix_fmt", "yuv420p",
                    "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.3)}k", "-bufsize", f"{kbps * 2}k",
                    "-c:a", "copy", "-movflags", "+faststart", str(out)], check=True)
    if out.stat().st_size > UPLOAD_MAX:
        raise InstagramError(f"the Instagram copy of {path.name} is still {out.stat().st_size / 1e6:.0f} MB")
    return out


def publish_reel(path: Path, caption: str, cover_ms: Optional[int] = None,
                 on_stage: Callable[[str], None] = lambda s: None,
                 sleep: Callable[[float], None] = time.sleep) -> dict:
    """Create a REELS container, upload ``path``, wait for processing, publish.
    Returns ``{media_id, container_id}``. The Reel is PUBLIC when this returns."""
    size = path.stat().st_size
    if size > UPLOAD_MAX:
        raise InstagramError(f"{path.name} is {size / 1e6:.0f} MB; Instagram's upload fails on big files "
                             "(use fit_for_upload first)")
    tok = _token()
    token, ig = tok["page_token"], tok["ig_user_id"]
    params = {"media_type": "REELS", "upload_type": "resumable", "caption": caption, "share_to_feed": "true"}
    if cover_ms is not None:
        params["thumb_offset"] = str(int(cover_ms))
    on_stage("creating container")
    container = _call("POST", f"{ig}/media", token, **params)["id"]
    on_stage(f"uploading {size / 1e6:.0f} MB")
    _upload(container, path, token, size)
    on_stage("waiting for Instagram to process it")
    _wait(container, token, sleep=sleep)
    on_stage("publishing")
    media = _call("POST", f"{ig}/media_publish", token, creation_id=container)["id"]
    return {"media_id": media, "container_id": container}


def permalink(media_id: str) -> str:
    tok = _token()
    return _call("GET", media_id, tok["page_token"], fields="permalink").get("permalink", "")
