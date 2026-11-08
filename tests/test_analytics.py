import sqlite3

import pytest
from conftest import HOG, ME, PEKKA

from crai.analytics import Analyzer, CardNotFound, Filters


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("pekka", PEKKA),
        ("xbow", 27000008),
        ("mk", 26000055),
        ("hog", HOG),
        ("log", 28000011),
        (HOG, HOG),
        (str(HOG), HOG),
    ],
)
def test_resolve_card(analyzer: Analyzer, query: str | int, expected: int) -> None:
    assert analyzer.resolve_card(query) == expected


def test_resolve_card_ambiguous(analyzer: Analyzer) -> None:
    with pytest.raises(CardNotFound, match="ambiguous"):
        analyzer.resolve_card("skel")  # Skeletons vs Skeleton Army


def test_card_stats(analyzer: Analyzer) -> None:
    assert analyzer.decks().height == 400
    s = analyzer.card_stats(min_games=1)
    hog = s.filter(s["name"] == "Hog Rider").row(0, named=True)
    assert hog["games"] == 200
    assert 0.6 < hog["win_rate"] < 0.8


def test_card_pairs(analyzer: Analyzer) -> None:
    summary, p = analyzer.card_pairs("hog", min_games=1)
    assert summary["decks_with_card"] == 200
    assert set(p["name"]) == {
        "The Log",
        "Fireball",
        "Skeletons",
        "Ice Spirit",
        "Musketeer",
        "Cannon",
        "Knight",
    }


def test_matchups_and_compare(analyzer: Analyzer) -> None:
    _, m = analyzer.card_matchups("hog", min_games=1)
    assert "P.E.K.K.A" in m["opponent_card"].to_list()
    assert analyzer.compare_cards("hog", "pekka")["games"] == 200


def test_decks(analyzer: Analyzer) -> None:
    decks = analyzer.top_decks(min_games=1)
    assert decks[0]["games"] == 200
    assert "Hog Rider" in decks[0]["cards"]
    assert analyzer.top_decks(Filters.of(["trail"]), min_games=1) == []

    c = analyzer.complete_deck(["hog", "log"], min_games=1)
    assert c["matching_decks"] == 200
    assert len(c["suggested_cards"]) == 6


def test_player(analyzer: Analyzer) -> None:
    prof = analyzer.player_profile(ME)
    assert prof["current_deck"][0]["name"] == "Hog Rider"
    assert len(prof["recent_battles"]) == 15
    assert set(analyzer.player_cards(ME)["level_normalized"]) == {15}
    mine = analyzer.decks_for_player(ME, min_games=1)
    assert len(mine) == 1
    assert mine[0]["avg_level"] == 15


def test_sql_is_read_only(analyzer: Analyzer) -> None:
    assert analyzer.sql("SELECT count(*) AS n FROM battles")["rows"] == [[200]]
    with pytest.raises(sqlite3.OperationalError):
        analyzer.sql("DELETE FROM battles")
