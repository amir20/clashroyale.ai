"""Breadth-first crawl: seeds -> everyone they fought -> everyone *they* fought, ...

Repeatable: players whose battle log was fetched within `refresh_hours` are not re-fetched,
but their already-stored opponents are still used to expand the frontier.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from . import db
from .api import ClashAPIError, ClashClient, normalize_tag

log = logging.getLogger(__name__)


@dataclass
class CrawlParams:
    top: int = 200
    tags: list[str] = field(default_factory=list)
    depth: int = 2
    max_players: int = 3000
    refresh_hours: float = 12.0
    location: str = "global"
    expand_friendly: bool = False


@dataclass
class CrawlStats:
    players_fetched: int = 0
    players_skipped_fresh: int = 0
    players_missing: int = 0
    players_failed: int = 0
    battles_new: int = 0
    per_depth: dict[int, int] = field(default_factory=dict)


def _is_fresh(conn: sqlite3.Connection, tag: str, cutoff: str) -> bool:
    row = conn.execute(
        "SELECT battlelog_fetched_at FROM players WHERE tag = ?", (tag,)
    ).fetchone()
    return bool(row and row[0] and row[0] >= cutoff)


async def _fetch(client: ClashClient, tag: str):
    """Fetch one player. Per-player failures are returned, not raised, so one flaky
    player can't kill the crawl. 403 (bad token / IP) is fatal and still raises."""
    try:
        profile, battles = await asyncio.gather(
            client.player(tag), client.battlelog(tag)
        )
    except ClashAPIError as e:
        if e.status == 403:
            raise
        return tag, None, None, str(e)
    return tag, profile, battles, None


async def crawl(
    client: ClashClient, conn: sqlite3.Connection, params: CrawlParams
) -> CrawlStats:
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
            t = p["tag"]
            seeds.append(t)
            seed_rank[t] = p.get("rank") or i + 1
    for t in params.tags:
        seeds.append(normalize_tag(t))

    cutoff = (datetime.now(UTC) - timedelta(hours=params.refresh_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    seen: set[str] = set()
    frontier = [t for t in dict.fromkeys(seeds)]
    seen.update(frontier)
    seed_set = set(frontier)
    budget = params.max_players
    tasks: list[asyncio.Task] = []

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
            done = 0
            for fut in asyncio.as_completed(tasks):
                tag, profile, battles, error = await fut
                done += 1
                if error:
                    stats.players_failed += 1
                    log.warning("skipping %s: %s", tag, error)
                    continue
                if profile is None and not battles:
                    stats.players_missing += 1
                    continue
                if profile:
                    db.upsert_player(
                        conn,
                        profile,
                        is_seed=tag in seed_set,
                        seed_rank=seed_rank.get(tag),
                        depth=depth,
                    )
                stats.battles_new += db.ingest_battlelog(conn, battles)
                db.mark_battlelog_fetched(conn, tag)
                stats.players_fetched += 1
                for b in battles:
                    if b.get("type") == "friendly" and not params.expand_friendly:
                        continue
                    for side in ("team", "opponent"):
                        for p in b.get(side) or []:
                            if p.get("tag") and p["tag"] != tag:
                                discovered.append(p["tag"])
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
                discovered.extend(db.opponents_of(conn, t))

            frontier = []
            for t in discovered:
                if t not in seen:
                    seen.add(t)
                    frontier.append(t)
        status = "ok"
    except BaseException:
        status = "error"
        raise
    finally:
        # Cancel in-flight requests so an error/Ctrl-C exits cleanly.
        pending = [t for t in tasks if not t.done()]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        conn.execute(
            """UPDATE crawl_runs SET finished_at=?, players_fetched=?, battles_new=?, requests=?, status=?
               WHERE id=?""",
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
