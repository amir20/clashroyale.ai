"""MCP tool outputs: columnar, rounded, no ids, with margins."""

from pathlib import Path

import polars as pl
import pytest

from crai import mcp_server
from crai.analysis.analytics import Analyzer


@pytest.fixture(autouse=True)
def use_test_db(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setattr(mcp_server, "analyzer", Analyzer(db_path))


def test_table_is_columnar_rounded_without_ids() -> None:
    df = pl.DataFrame({"name": ["Hog"], "card_id": [1], "win_rate": [0.123456]})
    assert mcp_server.table(df) == {"columns": ["name", "win_rate"], "rows": [["Hog", 0.123]]}
    assert mcp_server.table([]) == {"columns": [], "rows": []}


def test_card_pairs() -> None:
    r = mcp_server.card_pairs("hog", min_games=1)
    assert r["summary"]["decks_with_card"] == 200
    cols = r["partners"]["columns"]
    assert "card_id" not in cols
    assert {"name", "games", "win_rate", "margin"} <= set(cols)
    assert len(r["partners"]["rows"]) == 7


def test_decks_and_profile() -> None:
    decks = mcp_server.top_decks(min_games=1)
    assert decks["columns"][:2] == ["cards", "avg_elixir"]
    assert "margin" in decks["columns"]
    mine = mcp_server.decks_for_player("#2P", min_games=1)
    row = dict(zip(mine["columns"], mine["rows"][0], strict=True))
    assert len(row["levels"]) == len(row["cards"]) == 8
    prof = mcp_server.player_profile("#2P")
    assert prof["recent_battles"]["columns"][0] == "battle_time"
    assert len(prof["recent_battles"]["rows"]) == 15
