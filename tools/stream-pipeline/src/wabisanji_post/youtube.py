"""YouTube Data API client: find the streamer's uploads by exact title, and upload approved videos.

Reads use ``youtube.readonly``. Uploads (``videos.insert``, resumable), thumbnails and
playlist inserts use ``youtube.upload`` and ``youtube``. Until the project passes the
YouTube API audit, API uploads are locked private by YouTube, so everything here
uploads as private and the streamer makes videos public in Studio.

Login is the installed-app (desktop) OAuth flow with a one-shot loopback server on
``http://localhost:17564``. The OAuth client JSON (Google Cloud console, "Desktop app",
YouTube Data API v3 enabled) is at the path in ``YOUTUBE_CLIENT_SECRETS``. The token
pair is kept in ``youtube.token.json`` at the repo root (gitignored, ``*.token.json``).

Uploads are listed from the channel's uploads playlist (``channels.list mine=true`` →
``playlistItems.list``), which includes private and scheduled videos for the owner.
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


API = "https://www.googleapis.com/youtube/v3"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT_PORT = 17564
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3"
READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
UPLOAD_SCOPES = ("https://www.googleapis.com/auth/youtube.upload",  # videos.insert, thumbnails.set
                 "https://www.googleapis.com/auth/youtube")          # playlistItems.insert
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"  # stream-metrics: watch time, retention
SCOPES = (READ_SCOPE, *UPLOAD_SCOPES, ANALYTICS_SCOPE)
ANALYTICS_API = "https://youtubeanalytics.googleapis.com/v2/reports"
# Thumbnail impressions and CTR aren't in the Analytics API: only in the Reporting API's daily bulk
# reports, which YouTube starts writing once a job exists (same yt-analytics.readonly scope). The
# API has to be enabled in the Google Cloud project first.
REPORTING_API = "https://youtubereporting.googleapis.com/v1"
REACH_REPORT = "channel_reach_basic_a1"
CHUNK = 32 * 1024 * 1024  # resumable upload chunk; a multiple of 256 KiB
TOKEN_FILE = secrets.REPO_ROOT / "youtube.token.json"


class YouTubeError(RuntimeError):
    pass


def _client() -> dict:
    path = Path(secrets.require("YOUTUBE_CLIENT_SECRETS")["YOUTUBE_CLIENT_SECRETS"]).expanduser()
    if not path.exists():
        raise YouTubeError(f"YOUTUBE_CLIENT_SECRETS points at a missing file ({path.name})")
    data = json.loads(path.read_text(encoding="utf-8"))
    c = data.get("installed") or data.get("web")
    if not c:
        raise YouTubeError("client JSON has no 'installed' section: create a Desktop app OAuth client")
    return {"client_id": c["client_id"], "client_secret": c["client_secret"]}


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def login(open_browser: bool = True, timeout: float = 300.0, say: Callable[[str], None] = print) -> dict:
    """Authorization-code flow. Blocks until the streamer approves in the browser (or timeout)."""
    c = _client()
    state = pysecrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": c["client_id"], "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES), "state": state, "access_type": "offline", "prompt": "consent",
    })
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
                result["error"] = q["error"][0]
            elif "code" in q:
                result["code"] = q["code"][0]
            ok = "code" in result
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Wabi Sanji pipeline: YouTube login done. You can close this tab." if ok else \
                f"YouTube login failed: {result.get('error', 'no code')}"
            self.wfile.write(f"<html><body style='font-family:sans-serif'><h2>{msg}</h2></body></html>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), Handler)
    server.timeout = 1.0
    say(f"Opening Google to approve YouTube access: {', '.join(x.rsplit('/', 1)[-1] for x in SCOPES)}")
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
        raise YouTubeError(f"login did not complete: {result.get('error', 'timed out')}")

    r = requests.post(TOKEN_URL, data={
        **c, "code": result["code"], "grant_type": "authorization_code", "redirect_uri": REDIRECT_URI,
    }, timeout=30)
    if not r.ok:
        raise YouTubeError(f"token exchange failed ({r.status_code}): {r.json().get('error', '')}")
    _save_token(r.json())
    return whoami()


def _save_token(data: dict, keep_refresh: Optional[str] = None) -> None:
    data = {k: data[k] for k in ("access_token", "refresh_token", "scope", "expires_in") if k in data}
    if "refresh_token" not in data and keep_refresh:
        data["refresh_token"] = keep_refresh  # Google doesn't resend it on refresh
    data["obtained_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    TOKEN_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")


def _load_token() -> Optional[dict]:
    return json.loads(TOKEN_FILE.read_text(encoding="utf-8")) if TOKEN_FILE.exists() else None


def _refresh(tok: dict) -> dict:
    r = requests.post(TOKEN_URL, data={
        **_client(), "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
    }, timeout=30)
    if not r.ok:
        raise YouTubeError("refresh failed: run `stream-auth youtube` again")
    _save_token(r.json(), keep_refresh=tok["refresh_token"])
    return _load_token()


def _token() -> dict:
    tok = _load_token()
    if not tok:
        raise YouTubeError("not logged in: run `uv run python pipeline.py stream-auth youtube`")
    try:  # refresh a few minutes early: long uploads outlive the 1 h access token
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(tok["obtained_at"])).total_seconds()
        if age > int(tok.get("expires_in", 3600)) - 300 and tok.get("refresh_token"):
            tok = _refresh(tok)
    except (KeyError, ValueError):
        pass
    return tok


def has_scopes(*scopes: str) -> bool:
    granted = set((_load_token() or {}).get("scope", "").split())
    return all(s in granted for s in scopes)


def _error(r: requests.Response) -> str:
    try:
        err = r.json().get("error", {})
    except ValueError:
        return r.text[:200]
    if not isinstance(err, dict):
        return str(err)
    reasons = ", ".join(e.get("reason", "") for e in err.get("errors", []) if e.get("reason"))
    return err.get("message", "") + (f" [{reasons}]" if reasons else "")


def _request(method: str, url: str, headers: Optional[dict] = None, timeout: float = 60, **kw) -> requests.Response:
    """One authorised call, refreshing the token once on a 401."""
    tok = _token()
    for attempt in (0, 1):
        r = requests.request(method, url, timeout=timeout,
                             headers={**(headers or {}), "Authorization": f"Bearer {tok['access_token']}"}, **kw)
        if r.status_code == 401 and attempt == 0 and tok.get("refresh_token"):
            tok = _refresh(tok)
            continue
        return r
    return r


def _get(path: str, params: dict) -> dict:
    r = _request("GET", f"{API}/{path}", params=params, timeout=30)
    if not r.ok:
        raise YouTubeError(f"{path} failed ({r.status_code}): {_error(r)}")
    return r.json()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _my_channel() -> dict:
    items = _get("channels", {"part": "snippet,contentDetails", "mine": "true"}).get("items", [])
    if not items:
        raise YouTubeError("this Google account has no YouTube channel")
    return items[0]


def whoami() -> dict:
    ch = _my_channel()
    return {"channel_id": ch["id"], "title": ch["snippet"]["title"]}


def uploads(limit: int = 200) -> list[dict]:
    """Newest uploads first: ``[{video_id, title, privacy, published_at}]``, private ones included."""
    playlist = _my_channel()["contentDetails"]["relatedPlaylists"]["uploads"]
    out: list[dict] = []
    page: Optional[str] = None
    while len(out) < limit:
        params = {"part": "snippet,status", "playlistId": playlist, "maxResults": 50}
        if page:
            params["pageToken"] = page
        data = _get("playlistItems", params)
        for it in data.get("items", []):
            sn = it["snippet"]
            out.append({"video_id": sn["resourceId"]["videoId"], "title": sn["title"],
                        "privacy": it.get("status", {}).get("privacyStatus", ""),
                        "published_at": sn.get("publishedAt", "")})
        page = data.get("nextPageToken")
        if not page:
            break
    return out[:limit]


# ---------------------------------------------------------------------------
# Writes (need UPLOAD_SCOPES)
# ---------------------------------------------------------------------------

def _range_end(r: requests.Response) -> int:
    """Bytes YouTube already has, from a 308's ``Range: bytes=0-N`` (0 if none yet)."""
    rng = r.headers.get("Range")
    return int(rng.rsplit("-", 1)[1]) + 1 if rng else 0


def upload_video(path: Path, title: str, description: str, tags: list[str], category_id: str = "20",
                 publish_at: Optional[dt.datetime] = None, public_now: bool = False,
                 progress: Optional[Callable[[int, int], None]] = None) -> str:
    """Resumable ``videos.insert`` as PRIVATE, scheduled to go public at ``publish_at`` if given,
    or PUBLIC straight away with ``public_now`` (only when the streamer asks for it).
    Returns the video ID. Resumes after network errors."""
    size = path.stat().st_size
    status = {"privacyStatus": "public" if public_now else "private", "selfDeclaredMadeForKids": False}
    if publish_at is not None and not public_now:
        status["publishAt"] = publish_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    body = {"snippet": {"title": title[:100], "description": description[:5000], "tags": tags,
                        "categoryId": category_id},
            "status": status}
    r = _request("POST", f"{UPLOAD_API}/videos", params={"uploadType": "resumable", "part": "snippet,status"},
                 json=body, headers={"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"})
    if not r.ok or "Location" not in r.headers:
        raise YouTubeError(f"videos.insert failed to start ({r.status_code}): {_error(r)}")
    session_url = r.headers["Location"]
    sent, failures = 0, 0
    with path.open("rb") as fh:
        while True:
            fh.seek(sent)
            chunk = fh.read(CHUNK)
            # (connect, read/send) timeouts: a socket op that makes no progress for 2 min drops to the status
            # query below. A chunk normally spends most of its time in sendall; that is sending, not a hang.
            try:
                r = _request("PUT", session_url, data=chunk, timeout=(30, 120),
                             headers={"Content-Range": f"bytes {sent}-{sent + len(chunk) - 1}/{size}"})
            except requests.RequestException:
                r = None
            if r is not None and r.status_code in (200, 201):
                if progress:
                    progress(size, size)
                return r.json()["id"]
            if r is not None and r.status_code == 308:
                sent, failures = _range_end(r), 0
                if progress:
                    progress(sent, size)
                continue
            if r is not None and r.status_code < 500 and r.status_code not in (408, 429):
                raise YouTubeError(f"upload of {path.name} failed ({r.status_code}): {_error(r)}")
            failures += 1  # 5xx, 408, 429 or a dropped connection: ask where we are, then resume
            if failures > 8:
                raise YouTubeError(f"upload of {path.name} kept failing; rerun to start it again")
            time.sleep(min(60, 2 ** failures))
            try:
                q = _request("PUT", session_url, headers={"Content-Range": f"bytes */{size}"})
            except requests.RequestException:
                continue
            if q.status_code in (200, 201):
                return q.json()["id"]
            if q.status_code == 308:
                sent = _range_end(q)


def schedule_existing(video_id: str, publish_at: dt.datetime) -> None:
    """Schedule one of my private videos to go public at ``publish_at`` (videos.update, 50 units)."""
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
              "publishAt": publish_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    r = _request("PUT", f"{API}/videos", params={"part": "status"}, json={"id": video_id, "status": status})
    if not r.ok:
        raise YouTubeError(f"videos.update failed ({r.status_code}): {_error(r)}")


def make_public(video_id: str) -> None:
    """Make one of my private videos public now (videos.update, 50 units). Only when the streamer asks."""
    status = {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    r = _request("PUT", f"{API}/videos", params={"part": "status"}, json={"id": video_id, "status": status})
    if not r.ok:
        raise YouTubeError(f"videos.update failed ({r.status_code}): {_error(r)}")


def video_description(video_id: str) -> Optional[str]:
    items = _get("videos", {"part": "snippet", "id": video_id}).get("items", [])
    return items[0]["snippet"].get("description", "") if items else None


def update_description(video_id: str, description: str) -> None:
    """Replace one of my videos' description, keeping the rest of its snippet (videos.update, 50 units)."""
    items = _get("videos", {"part": "snippet", "id": video_id}).get("items", [])
    if not items:
        raise YouTubeError(f"video {video_id} not found")
    sn = items[0]["snippet"]
    snippet = {k: sn[k] for k in ("title", "categoryId", "tags", "defaultLanguage", "defaultAudioLanguage") if k in sn}
    snippet["description"] = description
    r = _request("PUT", f"{API}/videos", params={"part": "snippet"}, json={"id": video_id, "snippet": snippet})
    if not r.ok:
        raise YouTubeError(f"videos.update failed ({r.status_code}): {_error(r)}")


def set_thumbnail(video_id: str, image: Path) -> None:
    data = image.read_bytes()
    if len(data) > 2 * 1024 * 1024:
        raise YouTubeError(f"thumbnail {image.name} is over YouTube's 2 MB limit")
    r = _request("POST", f"{UPLOAD_API}/thumbnails/set", params={"videoId": video_id, "uploadType": "media"},
                 data=data, headers={"Content-Type": "image/jpeg"}, timeout=120)
    if not r.ok:
        raise YouTubeError(f"thumbnails.set failed ({r.status_code}): {_error(r)}")


def video_status(video_ids: list[str]) -> dict[str, dict]:
    """``{id: {privacy, publish_at, upload, processing}}`` for my videos. ``upload`` is YouTube's
    uploadStatus: ``uploaded`` is reported from the START of an upload (YouTube creates the video
    when the upload session opens), so it can't tell "still uploading" from "processing"; only
    ``processed`` means the file is complete. Also failed, rejected, deleted;
    ``processing`` is processingDetails.processingStatus. ``publish_at`` is set on scheduled ones."""
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        data = _get("videos", {"part": "status,processingDetails", "id": ",".join(video_ids[i:i + 50]),
                               "maxResults": 50})
        for v in data.get("items", []):
            st = v.get("status", {})
            out[v["id"]] = {"privacy": st.get("privacyStatus", ""), "publish_at": st.get("publishAt"),
                            "upload": st.get("uploadStatus", ""),
                            "processing": v.get("processingDetails", {}).get("processingStatus", "")}
    return out


def video_stats(video_ids: list[str]) -> dict[str, dict]:
    """Public counts for my videos: ``{id: {title, published_at, privacy, publish_at, duration,
    views, likes, comments}}``. Needs only ``youtube.readonly``."""
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        data = _get("videos", {"part": "snippet,statistics,status,contentDetails",
                               "id": ",".join(video_ids[i:i + 50]), "maxResults": 50})
        for v in data.get("items", []):
            st, sn, n = v.get("status", {}), v.get("snippet", {}), v.get("statistics", {})
            out[v["id"]] = {"title": sn.get("title", ""), "published_at": sn.get("publishedAt"),
                            "privacy": st.get("privacyStatus", ""), "publish_at": st.get("publishAt"),
                            "duration": v.get("contentDetails", {}).get("duration", ""),
                            **{k: int(n[f]) for k, f in (("views", "viewCount"), ("likes", "likeCount"),
                                                         ("comments", "commentCount")) if f in n}}
    return out


ANALYTICS_METRICS = ("views", "engagedViews", "estimatedMinutesWatched", "averageViewDuration",
                     "averageViewPercentage", "subscribersGained", "likes", "comments", "shares")


def analytics_by_video(video_ids: list[str], start: dt.date, end: dt.date) -> dict[str, dict]:
    """YouTube Analytics totals per video over ``start``..``end`` (needs ``ANALYTICS_SCOPE``).
    Data lags about two days. Metrics YouTube rejects for this channel are dropped and retried."""
    metrics = list(ANALYTICS_METRICS)
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 200):
        batch = video_ids[i:i + 200]
        while True:
            r = _request("GET", ANALYTICS_API, timeout=60, params={
                "ids": "channel==MINE", "startDate": start.isoformat(), "endDate": end.isoformat(),
                "metrics": ",".join(metrics), "dimensions": "video", "filters": "video==" + ",".join(batch),
                "sort": "-views", "maxResults": 200})
            if r.status_code == 400 and "engagedViews" in metrics:
                metrics.remove("engagedViews")  # newer metric; not every channel or query supports it
                continue
            if not r.ok:
                raise YouTubeError(f"youtubeAnalytics reports failed ({r.status_code}): {_error(r)}")
            break
        data = r.json()
        names = [h["name"] for h in data.get("columnHeaders", [])]
        for row in data.get("rows", []):
            rec = dict(zip(names, row))
            out[rec.pop("video")] = rec
    return out


def reporting_job(report_type: str = REACH_REPORT, create: bool = True) -> Optional[str]:
    """This channel's Reporting API job for ``report_type``, created if there isn't one. Once a job
    exists YouTube writes one report a day; the first appear within about two days."""
    r = _request("GET", f"{REPORTING_API}/jobs", timeout=30)
    if not r.ok:
        raise YouTubeError(f"reporting jobs failed ({r.status_code}): {_error(r)}")
    for job in r.json().get("jobs", []):
        if job.get("reportTypeId") == report_type:
            return job["id"]
    if not create:
        return None
    r = _request("POST", f"{REPORTING_API}/jobs", timeout=30,
                 json={"reportTypeId": report_type, "name": f"wabisanji {report_type}"})
    if not r.ok:
        raise YouTubeError(f"creating the {report_type} job failed ({r.status_code}): {_error(r)}")
    return r.json()["id"]


def reporting_reports(job_id: str) -> list[dict]:
    """Every report the job has (``id``, ``startTime``, ``createTime``, ``downloadUrl``)."""
    out: list[dict] = []
    token = None
    while True:
        r = _request("GET", f"{REPORTING_API}/jobs/{job_id}/reports", timeout=30,
                     params={"pageToken": token} if token else None)
        if not r.ok:
            raise YouTubeError(f"listing reports failed ({r.status_code}): {_error(r)}")
        data = r.json()
        out += data.get("reports", [])
        token = data.get("nextPageToken")
        if not token:
            return out


def download_report(url: str) -> str:
    r = _request("GET", url, timeout=120)
    if not r.ok:
        raise YouTubeError(f"report download failed ({r.status_code}): {_error(r)}")
    return r.content.decode("utf-8")


def my_playlists() -> list[dict]:
    """``[{id, title}]`` for every playlist on the channel (private ones included)."""
    out, page = [], None
    while True:
        params = {"part": "snippet", "mine": "true", "maxResults": 50}
        if page:
            params["pageToken"] = page
        data = _get("playlists", params)
        out += [{"id": p["id"], "title": p["snippet"]["title"]} for p in data.get("items", [])]
        page = data.get("nextPageToken")
        if not page:
            return out


def playlist_video_ids(playlist_id: str) -> list[str]:
    """Video IDs in one of my playlists, in playlist order."""
    out, page = [], None
    while True:
        params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50}
        if page:
            params["pageToken"] = page
        data = _get("playlistItems", params)
        out += [it["contentDetails"]["videoId"] for it in data.get("items", [])]
        page = data.get("nextPageToken")
        if not page:
            return out


def create_playlist(title: str, description: str = "", privacy: str = "public") -> str:
    """``playlists.insert`` (50 units). Returns the new playlist ID."""
    if privacy not in ("public", "unlisted", "private"):
        raise YouTubeError(f"playlist privacy must be public, unlisted or private, not {privacy!r}")
    body = {"snippet": {"title": title[:150], "description": description[:5000]},
            "status": {"privacyStatus": privacy}}
    r = _request("POST", f"{API}/playlists", params={"part": "snippet,status"}, json=body)
    if not r.ok:
        raise YouTubeError(f"playlists.insert failed ({r.status_code}): {_error(r)}")
    return r.json()["id"]


def add_to_playlist(video_id: str, playlist_id: str) -> None:
    body = {"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
    r = _request("POST", f"{API}/playlistItems", params={"part": "snippet"}, json=body)
    if not r.ok:
        raise YouTubeError(f"playlistItems.insert failed ({r.status_code}): {_error(r)}")


__all__ = ["YouTubeError", "login", "whoami", "uploads", "has_scopes", "upload_video", "set_thumbnail",
           "my_playlists", "playlist_video_ids", "create_playlist", "add_to_playlist", "UPLOAD_SCOPES",
           "TOKEN_FILE", "ANALYTICS_SCOPE", "video_stats", "analytics_by_video", "reporting_job",
           "reporting_reports", "download_report", "REACH_REPORT"]
