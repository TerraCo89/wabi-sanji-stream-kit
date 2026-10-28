"""Tiny HTTP client for the ARIA routes on `wabisanji serve` (:8770)."""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class LiveClient:
    def __init__(self, base_url: str, timeout: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def post(self, path: str, body: dict | None = None) -> dict | None:
        """POST and return the JSON reply; None (logged) if the server is down or says no."""
        try:
            r = requests.post(f"{self.base_url}{path}", json=body or {}, timeout=self.timeout)
        except requests.RequestException as e:
            log.warning("overlay server not reachable (%s): is `wabisanji serve` running?", e)
            return None
        if not r.ok:
            log.warning("POST %s -> %s %s", path, r.status_code, r.text[:200])
            return None
        return r.json()

    def get(self, path: str) -> dict | None:
        try:
            r = requests.get(f"{self.base_url}{path}", timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("GET %s failed: %s", path, e)
            return None
        return r.json()

    def card(self) -> dict | None:
        data = self.get("/api/live/card")
        return data.get("card") if data else None
