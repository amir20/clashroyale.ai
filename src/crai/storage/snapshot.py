"""Incremental Parquet snapshot of deck appearances, for fast analytics.

SQLite stays the source of truth (the crawler writes there, concurrently with readers). Pulling
~1M deck rows out of SQLite costs 2.5-12 s; the same rows from Parquet load in ~30 ms. So the
snapshot keeps them as Parquet parts in `<db>.snapshot/`:

    decks-000000001-000164179.parquet   battles with rowid 1..164179
    decks-000164180-000490833.parquet   rows appended by a later sync

`sync()` appends only battles newer than the highest rowid already covered (battles are
append-only between migrations), so refreshing after a crawl or `refresh_player` is cheap. The
crawler syncs as it goes, so the web server can read Parquet that's already there instead of
converting SQLite rows itself.
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

# Bump when the columns change: parts in an older format are rebuilt on the next sync.
FORMAT = 3
_FORMAT_FILE = ".format"

# One row per player per 1v1 battle with a full 8-card deck. `specials` lists the cards played as
# an Evolution or Hero, each as card_id * 10 + evolution_level, sorted and joined by '-'. `level` is
# the deck's average card level on the in-game scale (the first ? is the top level, e.g. 16).
_QUERY = """
SELECT bp.battle_id, bp.player_tag, bp.side, bp.result, bp.deck_key, bp.starting_trophies,
       b.battle_type, b.deck_selection, b.battle_time,
       (SELECT group_concat(v, '-') FROM (
          SELECT pc.card_id * 10 + pc.evolution_level AS v FROM participant_cards pc
          WHERE pc.battle_id = bp.battle_id AND pc.player_tag = bp.player_tag
            AND pc.kind = 'deck' AND pc.evolution_level > 0 ORDER BY v)) AS specials,
       (SELECT avg(pc.level + ? - pc.max_level) FROM participant_cards pc
        WHERE pc.battle_id = bp.battle_id AND pc.player_tag = bp.player_tag
          AND pc.kind = 'deck') AS level
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
        "specials": pl.String(),
        "level": pl.Float32(),
    }


def snapshot_dir(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".snapshot")


class Busy(Exception):
    """Another process holds the snapshot (and the caller asked not to wait)."""


@contextmanager
def _locked(db_path: Path, wait: bool = True) -> Generator[None]:
    d = snapshot_dir(db_path)
    d.mkdir(parents=True, exist_ok=True)
    with (d / ".lock").open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise Busy(db_path) from None
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


def backlog(db_path: Path) -> int:
    """How many battle ids the database has past what the snapshot covers (an upper bound)."""
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)) as conn:
        latest: int = conn.execute("SELECT coalesce(max(rowid), 0) FROM battles").fetchone()[0]
    return max(0, latest - covered_rowid(db_path))


def _read_range(conn: sqlite3.Connection, lo: int, hi: int) -> pl.DataFrame:
    top = conn.execute("SELECT max(max_level) FROM cards WHERE is_support = 0").fetchone()[0]
    cur = conn.execute(_QUERY, (top or 16, lo, hi))
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


def sync(db_path: Path, wait: bool = True) -> int:
    """Bring the snapshot up to date with the database. Returns the number of rows appended.

    With `wait` off, raises Busy instead of waiting for another process's sync.
    """
    with _locked(db_path, wait):
        return _sync(db_path)


def _sync(db_path: Path) -> int:
    d = snapshot_dir(db_path)
    appended = 0
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)) as conn:
        if is_legacy(conn):
            raise LegacySchemaError(db_path)
        latest: int = conn.execute("SELECT coalesce(max(rowid), 0) FROM battles").fetchone()[0]
        start = covered_rowid(db_path)
        stale = latest < start  # database was rebuilt or shrunk underneath us
        if stale or (start and _format(d) != FORMAT):
            log.warning("snapshot %s; rebuilding", "ahead of database" if stale else "outdated")
            for *_, part in _parts(d):
                part.unlink()
            start = 0
        _set_format(d)
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


def _format(d: Path) -> int:
    try:
        return int((d / _FORMAT_FILE).read_text())
    except OSError, ValueError:
        return 1  # parts written before the format was recorded


def _set_format(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    if _format(d) != FORMAT:
        (d / _FORMAT_FILE).write_text(str(FORMAT))


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
    sync: bool = True,
) -> tuple[int, pl.DataFrame]:
    """Sync (unless `sync` is off), then return (covered rowid, deck appearances).

    `days` keeps only recent battles. `after` keeps only battles with a higher id, so a reader
    that already holds everything up to it can fetch just what's new. `transform` runs while
    reading, which is faster than changing the collected table.
    """
    with _locked(db_path):
        if sync:
            _sync(db_path)
        parts = [p for p in _parts(snapshot_dir(db_path)) if p[1] > after]
        if not parts:
            empty = pl.DataFrame(schema=_schema()).drop("result")
            scan = empty.with_columns(
                score=pl.lit(None, pl.Float64()), cards=pl.lit(None, pl.List(pl.Int64()))
            ).lazy()
            return covered_rowid(db_path), (transform(scan) if transform else scan).collect()
        scan = pl.scan_parquet([p for *_, p in parts])
        if _format(snapshot_dir(db_path)) < FORMAT:  # not rebuilt yet: newer columns are unknown
            have = scan.collect_schema().names()
            scan = scan.with_columns(
                pl.lit(None, dt).alias(name)
                for name, dt in _schema().items()
                if name not in have and name != "result"  # result became score
            )
        if after:
            scan = scan.filter(pl.col("battle_id") > after)
        if days:  # keep long-running servers from loading every battle ever crawled
            scan = scan.filter(pl.col("battle_time") >= cutoff(days))
        return parts[-1][1], (transform(scan) if transform else scan).collect()


def cutoff(days: int) -> str:
    """The `battle_time` before which a battle is older than `days`."""
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
