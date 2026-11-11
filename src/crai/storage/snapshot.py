"""Incremental Parquet snapshot of deck appearances, for fast analytics.

SQLite stays the source of truth (the crawler writes there, concurrently with readers). Pulling
~1M deck rows out of SQLite costs 2.5-12 s; the same rows from Parquet load in ~30 ms. So the
snapshot keeps them as Parquet parts in `<db>.snapshot/`:

    decks-000000001-000164179.parquet   battles with rowid 1..164179
    decks-000164180-000490833.parquet   rows appended by a later sync

`sync()` appends only battles newer than the highest rowid already covered (battles are
append-only between migrations), so refreshing after a crawl or `refresh_player` is cheap.
Migrations that delete battles call `invalidate()`. A lock file serializes sync/compact/read across
processes (CLI, MCP server, crawler), and parts are written to a temp file then renamed.

Seed (top-player) status is *not* stored: it changes after battles are recorded, so it's joined in
from the `players` table at load time.
"""

import fcntl
import logging
import os
import re
import shutil
import sqlite3
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

lazy import polars as pl

from crai.storage.schema import LegacySchemaError, is_legacy

log = logging.getLogger(__name__)

MAX_PARTS = 16  # compact into one part beyond this
CHUNK_BATTLES = 200_000  # battle ids read per part, so a full rebuild never holds every row
_PART = re.compile(r"decks-(\d{9})-(\d{9})\.parquet")

# One row per player per 1v1 battle with a full 8-card deck.
_QUERY = """
SELECT bp.battle_id, bp.player_tag, bp.side, bp.result, bp.deck_key, bp.starting_trophies,
       b.battle_type, b.deck_selection, b.battle_time
FROM battles b JOIN battle_participants bp ON bp.battle_id = b.id
WHERE b.rowid BETWEEN ? AND ? AND b.team_size = 1
  AND bp.deck_key IS NOT NULL AND bp.result IS NOT NULL
"""


def _schema() -> dict[str, pl.DataType]:
    return {
        "battle_id": pl.Int64(),
        "player_tag": pl.String(),
        "side": pl.Int8(),
        "result": pl.String(),
        "deck_key": pl.String(),
        "starting_trophies": pl.Int32(),
        "battle_type": pl.String(),
        "deck_selection": pl.String(),
        "battle_time": pl.String(),
    }


def snapshot_dir(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".snapshot")


@contextmanager
def _locked(db_path: Path) -> Generator[None]:
    d = snapshot_dir(db_path)
    d.mkdir(parents=True, exist_ok=True)
    with (d / ".lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def invalidate(db_path: Path) -> None:
    shutil.rmtree(snapshot_dir(db_path), ignore_errors=True)


def _parts(d: Path) -> list[tuple[int, int, Path]]:
    if not d.is_dir():
        return []
    found = [(int(m[1]), int(m[2]), p) for p in d.iterdir() if (m := _PART.fullmatch(p.name))]
    return sorted(found)


def covered_rowid(db_path: Path) -> int:
    parts = _parts(snapshot_dir(db_path))
    return parts[-1][1] if parts else 0


def _read_range(conn: sqlite3.Connection, lo: int, hi: int) -> pl.DataFrame:
    cur = conn.execute(_QUERY, (lo, hi))
    rows = cur.fetchall()
    schema = _schema()
    if not rows:
        return pl.DataFrame(schema=schema)
    cols = list(zip(*rows, strict=True))
    return (
        pl.DataFrame(
            {
                name: pl.Series(name, col, dtype=dt)
                for (name, dt), col in zip(schema.items(), cols, strict=True)
            }
        )
        .with_columns(
            score=pl.col("result").replace_strict({"win": 1.0, "draw": 0.5}, default=0.0),
            cards=pl.col("deck_key").str.split("-").cast(pl.List(pl.Int64())),
        )
        .drop("result")
    )


def _write(d: Path, lo: int, hi: int, df: pl.DataFrame) -> None:
    d.mkdir(parents=True, exist_ok=True)
    final = d / f"decks-{lo:09d}-{hi:09d}.parquet"
    tmp = d / f".{final.name}.{os.getpid()}.tmp"
    df.write_parquet(tmp, compression="zstd")
    tmp.replace(final)  # atomic


def sync(db_path: Path) -> int:
    """Bring the snapshot up to date with the database. Returns the number of rows appended."""
    with _locked(db_path):
        return _sync(db_path)


def _sync(db_path: Path) -> int:
    d = snapshot_dir(db_path)
    appended = 0
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)) as conn:
        if is_legacy(conn):
            raise LegacySchemaError(db_path)
        latest: int = conn.execute("SELECT coalesce(max(rowid), 0) FROM battles").fetchone()[0]
        start = covered_rowid(db_path)
        if latest < start:  # database was rebuilt or shrunk underneath us
            log.warning("snapshot ahead of database; rebuilding")
            for *_, part in _parts(d):
                part.unlink()
            start = 0
        if latest == start:
            return 0
        # One part per chunk of battle ids. Memory stays flat however far behind the snapshot
        # is, and a rebuild that gets interrupted resumes from the last part it wrote.
        while start < latest:
            row = conn.execute("SELECT min(rowid) FROM battles WHERE rowid > ?", (start,))
            lo: int = row.fetchone()[0]
            hi = min(lo + CHUNK_BATTLES - 1, latest)
            df = _read_range(conn, lo, hi)
            # An empty chunk is skipped (the next part's range covers the gap), except the last
            # one, which always lands so the snapshot records that it reached `latest`.
            if df.height or hi == latest:
                _write(d, start + 1, hi, df)
                appended += df.height
                log.debug("snapshot: appended %d rows (battles %d..%d)", df.height, lo, hi)
            start = hi
    if len(_parts(d)) > MAX_PARTS:
        _compact(d)
    return appended


def _compact(d: Path) -> None:
    """Merge all parts into one, streaming so the merged table is never held in memory."""
    parts = _parts(d)
    if len(parts) <= 1:
        return
    final = d / f"decks-{parts[0][0]:09d}-{parts[-1][1]:09d}.parquet"
    tmp = d / f".{final.name}.{os.getpid()}.tmp"
    pl.scan_parquet([p for _, _, p in parts]).sink_parquet(tmp, compression="zstd")
    tmp.replace(final)  # atomic
    for _, _, p in parts:
        if p != final:
            p.unlink(missing_ok=True)


def read(
    db_path: Path,
    days: int = 0,
    after: int = 0,
    transform: Callable[[pl.LazyFrame], pl.LazyFrame] | None = None,
) -> tuple[int, pl.DataFrame]:
    """Sync, then return (covered rowid, deck appearances).

    `days` keeps only recent battles. `after` keeps only battles with a higher id, so a reader
    that already holds everything up to it can fetch just what's new. `transform` runs while
    reading, which is faster than changing the collected table.
    """
    with _locked(db_path):
        _sync(db_path)
        parts = [p for p in _parts(snapshot_dir(db_path)) if p[1] > after]
        if not parts:
            empty = pl.DataFrame(schema=_schema()).drop("result")
            scan = empty.with_columns(
                score=pl.lit(None, pl.Float64()), cards=pl.lit(None, pl.List(pl.Int64()))
            ).lazy()
            return covered_rowid(db_path), (transform(scan) if transform else scan).collect()
        scan = pl.scan_parquet([p for *_, p in parts])
        if after:
            scan = scan.filter(pl.col("battle_id") > after)
        if days:  # keep long-running servers from loading every battle ever crawled
            scan = scan.filter(pl.col("battle_time") >= cutoff(days))
        return parts[-1][1], (transform(scan) if transform else scan).collect()


def cutoff(days: int) -> str:
    """The `battle_time` before which a battle is older than `days`."""
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
