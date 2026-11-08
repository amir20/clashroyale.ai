import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from conftest import ME, battles, flipped

from crai import db
from crai.api import normalize_tag
from crai.models import ApiPlayer


def count(path: Path, table: str) -> int:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_normalize_tag() -> None:
    assert normalize_tag("8cp8ocpq") == "#8CP80CPQ"
    with pytest.raises(ValueError, match="Invalid player tag"):
        normalize_tag("#ABC")


def test_parse_battle_time() -> None:
    assert db.parse_battle_time("20261107T123456.000Z") == "2026-11-07T12:34:56Z"


@pytest.mark.parametrize("shift", [0, 1, -2, 10])
def test_same_battle_from_other_side_is_deduplicated(db_path: Path, shift: int) -> None:
    with db.session(db_path) as conn:
        assert db.ingest_battlelog(conn, [flipped(b, shift_seconds=shift) for b in battles()]) == 0
    assert count(db_path, "battles") == 200


def test_distinct_battles_outside_window_are_kept(db_path: Path) -> None:
    with db.session(db_path) as conn:
        assert db.ingest_battlelog(conn, [flipped(battles()[0], shift_seconds=120)]) == 1


def test_canonical_sides(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        sides = conn.execute(
            "SELECT DISTINCT side FROM battle_participants WHERE player_tag = ?", (ME,)
        ).fetchall()
    assert sides == [(1,)]  # '#2P' sorts after '#22...', so it is always side 1


def test_migration_v1_dedupes_existing_rows(tmp_path: Path) -> None:
    """A v1 database (no participants_key, near-duplicate rows) is repaired on connect."""
    path = tmp_path / "old.db"
    conn = db.connect(path)
    first = battles(3)
    db.ingest_battlelog(conn, first)
    # Simulate v1: inject a duplicate the old exact-timestamp ID scheme let through.
    dup = flipped(first[0], shift_seconds=1)
    conn.execute("UPDATE battles SET participants_key = NULL")
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO battles (id, battle_time, team_size) VALUES (?, ?, 1)",
        (db.battle_id(dup), db.parse_battle_time(dup["battleTime"])),
    )
    conn.execute(
        """INSERT INTO battle_participants (battle_id, player_tag, side)
           SELECT ?, player_tag, side FROM battle_participants WHERE battle_id = ?""",
        (db.battle_id(dup), db.battle_id(first[0])),
    )
    conn.commit()
    conn.close()
    assert count(path, "battles") == 4

    db.connect(path).close()
    assert count(path, "battles") == 3
    with closing(sqlite3.connect(path)) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert (
            c.execute("SELECT count(*) FROM battles WHERE participants_key IS NULL").fetchone()[0]
            == 0
        )


def test_player_collection_replaced_on_refresh(db_path: Path) -> None:
    with db.session(db_path) as conn:
        db.upsert_player(
            conn, {"tag": ME, "cards": [{"id": 26000000, "level": 16, "maxLevel": 16}]}
        )
    assert count(db_path, "player_cards") == 1


def test_explicit_nulls_from_api(db_path: Path) -> None:
    """Regression: the API sends `null` for absent objects (e.g. no ranked season this year)."""
    nulls: ApiPlayer = {
        "tag": "#9Q",
        "clan": None,
        "arena": None,
        "currentPathOfLegendSeasonResult": None,
        "bestPathOfLegendSeasonResult": None,
        "cards": None,
        "supportCards": None,
        "currentDeck": None,
    }
    b = battles(1)[0]
    b["arena"] = b["gameMode"] = None
    b["team"] = [p | {"clan": None, "supportCards": None, "crowns": None} for p in b["team"] or []]
    with db.session(db_path) as conn:
        db.upsert_player(conn, nulls)
        db.upsert_cards(conn, {"items": None, "supportItems": None})
        assert db.ingest_battle(conn, flipped(b, shift_seconds=600))
    assert count(db_path, "battles") == 201
