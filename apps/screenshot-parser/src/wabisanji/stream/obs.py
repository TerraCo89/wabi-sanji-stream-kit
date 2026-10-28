"""Refresh this server's OBS browser sources when `wabisanji serve` starts.

OBS loads a browser source once. After a reboot OBS often opens before the
server, the sources get an error page, and they never try again. This presses
each one's "Refresh cache of current page" button through obs-websocket v5.

The password comes from the OBS_WS_PASSWORD env var (stream.obs.password_env),
else from obs-websocket's own config on this PC. It's never printed or stored.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

from ..config import ObsConfig

LOCAL_HOSTS = {"localhost", "127.0.0.1"}


class ObsError(RuntimeError):
    pass


def is_ours(url: str, port: int) -> bool:
    """A browser source pointing at this server (overlay, memes, ARIA card)."""
    try:
        u = urlparse(url)
        return u.hostname in LOCAL_HOSTS and u.port == port
    except ValueError:
        return False


def password(cfg: ObsConfig) -> str | None:
    pw = os.environ.get(cfg.password_env)
    if pw:
        return pw
    path = Path(os.environ.get("APPDATA", "")) / "obs-studio/plugin_config/obs-websocket/config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("server_password") or None
    except (OSError, ValueError):
        return None


def auth_string(pw: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((pw + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


def refresh_sources(cfg: ObsConfig, port: int, timeout: float = 3.0) -> list[str]:
    """Names of the browser sources refreshed. Raises ObsError when OBS can't be reached."""
    import websockets

    try:
        return asyncio.run(asyncio.wait_for(_refresh(cfg, port, timeout), timeout * 4))
    except ObsError:
        raise
    except (OSError, asyncio.TimeoutError) as e:
        raise ObsError("OBS isn't running, or its websocket server is off") from e
    except websockets.exceptions.ConnectionClosed as e:
        if e.rcvd and e.rcvd.code == 4009:
            raise ObsError(f"OBS rejected the password (set {cfg.password_env})") from e
        raise ObsError(f"OBS closed the connection ({e})") from e


async def _refresh(cfg: ObsConfig, port: int, timeout: float) -> list[str]:
    import websockets

    async with websockets.connect(f"ws://{cfg.host}:{cfg.port}", open_timeout=timeout,
                                  max_size=None) as ws:
        hello = json.loads(await ws.recv())["d"]
        identify: dict = {"rpcVersion": 1, "eventSubscriptions": 0}
        if auth := hello.get("authentication"):
            pw = password(cfg)
            if not pw:
                raise ObsError(f"OBS wants a password: set {cfg.password_env}")
            identify["authentication"] = auth_string(pw, auth["salt"], auth["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify}))
        if json.loads(await ws.recv()).get("op") != 2:
            raise ObsError("OBS didn't accept the login")

        async def call(request: str, data: dict | None = None) -> dict:
            rid = uuid.uuid4().hex
            await ws.send(json.dumps({"op": 6, "d": {"requestType": request, "requestId": rid,
                                                    "requestData": data or {}}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("op") == 7 and msg["d"]["requestId"] == rid:
                    status = msg["d"]["requestStatus"]
                    if not status["result"]:
                        raise ObsError(f"{request}: {status.get('comment') or status['code']}")
                    return msg["d"].get("responseData") or {}

        refreshed = []
        for item in (await call("GetInputList", {"inputKind": "browser_source"}))["inputs"]:
            name = item["inputName"]
            settings = (await call("GetInputSettings", {"inputName": name}))["inputSettings"]
            url = settings.get("url") or ""
            if url and "://" not in url and is_ours(f"http://{url}", port):
                # "localhost:8770/aria" typed without http:// never loads in OBS
                url = f"http://{url}"
                await call("SetInputSettings", {"inputName": name, "inputSettings": {"url": url}})
                name += " (added http://)"
            elif is_ours(url, port):
                await call("PressInputPropertiesButton",
                           {"inputName": name, "propertyName": "refreshnocache"})
            else:
                continue
            refreshed.append(name)
        return refreshed
