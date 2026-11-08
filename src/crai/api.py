"""Async client for the official Clash Royale API (https://developer.clashroyale.com)."""

import asyncio
import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Self, cast
from urllib.parse import quote

lazy import httpx

from .models import ApiBattle, ApiPlayer, CardList, ItemsPage, RankedPlayer, Season

log = logging.getLogger(__name__)

GLOBAL_LOCATION_ID = 57000000
_TAG_CHARS = frozenset("0289PYLQGRJCUV")


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


@dataclass(slots=True)
class _Key:
    """One API key: its own concurrency lane, backoff and health."""

    index: int
    token: str
    sem: asyncio.Semaphore
    load: int = 0  # requests assigned to this key: waiting for a slot + in flight
    cooldown_until: float = 0.0
    backoff: float = 0.0
    disabled: str | None = None
    requests: int = 0
    throttled: int = 0


class ClashClient:
    """Async client that spreads requests over one or more API keys.

    Each key gets `concurrency` parallel requests. A key that gets a 429 cools down on its own
    (with exponential backoff) while the others keep working; a key that gets a 403 is disabled,
    and only when every key is disabled does the 403 become fatal.
    """

    def __init__(
        self,
        tokens: str | Sequence[str],
        base_url: str,
        *,
        concurrency: int = 8,
        max_retries: int = 8,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        toks = [tokens] if isinstance(tokens, str) else [t for t in tokens if t]
        if not toks:
            raise ValueError("At least one API token is required.")
        self._keys = [_Key(i, t, asyncio.Semaphore(concurrency)) for i, t in enumerate(toks)]
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Accept": "application/json"},
            timeout=timeout,
            transport=transport,
            limits=httpx.Limits(max_connections=concurrency * len(toks) + 4),
        )
        self._max_retries = max_retries

    @property
    def requests(self) -> int:
        return sum(k.requests for k in self._keys)

    @property
    def throttled(self) -> int:
        return sum(k.throttled for k in self._keys)

    def key_stats(self) -> list[dict[str, object]]:
        return [
            {"key": k.index + 1, "requests": k.requests, "throttled_429": k.throttled}
            | ({"disabled": k.disabled} if k.disabled else {})
            for k in self._keys
        ]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _pick(self) -> _Key:
        """The live, non-cooling key with the fewest requests in flight (waits if all cool)."""
        while True:
            live = [k for k in self._keys if not k.disabled]
            if not live:
                reasons = "; ".join(f"key {k.index + 1}: {k.disabled}" for k in self._keys)
                raise ClashAPIError(403, "(all keys)", f"every API key was rejected ({reasons})")
            now = asyncio.get_running_loop().time()
            if ready := [k for k in live if k.cooldown_until <= now]:
                # Count load at assignment, not after the semaphore: otherwise every request queued
                # while all keys are busy sees equal loads and piles onto the first key.
                key = min(ready, key=lambda k: (k.load, k.requests))
                key.load += 1
                return key
            await asyncio.sleep(min(k.cooldown_until for k in live) - now)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a path. Returns parsed JSON, or None on 404. Retries 429/5xx/transport errors."""
        last, attempts, throttles = "", 0, 0
        while attempts < self._max_retries and throttles < 4 * self._max_retries:
            delay = min(2**attempts, 60) + random.random()
            key = await self._pick()
            try:
                async with key.sem:
                    key.requests += 1
                    resp = await self._client.get(
                        path, params=params, headers={"Authorization": f"Bearer {key.token}"}
                    )
            except httpx.TransportError as e:
                key.load -= 1
                attempts += 1
                last = f"{type(e).__name__}: {e}"
                log.debug("%s on %s, retrying in %.1fs", last, path, delay)
                await asyncio.sleep(delay)
                continue
            except BaseException:
                key.load -= 1
                raise
            key.load -= 1
            match resp.status_code:
                case 200:
                    key.backoff = 0.0
                    return resp.json()
                case 404:
                    return None
                case 429:
                    # Rate-limited: rest this key and retry immediately on another one.
                    throttles += 1
                    key.throttled += 1
                    key.backoff = min(max(key.backoff * 2, 0.5), 30.0)
                    retry_after = resp.headers.get("Retry-After", "")
                    rest = float(retry_after) if retry_after.isdigit() else key.backoff
                    key.cooldown_until = asyncio.get_running_loop().time() + rest
                    last = "HTTP 429"
                    log.debug("429 on key %d, resting %.1fs", key.index + 1, rest)
                case 500 | 502 | 503 | 504:
                    attempts += 1
                    last = f"HTTP {resp.status_code}"
                    log.debug("%s on %s, retrying in %.1fs", last, path, delay)
                    await asyncio.sleep(delay)
                case 403 if len(self._keys) > 1:
                    if not key.disabled:
                        key.disabled = resp.text[:120]
                        log.warning("API key %d rejected (403), disabling it", key.index + 1)
                case status:
                    raise ClashAPIError(status, path, resp.text)
        raise ClashAPIError(-1, path, f"gave up after {attempts + throttles} attempts ({last})")

    @staticmethod
    def _tag(tag: str) -> str:
        return quote(normalize_tag(tag), safe="")

    async def player(self, tag: str) -> ApiPlayer | None:
        return cast(ApiPlayer | None, await self.get(f"/players/{self._tag(tag)}"))

    async def battlelog(self, tag: str) -> list[ApiBattle]:
        return cast(list[ApiBattle], await self.get(f"/players/{self._tag(tag)}/battlelog") or [])

    async def cards(self) -> CardList:
        return cast(CardList, await self.get("/cards") or {})

    async def top_players(self, limit: int = 200, location: str = "global") -> list[RankedPlayer]:
        """Top ladder players. Tries Path of Legend (current season), then trophy rankings,
        then the most recent finished Path of Legend season."""
        locs: list[str | int] = (
            [GLOBAL_LOCATION_ID, "global"] if location == "global" else [location]
        )
        paths = [f"/locations/{loc}/pathoflegend/players" for loc in locs]
        paths += [f"/locations/{loc}/rankings/players" for loc in locs]
        for path in paths:
            if items := await self._items(path, limit):
                log.info("top players from %s (%d)", path, len(items))
                return cast(list[RankedPlayer], items)
        seasons = cast(
            list[Season],
            await self._items("/locations/global/seasonsV2", 100)
            or await self._items("/locations/global/seasons", 100),
        )
        codes = sorted((c for s in seasons if (c := s.get("code") or s.get("id"))), reverse=True)
        for code in codes[:3]:
            path = f"/locations/global/pathoflegend/{code}/rankings/players"
            if items := await self._items(path, limit):
                log.info("top players from PoL season %s (%d)", code, len(items))
                return cast(list[RankedPlayer], items)
        return []

    async def _items(self, path: str, limit: int) -> list[Any]:
        try:
            data = cast(ItemsPage[Any] | None, await self.get(path, {"limit": limit}))
        except ClashAPIError as e:
            if e.status in (400, 404):
                return []
            raise
        return (data or {}).get("items") or []
