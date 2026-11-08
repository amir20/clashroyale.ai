import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import ME, battle, battles, flipped
from legacy import make_legacy

from crai import db
from crai.analytics import Analyzer
from crai.api import normalize_tag
from crai.models import ApiPlayer
from crai.schema import SCHEMA_VERSION, LegacySchemaError
from crai.upgrade import upgrade

START_OLD = datetime.now(UTC) - timedelta(days=40)


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


def test_legacy_database_is_refused(db_path: Path, tmp_path: Path) -> None:
    old = tmp_path / "old.db"
    make_legacy(db_path, old)
    with pytest.raises(LegacySchemaError, match="upgrade-db"):
        db.connect(old)
    with pytest.raises(LegacySchemaError):
        db.connect(old, read_only=True)


@pytest.mark.parametrize("version", [1, 2])
def test_upgrade_preserves_everything(db_path: Path, tmp_path: Path, version: int) -> None:
    old = tmp_path / "old.db"
    make_legacy(db_path, old, version=version)
    if version == 1:  # add a near-duplicate (1 s apart), as v1 could contain
        with closing(sqlite3.connect(old)) as c:
            first = c.execute("SELECT * FROM battles ORDER BY rowid LIMIT 1").fetchone()
            dup = "f" * 40
            c.execute(
                "INSERT INTO battles VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ', ?, '+1 second'),"
                " ?,?,?,?,?,?,?,?,?,?,?)",
                (dup, first[1], *first[2:]),
            )
            for t in ("battle_participants", "participant_cards"):
                cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})") if r[1] != "battle_id"]
                c.execute(
                    f"INSERT INTO {t} (battle_id, {', '.join(cols)})"
                    f" SELECT ?, {', '.join(cols)} FROM {t} WHERE battle_id = ?",
                    (dup, first[0]),
                )
            c.commit()
    report = upgrade(old)
    assert report.counts["battles"] == 200
    assert report.counts == {
        "battles": 200,
        "battle_participants": 400,
        "participant_cards": 3200,
        "players": count(db_path, "players"),
        "player_cards": count(db_path, "player_cards"),
    }
    assert report.backup_path.exists()
    with db.session(old, read_only=True) as conn:  # opens: it's v3 now
        ids = [r[0] for r in conn.execute("SELECT id FROM battles ORDER BY id")]
        assert ids == list(range(1, 201))  # sequential integers, original order kept
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert Analyzer(old).decks().height == 400  # analytics works on the upgraded file


def test_upgrade_refuses_v3_and_existing_backup(db_path: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="already"):
        upgrade(db_path)
    old = tmp_path / "old.db"
    make_legacy(db_path, old)
    (tmp_path / "old.v2-backup.db").touch()
    with pytest.raises(FileExistsError):
        upgrade(old)


def test_ingest_ids_are_sequential(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as c:
        ids = [r[0] for r in c.execute("SELECT id FROM battles ORDER BY rowid")]
    assert ids == sorted(ids) == list(range(1, len(ids) + 1))


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


def test_prune_keeps_recent_and_seed_battles(db_path: Path) -> None:
    with db.session(db_path) as conn:
        old = START_OLD
        kept = battle(old, ME, "#Q8", a_wins=True)  # ME is a seed: kept however old
        gone = battle(old, "#QQ", "#Q8", a_wins=True)
        assert db.ingest_battlelog(conn, [kept, gone]) == 2
        before = count_rows(conn, "participant_cards")
        assert db.prune_battles(conn, 14) == 1
        assert count_rows(conn, "participant_cards") == before - 16


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
