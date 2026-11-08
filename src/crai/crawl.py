"""Breadth-first crawl: seeds -> everyone they fought -> everyone *they* fought, ...

Repeatable: players whose battle log was fetched within `refresh_hours` are not re-fetched,
but their already-stored opponents are still used to expand the frontier.
"""

import asyncio
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from . import db
from .api import ClashAPIError, ClashClient, normalize_tag
from .models import ApiBattle, ApiPlayer

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CrawlParams:
    top: int = 200
    tags: list[str] = field(default_factory=list)
    depth: int = 2
    max_players: int = 3000
    refresh_hours: float = 12.0
    location: str = "global"
    expand_friendly: bool = False


@dataclass(slots=True)
class CrawlStats:
    players_fetched: int = 0
    players_skipped_fresh: int = 0
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


async def crawl(client: ClashClient, conn: sqlite3.Connection, params: CrawlParams) -> CrawlStats:
    stats = CrawlStats()
    run_id = conn.execute(
        "INSERT INTO crawl_runs (started_at, params, status) VALUES (?, ?, 'running')",
        (db.now_iso(), json.dumps(asdict(params))),
    ).lastrowid
    conn.commit()

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
    tasks: list[asyncio.Task[FetchResult]] = []
    status = "error"

    try:
        for depth in range(params.depth + 1):
            if not frontier:
                break
            to_fetch: list[str] = []
            expand_from_db: list[str] = []
            for t in frontier:
                if _is_fresh(conn, t, cutoff):
                    expand_from_db.append(t)
                elif budget > 0:
                    to_fetch.append(t)
                    budget -= 1
            stats.players_skipped_fresh += len(expand_from_db)
            log.info(
                "depth %d: %d to fetch, %d fresh (budget left %d)",
                depth,
                len(to_fetch),
                len(expand_from_db),
                budget,
            )

            discovered: list[str] = []
            tasks = [asyncio.create_task(_fetch(client, t)) for t in to_fetch]
            for done, fut in enumerate(asyncio.as_completed(tasks), start=1):
                r = await fut
                if r.error:
                    stats.players_failed += 1
                    log.warning("skipping %s: %s", r.tag, r.error)
                elif r.profile is None and not r.battles:
                    stats.players_missing += 1
                else:
                    if r.profile:
                        db.upsert_player(
                            conn,
                            r.profile,
                            is_seed=r.tag in seed_set,
                            seed_rank=seed_rank.get(r.tag),
                            depth=depth,
                        )
                    stats.battles_new += db.ingest_battlelog(conn, r.battles)
                    db.mark_battlelog_fetched(conn, r.tag)
                    stats.players_fetched += 1
                    discovered += _discovered(r, expand_friendly=params.expand_friendly)
                if done % 50 == 0:
                    conn.commit()
                    log.info(
                        "  depth %d: %d/%d players, %d new battles so far, %d requests",
                        depth,
                        done,
                        len(to_fetch),
                        stats.battles_new,
                        client.requests,
                    )
            conn.commit()
            stats.per_depth[depth] = len(to_fetch)
            if depth == params.depth:
                break
            for t in expand_from_db:
                discovered += db.opponents_of(conn, t)
            frontier = [t for t in dict.fromkeys(discovered) if t not in seen]
            seen.update(frontier)
        status = "ok"
    finally:
        # Cancel in-flight requests so an error/Ctrl-C exits cleanly.
        pending = [t for t in tasks if not t.done()]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        conn.execute(
            """UPDATE crawl_runs SET finished_at=?, players_fetched=?, battles_new=?, requests=?,
                 status=? WHERE id=?""",
            (
                db.now_iso(),
                stats.players_fetched,
                stats.battles_new,
                client.requests,
                status,
                run_id,
            ),
        )
        conn.commit()
    return stats


async def sync_cards(client: ClashClient, conn: sqlite3.Connection) -> int:
    n = db.upsert_cards(conn, await client.cards())
    conn.commit()
    return n
