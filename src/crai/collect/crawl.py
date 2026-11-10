"""Breadth-first crawl: seeds -> everyone they fought -> everyone *they* fought, ...

Repeatable: players whose battle log was fetched within `refresh_hours` are not re-fetched,
but their already-stored opponents are still used to expand the frontier.
"""

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from crai.collect.client import ClashAPIError, ClashClient, normalize_tag
from crai.collect.models import ApiBattle, ApiPlayer
from crai.storage import database as db

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CrawlParams:
    top: int = 200
    tags: list[str] = field(default_factory=list)
    depth: int = 2
    max_players: int = 3000
    refresh_hours: float = 12.0
    tracked_refresh_hours: float = 2.0  # people who use Rook: their logs only hold ~25 battles
    location: str = "global"
    expand_friendly: bool = False


@dataclass(slots=True)
class CrawlStats:
    players_fetched: int = 0
    players_skipped_fresh: int = 0
    tracked_refreshed: int = 0
    players_missing: int = 0
    players_failed: int = 0
    battles_new: int = 0
    per_depth: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FetchResult:
    tag: str
    profile: ApiPlayer | None = None
    battles: list[ApiBattle] = field(default_factory=list)
    error: str | None = None


class CrawlProgress(Protocol):
    """Optional live-progress sink (the CLI renders it as progress bars)."""

    def depth_started(self, depth: int, to_fetch: int, fresh: int, budget_left: int) -> None: ...

    def player_done(
        self, done: int, battles_new: int, requests: int, throttled: int, failed: int
    ) -> None: ...


class _Writer:
    """Runs every SQLite call on one dedicated thread, in submission order.

    The event loop keeps fetching while the thread writes (SQLite releases the GIL while it
    executes statements), so fetch time and write time overlap instead of adding up. At most
    `max_pending` writes queue up before fetching waits (bounded memory).
    """

    def __init__(self, max_pending: int = 256) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="crai-sqlite")
        self._pending: deque[asyncio.Future[Any]] = deque()
        self._max = max_pending

    def submit[T](self, fn: Callable[..., T], *args: Any) -> asyncio.Future[T]:
        fut = asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)
        self._pending.append(fut)
        return fut

    async def call[T](self, fn: Callable[..., T], *args: Any) -> T:
        return await self.submit(fn, *args)

    def _reap(self) -> None:
        while self._pending and self._pending[0].done():
            self._pending.popleft().result()  # re-raises a failed write (DB errors are fatal)

    async def backpressure(self) -> None:
        self._reap()
        while len(self._pending) > self._max:
            await self._pending.popleft()

    async def drain(self) -> None:
        while self._pending:
            await self._pending.popleft()

    def close(self) -> None:
        self._pool.shutdown(wait=True)


def _ingest(
    conn: sqlite3.Connection, r: FetchResult, is_seed: bool, seed_rank: int | None, depth: int
) -> int | None:
    """Write one player (writer thread). Returns new battles, or None for a malformed payload.

    A malformed payload shouldn't end a 10k-player crawl: roll back just that player's writes and
    move on. Database errors (locked, full) still raise and stop the crawl.
    """
    conn.execute("SAVEPOINT player")
    try:
        if r.profile:
            db.upsert_player(conn, r.profile, is_seed=is_seed, seed_rank=seed_rank, depth=depth)
        new = db.ingest_battlelog(conn, r.battles)
        db.mark_battlelog_fetched(conn, r.tag)
    except KeyError, TypeError, AttributeError, ValueError:
        conn.execute("ROLLBACK TO player")
        conn.execute("RELEASE player")
        log.warning("skipping %s: malformed payload", r.tag, exc_info=True)
        return None
    conn.execute("RELEASE player")
    return new


def _is_fresh(conn: sqlite3.Connection, tag: str, cutoff: str) -> bool:
    row = conn.execute("SELECT battlelog_fetched_at FROM players WHERE tag = ?", (tag,)).fetchone()
    return bool(row and row[0] and row[0] >= cutoff)


async def _fetch(client: ClashClient, tag: str) -> FetchResult:
    """Fetch one player. Per-player failures are returned, not raised, so one flaky
    player can't kill the crawl. 403 (bad token / IP) is fatal and still raises."""
    profile, battles = await asyncio.gather(
        client.player(tag), client.battlelog(tag), return_exceptions=True
    )
    errors = [e for e in (profile, battles) if isinstance(e, BaseException)]
    for e in errors:
        if not isinstance(e, ClashAPIError) or e.status == 403:
            raise e
    if errors or isinstance(profile, BaseException) or isinstance(battles, BaseException):
        return FetchResult(tag, error="; ".join(map(str, errors)))
    return FetchResult(tag, profile, battles)


def _discovered(result: FetchResult, *, expand_friendly: bool) -> list[str]:
    battles = [b for b in result.battles if expand_friendly or b.get("type") != "friendly"]
    players = [*db.participants(b) for b in battles]  # PEP 798 unpacking comprehension
    return [t for p in players if (t := p.get("tag")) and t != result.tag]


async def _crawl_depth(
    client: ClashClient,
    conn: sqlite3.Connection,
    writer: _Writer,
    stats: CrawlStats,
    to_fetch: list[str],
    depth: int,
    params: CrawlParams,
    seed_set: set[str],
    seed_rank: dict[str, int],
    progress: CrawlProgress | None,
) -> tuple[list[str], list[asyncio.Task[None]]]:
    """Fetch and store one depth with a fixed number of workers.

    A worker finishes saving one player before fetching the next, so only `client.parallelism`
    players are ever in progress. Without this limit, finished battle logs pile up in memory and
    the flood of responses starves the writer thread (it made a NAS crawl 20x slower).
    """
    discovered: list[str] = []
    pending = iter(to_fetch)
    done = submitted = 0

    def finished() -> None:
        nonlocal done
        done += 1
        if progress:
            progress.player_done(
                done, stats.battles_new, client.requests, client.throttled, stats.players_failed
            )
        elif done % 500 == 0:
            log.info(
                "  depth %d: %d/%d players, %d new battles, %d requests, %d throttled",
                depth,
                done,
                len(to_fetch),
                stats.battles_new,
                client.requests,
                client.throttled,
            )

    async def worker() -> None:
        nonlocal submitted
        for tag in pending:  # one shared iterator: each player goes to exactly one worker
            r = await _fetch(client, tag)
            if r.error:
                stats.players_failed += 1
                log.warning("skipping %s: %s", r.tag, r.error)
            elif r.profile is None and not r.battles:
                stats.players_missing += 1
            else:
                new = await writer.submit(
                    _ingest, conn, r, r.tag in seed_set, seed_rank.get(r.tag), depth
                )
                if new is None:
                    stats.players_failed += 1
                else:
                    stats.battles_new += new
                    stats.players_fetched += 1
                    discovered.extend(_discovered(r, expand_friendly=params.expand_friendly))
                submitted += 1
                if submitted % 50 == 0:
                    writer.submit(conn.commit)
                await writer.backpressure()
            finished()

    tasks = [asyncio.create_task(worker()) for _ in range(client.parallelism)]
    await asyncio.gather(*tasks)
    writer.submit(conn.commit)
    await writer.drain()
    return discovered, tasks


async def crawl(
    client: ClashClient,
    conn: sqlite3.Connection,
    params: CrawlParams,
    progress: CrawlProgress | None = None,
) -> CrawlStats:
    stats = CrawlStats()
    writer = _Writer()
    run_id = await writer.call(_start_run, conn, params)

    seeds: list[str] = []
    seed_rank: dict[str, int] = {}
    if params.top > 0:
        top = await client.top_players(params.top, params.location)
        if not top:
            log.warning("No ranking data returned; continuing with explicit tags only.")
        for i, p in enumerate(top):
            seeds.append(p["tag"])
            seed_rank[p["tag"]] = p.get("rank") or i + 1
    seeds += [normalize_tag(t) for t in params.tags]

    cutoff = (datetime.now(UTC) - timedelta(hours=params.refresh_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    frontier = list(dict.fromkeys(seeds))
    seen = set(frontier)
    seed_set = set(frontier)
    budget = params.max_players
    tasks: list[asyncio.Task[None]] = []
    status = "error"
    tracked_cutoff = (datetime.now(UTC) - timedelta(hours=params.tracked_refresh_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    try:
        # Rook's users first, outside the budget: they're few, and their history is the product.
        tracked = [
            t
            for t in await writer.call(db.tracked_players, conn, tracked_cutoff)
            if t not in seed_set
        ]
        if tracked:
            if progress:
                progress.depth_started(0, len(tracked), 0, budget)
            _, tasks = await _crawl_depth(
                client, conn, writer, stats, tracked, 0, params, seed_set, seed_rank, progress
            )
            stats.tracked_refreshed = len(tracked)
            seen.update(tracked)
        for depth in range(params.depth + 1):
            if not frontier:
                break
            fresh = await writer.call(_fresh_set, conn, frontier, cutoff)
            to_fetch: list[str] = []
            expand_from_db: list[str] = []
            for t in frontier:
                if t in fresh:
                    expand_from_db.append(t)
                elif budget > 0:
                    to_fetch.append(t)
                    budget -= 1
            stats.players_skipped_fresh += len(expand_from_db)
            if progress:
                progress.depth_started(depth, len(to_fetch), len(expand_from_db), budget)
            log.debug(
                "depth %d: %d to fetch, %d fresh (budget left %d)",
                depth,
                len(to_fetch),
                len(expand_from_db),
                budget,
            )

            discovered, tasks = await _crawl_depth(
                client, conn, writer, stats, to_fetch, depth, params, seed_set, seed_rank, progress
            )
            stats.per_depth[depth] = len(to_fetch)
            if depth == params.depth:
                break
            for t in expand_from_db:
                discovered += await writer.call(db.opponents_of, conn, t)
            frontier = [t for t in dict.fromkeys(discovered) if t not in seen]
            seen.update(frontier)
        status = "ok"
    finally:
        # Cancel in-flight requests so an error/Ctrl-C exits cleanly, then let queued writes land.
        pending = [t for t in tasks if not t.done()]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        with contextlib.suppress(Exception):
            await writer.drain()
        await writer.call(_finish_run, conn, run_id, stats, client.requests, status)
        writer.close()
    return stats


def _start_run(conn: sqlite3.Connection, params: CrawlParams) -> int:
    cur = conn.execute(
        "INSERT INTO crawl_runs (started_at, params, status) VALUES (?, ?, 'running')",
        (db.now_iso(), json.dumps(asdict(params))),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def _finish_run(
    conn: sqlite3.Connection, run_id: int, stats: CrawlStats, requests: int, status: str
) -> None:
    conn.execute(
        """UPDATE crawl_runs SET finished_at=?, players_fetched=?, battles_new=?, requests=?,
             status=? WHERE id=?""",
        (db.now_iso(), stats.players_fetched, stats.battles_new, requests, status, run_id),
    )
    conn.commit()


def _fresh_set(conn: sqlite3.Connection, tags: list[str], cutoff: str) -> set[str]:
    return {t for t in tags if _is_fresh(conn, t, cutoff)}


async def sync_cards(client: ClashClient, conn: sqlite3.Connection) -> int:
    n = db.upsert_cards(conn, await client.cards())
    conn.commit()
    return n
