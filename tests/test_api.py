"""Multi-key client: spreading load, per-key 429 cooldown, per-key 403 disabling."""

import asyncio
from collections import Counter

import httpx
import pytest

from crai.collect.client import ClashAPIError, ClashClient
from crai.config import get_settings

KEYS = ["k1", "k2", "k3", "k4"]


def client(handler: httpx.MockTransport, keys: list[str] = KEYS) -> ClashClient:
    return ClashClient(keys, "https://api.test/v1", concurrency=2, transport=handler)


def key_of(request: httpx.Request) -> str:
    return request.headers["Authorization"].removeprefix("Bearer ")


async def test_requests_spread_over_keys() -> None:
    seen: Counter[str] = Counter()

    async def ok(request: httpx.Request) -> httpx.Response:
        seen[key_of(request)] += 1
        await asyncio.sleep(0.001)
        return httpx.Response(200, json={"ok": True})

    async with client(httpx.MockTransport(ok)) as c:
        await asyncio.gather(*(c.get("/cards") for _ in range(40)))
    assert set(seen) == set(KEYS)
    assert seen == dict.fromkeys(KEYS, 10)  # even, including requests that queued for a slot


async def test_throttled_key_rests_while_others_work() -> None:
    seen: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        seen[key_of(request)] += 1
        if key_of(request) == "k2":
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(200, json={})

    async with client(httpx.MockTransport(handler)) as c:
        await asyncio.gather(*(c.get("/cards") for _ in range(20)))
        assert c.throttled >= 1
    assert seen["k2"] <= 2  # benched for 60 s after its first 429s; others took the load


async def test_rejected_key_is_disabled_not_fatal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if key_of(request) == "k3":
            return httpx.Response(403, text='{"reason":"accessDenied.invalidIp"}')
        return httpx.Response(200, json={})

    async with client(httpx.MockTransport(handler)) as c:
        results = await asyncio.gather(*(c.get("/cards") for _ in range(12)))
        stats = {s["key"]: s for s in c.key_stats()}
    assert results == [{}] * 12
    assert "invalidIp" in str(stats[3]["disabled"])
    assert "disabled" not in stats[1]


async def test_all_keys_rejected_is_fatal() -> None:
    forbidden = httpx.MockTransport(lambda _: httpx.Response(403, text="accessDenied"))
    async with client(forbidden) as c:
        with pytest.raises(ClashAPIError, match="every API key was rejected"):
            await c.get("/cards")


async def test_single_key_403_is_immediately_fatal() -> None:
    forbidden = httpx.MockTransport(lambda _: httpx.Response(403, text="accessDenied"))
    async with client(forbidden, ["only"]) as c:
        with pytest.raises(ClashAPIError, match="403"):
            await c.get("/cards")


def test_tokens_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CR_API_TOKENS", " a , b,,c ")
    assert get_settings().api_tokens == ("a", "b", "c")
