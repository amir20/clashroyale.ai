"""One-time conversion of a legacy (v1/v2) database to the v3 layout.

The conversion builds a *new* file next to the old one rather than migrating in place: rows are
copied in primary-key order (fast appends), secondary indexes are built afterwards, row counts
are verified, and only then are the files swapped. The original is kept as `<name>.v2-backup.db`.
"""

import logging
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from . import snapshot
from .schema import INDEXES, SCHEMA_VERSION, TABLES, is_legacy

log = logging.getLogger(__name__)

DEDUP_WINDOW_SECONDS = 10

# Tables copied as-is (same columns in both layouts).
PLAIN_TABLES = ("cards", "players", "player_cards", "player_current_deck", "crawl_runs")
BATTLE_COLS = (
    "battle_time, participants_key, battle_type, game_mode_id, game_mode, arena, deck_selection,"
    " is_ladder_tournament, is_hosted_match, league_number, team_size, ingested_at"
)
PARTICIPANT_COLS = (
    "player_tag, side, name, clan_tag, starting_trophies, trophy_change, crowns, opponent_crowns,"
    " result, king_tower_hp, princess_towers_hp, elixir_leaked, deck_key, avg_elixir"
)
CARD_COLS = "player_tag, card_id, slot, kind, level, max_level, evolution_level"
COUNTED = ("battles", "battle_participants", "participant_cards", "players", "player_cards")


@dataclass
class UpgradeReport:
    old_path: Path
    backup_path: Path
    counts: dict[str, int] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)
    old_bytes: int = 0
    new_bytes: int = 0


def _bring_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Legacy fix-ups the v3 copy relies on: participants_key filled in, near-duplicates gone."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(battles)")}
    if "participants_key" not in cols:
        conn.execute("ALTER TABLE battles ADD COLUMN participants_key TEXT")
    conn.execute(
        """UPDATE battles SET participants_key = (
             SELECT group_concat(player_tag, '|') FROM (
               SELECT player_tag FROM battle_participants
               WHERE battle_id = battles.id ORDER BY player_tag))
           WHERE participants_key IS NULL"""
    )
    # A battle with no participants can't be deduplicated or analysed; drop it.
    conn.execute("DELETE FROM battles WHERE participants_key IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS battles_pkey ON battles(participants_key)")
    dupes = [
        (r[0],)
        for r in conn.execute(
            """SELECT DISTINCT b2.id FROM battles b1
               JOIN battles b2 ON b2.participants_key = b1.participants_key AND b2.id <> b1.id
               WHERE (b1.battle_time < b2.battle_time
                      OR (b1.battle_time = b2.battle_time AND b1.id < b2.id))
                 AND unixepoch(b2.battle_time) - unixepoch(b1.battle_time) <= ?""",
            (DEDUP_WINDOW_SECONDS,),
        )
    ]
    for table in ("participant_cards", "battle_participants", "battles"):
        col = "id" if table == "battles" else "battle_id"
        conn.executemany(f"DELETE FROM {table} WHERE {col} = ?", dupes)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()


def _counts(conn: sqlite3.Connection, schema: str = "main") -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {schema}.{t}").fetchone()[0] for t in COUNTED}


def upgrade(path: Path, step: Callable[[str], None] = lambda _: None) -> UpgradeReport:
    """Convert `path` to v3. `step(message)` is called as each phase starts (for progress UI)."""
    path = Path(path)
    building = path.with_name(path.stem + ".v3-building.db")
    backup = path.with_name(path.stem + ".v2-backup.db")
    report = UpgradeReport(path, backup)
    if backup.exists():
        raise FileExistsError(f"{backup} already exists; move or delete it first.")

    with closing(sqlite3.connect(path, timeout=60)) as old:
        if not is_legacy(old):
            raise ValueError(f"{path} is already in the v3 layout.")
        if old.execute("PRAGMA user_version").fetchone()[0] < 2:
            step("fixing up legacy v1 data")
            _bring_v1_to_v2(old)
        old.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # everything in the main file first
    report.old_bytes = path.stat().st_size

    building.unlink(missing_ok=True)
    # uri=True so the old file can be attached read-only (`file:...?mode=ro`).
    with closing(sqlite3.connect(f"file:{building}", uri=True)) as new:
        # A scratch file until verified, so durability doesn't matter: go fast.
        for pragma in (
            "journal_mode = OFF",
            "synchronous = OFF",
            "cache_size = -1048576",
            "temp_store = MEMORY",
        ):
            new.execute(f"PRAGMA {pragma}")
        new.executescript(TABLES)
        new.execute("ATTACH DATABASE ? AS old", (f"file:{path}?mode=ro",))

        def run(label: str, sql: str) -> None:
            step(label)
            t0 = time.monotonic()
            new.execute(sql)
            new.commit()
            report.seconds[label] = time.monotonic() - t0

        for t in PLAIN_TABLES:
            cols = ", ".join(r[1] for r in new.execute(f"PRAGMA table_info({t})"))
            run(f"copying {t}", f"INSERT INTO main.{t} ({cols}) SELECT {cols} FROM old.{t}")
        # Old rowids become the new integer ids: same order, so the battle sequence is kept.
        run(
            "copying battles",
            f"""INSERT INTO main.battles (id, {BATTLE_COLS})
                SELECT rowid, {BATTLE_COLS} FROM old.battles ORDER BY rowid""",
        )
        # CROSS JOIN pins the scan order (old battles by rowid), so rows arrive already sorted by
        # the new primary key and every insert is an append.
        bp_cols = ", ".join(f"bp.{c.strip()}" for c in PARTICIPANT_COLS.split(","))
        run(
            "copying battle_participants",
            f"""INSERT INTO main.battle_participants (battle_id, {PARTICIPANT_COLS})
                SELECT b.rowid, {bp_cols}
                FROM old.battles b CROSS JOIN old.battle_participants bp ON bp.battle_id = b.id
                ORDER BY b.rowid, bp.player_tag""",
        )
        pc_cols = ", ".join(f"pc.{c.strip()}" for c in CARD_COLS.split(","))
        run(
            "copying participant_cards",
            f"""INSERT INTO main.participant_cards (battle_id, {CARD_COLS})
                SELECT b.rowid, {pc_cols}
                FROM old.battles b CROSS JOIN old.participant_cards pc ON pc.battle_id = b.id
                ORDER BY b.rowid, pc.player_tag, pc.card_id""",
        )
        step("building indexes")
        t0 = time.monotonic()
        new.executescript(INDEXES)
        report.seconds["building indexes"] = time.monotonic() - t0

        step("verifying row counts")
        report.counts = _counts(new)
        expected = _counts(new, "old")
        if report.counts != expected:
            raise RuntimeError(
                f"row counts differ, not swapping: new {report.counts} vs old {expected}"
            )
        new.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        new.commit()
        new.execute("DETACH DATABASE old")
        new.execute("PRAGMA journal_mode = WAL")

    step("swapping files")
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    path.rename(backup)
    building.rename(path)
    snapshot.invalidate(path)  # battle ids changed type; rebuilt on next use
    report.new_bytes = path.stat().st_size
    return report
