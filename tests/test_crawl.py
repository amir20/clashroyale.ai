"""End-to-end crawl against a fake API (httpx.MockTransport): no network."""

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from conftest import START, battle, card_list, profile

from crai import db
from crai.api import ClashAPIError, ClashClient
from crai.crawl import CrawlParams, crawl
from crai.models import ApiBattle

# seed '#22' fought '#88' and '#99'; '#88' fought '#PP' (depth 2); '#99' always 500s.
GRAPH = frozendict({"#22": ("#88", "#99"), "#88": ("#22", "#PP"), "#PP": ("#88",)})
FAILING = "#99"


def log_for(tag: str) -> list[ApiBattle]:
    out = []
    for opp in GRAPH.get(tag, ()):
        h = sum(map(ord, "".join(sorted((tag, opp)))))  # identical from both players' logs
        when = START.replace(hour=(h // 60) % 24, minute=h % 60)
        out.append(battle(when, tag, opp, a_wins=True))
    return out


def handler(request: httpx.Request) -> httpx.Response:
    path = unquote(request.url.path)
    match path.removeprefix("/v1").split("/"):
        case ["", "cards"]:
            return httpx.Response(200, json=card_list())
        case ["", "locations", *_, "players"]:
            return httpx.Response(200, json={"items": [{"tag": "#22", "rank": 1}]})
        case ["", "players", tag, *_] if tag == FAILING:
            return httpx.Response(500)
        case ["", "players", tag, "battlelog"]:
            return httpx.Response(200, json=log_for(tag))
        case ["", "players", tag]:
            return httpx.Response(200, json=profile(tag))
    return httpx.Response(404)


def client(transport: httpx.MockTransport) -> ClashClient:
    return ClashClient("t", "https://api.test/v1", max_retries=1, transport=transport)


async def test_crawl_depth_two(tmp_path: Path) -> None:
    async with client(httpx.MockTransport(handler)) as c:
        with db.session(tmp_path / "c.db") as conn:
            stats = await crawl(c, conn, CrawlParams(top=1, depth=2))
            seeds = conn.execute("SELECT tag FROM players WHERE is_seed = 1").fetchall()
            run = conn.execute("SELECT status, players_fetched FROM crawl_runs").fetchone()

    assert stats.players_fetched == 3  # #22, #88, #PP
    assert stats.players_failed == 1  # #99 is skipped, not fatal
    assert stats.per_depth == {0: 1, 1: 2, 2: 1}
    assert [tuple(r) for r in seeds] == [("#22",)]
    assert tuple(run) == ("ok", 3)

    # Re-running inside the refresh window fetches nothing new but still expands via the DB.
    async with client(httpx.MockTransport(handler)) as c:
        with db.session(tmp_path / "c.db") as conn:
            again = await crawl(c, conn, CrawlParams(top=1, depth=2))
    assert again.players_fetched == 0
    assert again.players_failed == 1  # only #99 (never stored) is retried
    assert again.players_skipped_fresh == 3


async def test_forbidden_is_fatal(tmp_path: Path) -> None:
    def forbidden(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=json.dumps({"reason": "accessDenied"}))

    path = tmp_path / "c.db"
    async with client(httpx.MockTransport(forbidden)) as c:
        with pytest.raises(ClashAPIError, match="403"), db.session(path) as conn:
            await crawl(c, conn, CrawlParams(top=0, tags=["#22"], depth=0))
    with db.session(path, read_only=True) as conn:
        status = conn.execute("SELECT status FROM crawl_runs").fetchone()[0]
    assert status == "error"


async def test_malformed_player_is_skipped_not_fatal(tmp_path: Path) -> None:
    def broken_88(request: httpx.Request) -> httpx.Response:
        if unquote(request.url.path).endswith("#88/battlelog"):
            # Deliberately ill-typed, so built as raw JSON: a card without an id, in a battle with
            # a unique time (a duplicate would be skipped by dedup before the card is read).
            bad: list[Any] = json.loads(json.dumps(log_for("#88")))
            bad[0]["battleTime"] = "20261101T235959.000Z"
            bad[0]["team"] = [{"tag": "#88", "cards": [{"name": "no id"}]}]
            return httpx.Response(200, json=bad)
        return handler(request)

    async with client(httpx.MockTransport(broken_88)) as c:
        with db.session(tmp_path / "c.db") as conn:
            stats = await crawl(c, conn, CrawlParams(top=1, depth=2))
            fetched_88 = conn.execute(
                "SELECT battlelog_fetched_at FROM players WHERE tag = '#88'"
            ).fetchone()
    assert stats.players_failed == 2  # #99 (HTTP 500) and #88 (malformed)
    assert stats.players_fetched == 1  # #22; #PP is never discovered because #88 was skipped
    assert fetched_88 is None or fetched_88[0] is None  # rolled back, so it's retried next run


async def test_only_a_few_players_are_in_progress_at_once(tmp_path: Path) -> None:
    """Regression: fetching everyone up front buffered huge amounts and starved the writer."""
    inflight = peak = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.001)
        inflight -= 1
        return handler(request)

    tags = [f"#{'2' * i}" for i in range(2, 40)]
    transport = httpx.MockTransport(slow)
    async with ClashClient("t", "https://api.test/v1", concurrency=2, transport=transport) as c:
        with db.session(tmp_path / "c.db") as conn:
            stats = await crawl(c, conn, CrawlParams(top=0, tags=tags, depth=0))
    assert stats.players_fetched == len(tags)
    assert c.parallelism == 2
    assert peak <= 2  # the per-key limit still holds
