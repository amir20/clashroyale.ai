import time
from datetime import timedelta
from pathlib import Path

import polars as pl
import pytest
from conftest import ME, START, battle

from crai.analysis import analytics
from crai.analysis.analytics import Analyzer, Filters
from crai.storage import database as db
from crai.storage import snapshot


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


def test_big_sync_is_written_in_chunks(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot, "CHUNK_BATTLES", 60)  # 200 battles: four chunks
    assert snapshot.sync(db_path) == 400
    parts = sorted(p.name for p in snapshot.snapshot_dir(db_path).glob("*.parquet"))
    assert parts == [
        "decks-000000001-000000060.parquet",
        "decks-000000061-000000120.parquet",
        "decks-000000121-000000180.parquet",
        "decks-000000181-000000200.parquet",
    ]
    assert snapshot.read(db_path)[1].height == 400


def test_chunks_skip_pruned_gaps(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot, "CHUNK_BATTLES", 60)
    with db.session(db_path) as conn:
        for table, col in (
            ("participant_cards", "battle_id"),
            ("battle_participants", "battle_id"),
            ("battles", "id"),
        ):
            conn.execute(f"DELETE FROM {table} WHERE {col} BETWEEN 11 AND 150")
    assert snapshot.sync(db_path) == 120  # 60 battles left
    assert snapshot.covered_rowid(db_path) == 200
    assert snapshot.sync(db_path) == 0


def test_prune_after_crawl_does_not_rebuild(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with db.session(db_path) as conn:  # old games recorded last, as a crawl does
        old = [
            battle(START - timedelta(days=30, minutes=i), "#QQ", "#8Q", a_wins=True) for i in (1, 2)
        ]
        db.ingest_battlelog(conn, old)
    snapshot.sync(db_path)
    with db.session(db_path) as conn:
        assert db.prune_battles(conn, 14) == 1
    assert snapshot.sync(db_path) == 0
    assert "rebuilding" not in caplog.text


def test_new_battles_are_appended_not_reloaded(
    analyzer: Analyzer, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="crai.analysis.analytics")
    ranked = Filters(battle_types=("pathOfLegend",))
    assert analyzer.decks(ranked).height == 400
    add_battles(db_path, 5)
    assert analyzer.decks().height == 410
    assert analyzer.decks(ranked).height == 410  # the filtered copy grew too
    assert caplog.text.count("loaded") == 1
    assert "added 10 deck rows" in caplog.text
    # Same table as a fresh load.
    fresh = Analyzer(db_path).decks()
    assert (
        analyzer.decks()
        .sort("battle_id", "player_tag")
        .equals(fresh.sort("battle_id", "player_tag"))
    )


def test_seed_changes_reach_filtered_copies(analyzer: Analyzer, db_path: Path) -> None:
    ranked = Filters(battle_types=("pathOfLegend",))
    assert analyzer.decks(ranked).filter(is_top=1).height == 200
    with db.session(db_path) as conn:
        conn.execute("UPDATE players SET is_seed = 0")
    assert analyzer.decks(ranked).filter(is_top=1).height == 0


def test_window_drops_battles_that_age_out(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = Analyzer(db_path, window_days=7)
    assert a.decks().height == 400
    monkeypatch.setattr(snapshot, "cutoff", lambda days: "9999")  # everything is now too old
    monkeypatch.setattr(analytics, "TRIM_SECONDS", -1)
    assert a.decks().height == 0


def test_field_stats_outlive_new_battles_then_refresh(
    analyzer: Analyzer, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def games() -> int:
        return analyzer.decks().height

    assert analyzer._field_cached("games", games) == 400
    add_battles(db_path, 5)
    analyzer.decks()
    assert analyzer._field_cached("games", games) == 400  # kept: a few battles don't matter
    monkeypatch.setattr(analytics, "FIELD_REFRESH_SECONDS", -1)
    assert analyzer._field_cached("games", games) == 400  # stale value served at once...
    for _ in range(100):  # ...while a thread recomputes it
        if analyzer._field["games"][1] == 410:
            break
        time.sleep(0.02)
    assert analyzer._field["games"][1] == 410


def test_shared_cards_counts_overlap() -> None:
    decks = pl.DataFrame({"cards": [[1, 2, 3, 4], [5, 6, 7, 8], [1, 5, 9, 10]]})
    got = decks.select(analytics.shared_cards([1, 5, 9]))["cards"].to_list()
    assert got == [1, 1, 3]
