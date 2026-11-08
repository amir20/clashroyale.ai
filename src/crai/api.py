"""Async client for the official Clash Royale API (https://developer.clashroyale.com)."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Self
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

GLOBAL_LOCATION_ID = 57000000
_TAG_CHARS = set("0289PYLQGRJCUV")


def normalize_tag(tag: str) -> str:
    """'abc123', '#ABC123' -> '#ABC123'. The letter O is a common typo for zero."""
    t = tag.strip().upper().lstrip("#").replace("O", "0")
    if not t or not set(t) <= _TAG_CHARS:
        raise ValueError(f"Invalid player tag: {tag!r}")
    return "#" + t


class ClashAPIError(RuntimeError):
    def __init__(self, status: int, path: str, body: str) -> None:
        self.status = status
        hint = ""
        if status == 403:
            hint = (
                " (403: token invalid or your public IP isn't whitelisted on the key."
                " See README for the RoyaleAPI proxy option.)"
            )
        super().__init__(f"HTTP {status} for {path}: {body[:300]}{hint}")


class ClashClient:
    def __init__(
        self,
        token: str,
        base_url: str,
        *,
        concurrency: int = 8,
        max_retries: int = 6,
        timeout: float = 30.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._max_retries = max_retries
        self.requests = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        """GET a path. Returns parsed JSON, or None on 404. Retries 429/5xx/transport errors."""
        last = ""
        for attempt in range(self._max_retries):
            delay = min(2**attempt, 60) + random.random()
            try:
                async with self._sem:
                    self.requests += 1
                    resp = await self._client.get(path, params=params)
            except httpx.TransportError as e:
                last = f"{type(e).__name__}: {e}"
                log.debug("%s on %s, retrying in %.1fs", last, path, delay)
                await asyncio.sleep(delay)
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return None
            if resp.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, float(retry_after))
                log.debug("%s on %s, retrying in %.1fs", last, path, delay)
                await asyncio.sleep(delay)
                continue
            raise ClashAPIError(resp.status_code, path, resp.text)
        raise ClashAPIError(
            -1, path, f"gave up after {self._max_retries} attempts ({last})"
        )

    @staticmethod
    def _tag(tag: str) -> str:
        return quote(normalize_tag(tag), safe="")

    async def player(self, tag: str) -> dict[str, Any] | None:
        return await self.get(f"/players/{self._tag(tag)}")

    async def battlelog(self, tag: str) -> list[dict[str, Any]]:
        return await self.get(f"/players/{self._tag(tag)}/battlelog") or []

    async def cards(self) -> dict[str, Any]:
        return await self.get("/cards") or {}

    async def top_players(
        self, limit: int = 200, location: str | int = "global"
    ) -> list[dict[str, Any]]:
        """Top ladder players. Tries Path of Legend (current season), then trophy rankings,
        then the most recent finished Path of Legend season."""
        loc_ids = [location] if location != "global" else [GLOBAL_LOCATION_ID, "global"]
        paths = [f"/locations/{loc}/pathoflegend/players" for loc in loc_ids]
        paths += [f"/locations/{loc}/rankings/players" for loc in loc_ids]
        for path in paths:
            items = await self._items(path, limit)
            if items:
                log.info("top players from %s (%d)", path, len(items))
                return items
        seasons = await self._items(
            "/locations/global/seasonsV2", 100
        ) or await self._items("/locations/global/seasons", 100)
        codes = [s.get("code") or s.get("id") for s in seasons]
        for code in sorted((c for c in codes if c), reverse=True)[:3]:
            items = await self._items(
                f"/locations/global/pathoflegend/{code}/rankings/players", limit
            )
            if items:
                log.info("top players from PoL season %s (%d)", code, len(items))
                return items
        return []

    async def _items(self, path: str, limit: int) -> list[dict[str, Any]]:
        try:
            data = await self.get(path, {"limit": limit})
        except ClashAPIError as e:
            if e.status in (400, 404):
                return []
            raise
        return (data or {}).get("items") or []
