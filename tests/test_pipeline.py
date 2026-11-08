import random
import sqlite3

import pytest

from crai import db
from crai.analytics import Analyzer, CardNotFound, Filters
from crai.api import normalize_tag

CARDS = [
    (26000000, "Knight", 3, "Common"),
    (26000021, "Hog Rider", 4, "Rare"),
    (26000004, "P.E.K.K.A", 7, "Epic"),
    (27000008, "X-Bow", 6, "Epic"),
    (26000055, "Mega Knight", 7, "Legendary"),
    (28000011, "The Log", 2, "Legendary"),
    (28000000, "Fireball", 4, "Rare"),
    (26000010, "Skeletons", 1, "Common"),
    (26000030, "Ice Spirit", 1, "Common"),
    (26000014, "Musketeer", 4, "Rare"),
    (27000000, "Cannon", 3, "Common"),
    (26000012, "Skeleton Army", 3, "Epic"),
]
MAX = {"Common": 16, "Rare": 14, "Epic": 11, "Legendary": 8}
CYCLE = [
    26000021,
    28000011,
    28000000,
    26000010,
    26000030,
    26000014,
    27000000,
    26000000,
]  # wins
BEATDOWN = [
    26000004,
    26000055,
    28000011,
    28000000,
    26000010,
    26000012,
    26000014,
    26000000,
]


def card_json(cid):
    _, name, elixir, rarity = next(c for c in CARDS if c[0] == cid)
    return {
        "id": cid,
        "name": name,
        "elixirCost": elixir,
        "rarity": rarity,
        "level": MAX[rarity] - 1,
        "maxLevel": MAX[rarity],
        "evolutionLevel": 0,
        "iconUrls": {"medium": "x"},
    }


def player_json(tag, deck, crowns):
    return {
        "tag": tag,
        "name": tag[1:],
        "crowns": crowns,
        "startingTrophies": 9000,
        "trophyChange": 30,
        "cards": [card_json(c) for c in deck],
        "supportCards": [],
        "kingTowerHitPoints": 1000,
    }


def battle(t, a, b, a_wins):
    return {
        "type": "pathOfLegend",
        "battleTime": f"20261101T{t:06d}.000Z",
        "deckSelection": "collection",
        "gameMode": {"id": 72000464, "name": "Ranked1v1"},
        "arena": {"name": "Legendary Arena"},
        "team": [player_json(a, CYCLE, 3 if a_wins else 0)],
        "opponent": [player_json(b, BEATDOWN, 0 if a_wins else 1)],
    }


@pytest.fixture
def analyzer(tmp_path):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    db.upsert_cards(
        conn,
        {
            "items": [
                {
                    "id": c[0],
                    "name": c[1],
                    "elixirCost": c[2],
                    "rarity": c[3],
                    "maxLevel": MAX[c[3]],
                }
                for c in CARDS
            ]
        },
    )
    rng = random.Random(1)
    battles = [
        battle(i, "#2P", f"#{'2' * (i % 5 + 2)}", rng.random() < 0.7)
        for i in range(200)
    ]
    assert db.ingest_battlelog(conn, battles) == 200
    # Same battles seen from the opponent's side are deduplicated.
    flipped = [{**b, "team": b["opponent"], "opponent": b["team"]} for b in battles]
    assert db.ingest_battlelog(conn, flipped) == 0
    db.upsert_player(
        conn,
        {
            "tag": "#2P",
            "name": "me",
            "cards": [card_json(c) for c in CYCLE + [26000004]],
            "currentDeck": [card_json(c) for c in CYCLE],
        },
        is_seed=True,
    )
    conn.commit()
    return Analyzer(path)


def test_tags():
    assert normalize_tag("8cp8ocpq") == "#8CP80CPQ"
    with pytest.raises(ValueError):
        normalize_tag("#ABC")


def test_resolve(analyzer):
    assert analyzer.resolve_card("pekka") == 26000004
    assert analyzer.resolve_card("xbow") == 27000008
    assert analyzer.resolve_card("mk") == 26000055
    assert analyzer.resolve_card("hog") == 26000021
    assert analyzer.resolve_card("log") == 28000011
    with pytest.raises(CardNotFound):
        analyzer.resolve_card("skel")  # Skeletons vs Skeleton Army


def test_stats_pairs_counters(analyzer):
    assert analyzer.decks().height == 400
    s = analyzer.card_stats(min_games=1)
    hog = s.filter(s["name"] == "Hog Rider").row(0, named=True)
    assert hog["games"] == 200 and 0.6 < hog["win_rate"] < 0.8

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

    _, m = analyzer.card_matchups("hog", min_games=1)
    assert "P.E.K.K.A" in m["opponent_card"].to_list()

    decks = analyzer.top_decks(min_games=1)
    assert decks[0]["games"] == 200 and "Hog Rider" in decks[0]["cards"]

    c = analyzer.complete_deck(["hog", "log"], min_games=1)
    assert c["matching_decks"] == 200 and len(c["suggested_cards"]) == 6

    assert analyzer.compare_cards("hog", "pekka")["games"] == 200
    assert analyzer.top_decks(Filters(battle_types=("PvP",)), min_games=1) == []


def test_player(analyzer):
    prof = analyzer.player_profile("#2P")
    assert prof["current_deck"][0]["name"] == "Hog Rider"
    assert len(prof["recent_battles"]) == 15
    pc = analyzer.player_cards("#2P")
    assert set(pc["level_normalized"]) == {15}
    mine = analyzer.decks_for_player("#2P", min_games=1)
    assert len(mine) == 1 and mine[0]["avg_level"] == 15
    assert analyzer.sql("SELECT count(*) AS n FROM battles")["rows"] == [[200]]
    with pytest.raises(sqlite3.OperationalError):
        analyzer.sql("DELETE FROM battles")
