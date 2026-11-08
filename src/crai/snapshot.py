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
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

lazy import polars as pl

from .schema import LegacySchemaError, is_legacy

log = logging.getLogger(__name__)

MAX_PARTS = 16  # compact into one part beyond this
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
        df = _read_range(conn, start + 1, latest)
    _write(d, start + 1, latest, df)
    log.debug("snapshot: appended %d rows (battles %d..%d)", df.height, start + 1, latest)
    if len(_parts(d)) > MAX_PARTS:
        _compact(d)
    return df.height


def _compact(d: Path) -> None:
    """Merge all parts into one."""
    parts = _parts(d)
    if len(parts) <= 1:
        return
    merged = pl.read_parquet([p for _, _, p in parts])
    _write(d, parts[0][0], parts[-1][1], merged)
    for _, _, p in parts:
        p.unlink(missing_ok=True)


def read(db_path: Path) -> tuple[int, pl.DataFrame]:
    """Sync, then return (covered rowid, all deck appearances)."""
    with _locked(db_path):
        _sync(db_path)
        parts = _parts(snapshot_dir(db_path))
        if not parts:
            empty = pl.DataFrame(schema=_schema()).drop("result")
            return 0, empty.with_columns(
                score=pl.lit(None, pl.Float64()), cards=pl.lit(None, pl.List(pl.Int64()))
            )
        return parts[-1][1], pl.read_parquet([p for *_, p in parts])
