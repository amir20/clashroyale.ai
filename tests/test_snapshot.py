from datetime import timedelta
from pathlib import Path

import pytest
from conftest import ME, START, battle

from crai import db, snapshot
from crai.analytics import Analyzer


def add_battles(db_path: Path, n: int, *, offset_days: int = 30) -> None:
    with db.session(db_path) as conn:
        new = [
            battle(START + timedelta(days=offset_days, minutes=5 * i), ME, "#8Q", a_wins=True)
            for i in range(n)
        ]
        assert db.ingest_battlelog(conn, new) == n


def test_sync_is_incremental(db_path: Path) -> None:
    assert snapshot.sync(db_path) == 400  # 200 battles x 2 players
    assert snapshot.sync(db_path) == 0
    add_battles(db_path, 3)
    assert snapshot.sync(db_path) == 6
    parts = sorted(p.name for p in snapshot.snapshot_dir(db_path).glob("*.parquet"))
    assert parts == ["decks-000000001-000000200.parquet", "decks-000000201-000000203.parquet"]


def test_analyzer_sees_new_battles_without_full_reload(analyzer: Analyzer, db_path: Path) -> None:
    assert analyzer.decks().height == 400
    add_battles(db_path, 5)
    assert analyzer.decks().height == 410  # cache invalidated by the snapshot, not mtime


def test_seed_changes_apply_without_new_battles(analyzer: Analyzer, db_path: Path) -> None:
    assert analyzer.decks().filter(is_top=1).height == 200  # ME is a seed
    with db.session(db_path) as conn:
        conn.execute("UPDATE players SET is_seed = 0")
    assert analyzer.decks().filter(is_top=1).height == 0


def test_compaction(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot, "MAX_PARTS", 2)
    snapshot.sync(db_path)
    for day in (40, 50):
        add_battles(db_path, 1, offset_days=day)
        snapshot.sync(db_path)
    parts = list(snapshot.snapshot_dir(db_path).glob("*.parquet"))
    assert [p.name for p in parts] == ["decks-000000001-000000202.parquet"]
    assert snapshot.read(db_path)[1].height == 404


def test_snapshot_matches_database(analyzer: Analyzer) -> None:
    d = analyzer.decks()
    assert d["score"].sum() == analyzer.scalar(
        "SELECT sum(CASE result WHEN 'win' THEN 1.0 WHEN 'draw' THEN 0.5 ELSE 0 END)"
        " FROM battle_participants"
    )
    assert d["cards"].list.len().unique().to_list() == [8]


def test_memory_window_limits_what_is_loaded(db_path: Path) -> None:
    add_battles(db_path, 1, offset_days=-20)  # an old battle
    assert snapshot.read(db_path)[1].height == 402
    assert snapshot.read(db_path, days=7)[1].height == 400
