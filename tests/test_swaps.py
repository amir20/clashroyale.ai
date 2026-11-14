"""Swaps are judged on decks as close to the player's as possible, with what else changed and
where the new card loses."""

from datetime import timedelta
from pathlib import Path
from typing import Any

from conftest import CYCLE, ME, START, card, card_list, profile

from crai.analysis import insights, visuals
from crai.analysis.analytics import Analyzer
from crai.coach import voice
from crai.collect.models import ApiBattle
from crai.storage import database as db

KNIGHT, HOG, PEKKA, XBOW, MK, LOG, FIREBALL, SKELETONS, ICE_SPIRIT, MUSKETEER, CANNON, SKARMY = (
    26000000, 26000021, 26000004, 27000008, 26000055, 28000011,
    28000000, 26000010, 26000030, 26000014, 27000000, 26000012,
)  # fmt: skip
# Opponents run neither Musketeer nor P.E.K.K.A, so they never land in the comparison.
Deck = tuple[int, ...]
OPP_PLAIN: Deck = (KNIGHT, HOG, XBOW, MK, LOG, FIREBALL, SKELETONS, ICE_SPIRIT)
OPP_SKARMY: Deck = (KNIGHT, HOG, XBOW, MK, LOG, FIREBALL, SKELETONS, SKARMY)


def swap(deck: tuple[int, ...], out: int, into: int) -> tuple[int, ...]:
    return tuple(into if c == out else c for c in deck)


def side(tag: str, deck: tuple[int, ...], crowns: int) -> Any:
    return {
        "tag": tag,
        "name": tag[1:],
        "crowns": crowns,
        "startingTrophies": 9000,
        "trophyChange": 30,
        "cards": [card(c) for c in deck],
        "supportCards": [],
        "kingTowerHitPoints": 1000,
    }


def build(path: Path, games: list[tuple[Deck, Deck, bool]]) -> Analyzer:
    """`games` = (deck, opponent deck, won), each by a different player."""
    games = games + [(CYCLE, OPP_PLAIN, True)] * 5  # the player's own recent games
    battles: list[ApiBattle] = []
    for i, (deck, opp, won) in enumerate(games):
        tag = ME if i >= len(games) - 5 else f"#P{i}"
        battles.append(
            {
                "type": "pathOfLegend",
                "battleTime": (START + timedelta(seconds=i)).strftime("%Y%m%dT%H%M%S.000Z"),
                "deckSelection": "collection",
                "gameMode": {"id": 72000464, "name": "Ranked1v1"},
                "arena": {"name": "Legendary Arena"},
                "team": [side(tag, deck, 1 if won else 0)],
                "opponent": [side(f"#Q{i}", opp, 0 if won else 1)],
            }
        )
    with db.session(path) as conn:
        db.upsert_cards(conn, card_list())
        db.ingest_battlelog(conn, battles)
        db.upsert_player(conn, profile(), is_seed=True)
    return Analyzer(path)


def test_exact_one_card_swaps_come_first(tmp_path: Path) -> None:
    pekka_deck = swap(CYCLE, MUSKETEER, PEKKA)
    a = build(
        tmp_path / "e.db",
        [(CYCLE, OPP_PLAIN, k < 60) for k in range(150)]
        + [(pekka_deck, OPP_PLAIN, k < 90) for k in range(150)],
    )
    view = visuals.swap_view(a, ME, insights.games(a, ME), "musketeer", "pekka")
    assert view["match"]["exact"] is True
    assert view["match"]["also"] is None
    assert view["verdict"] == "better"


def test_other_changes_and_weak_spots_are_reported(tmp_path: Path) -> None:
    """Players who dropped Musketeer for P.E.K.K.A also swapped Cannon for Mega Knight, and
    lose every game against Skeleton Army that the Musketeer decks split."""
    keep = swap(CYCLE, CANNON, XBOW)
    drop = swap(swap(CYCLE, MUSKETEER, PEKKA), CANNON, MK)
    games = (
        [(keep, OPP_PLAIN, k < 30) for k in range(75)]
        + [(keep, OPP_SKARMY, k < 30) for k in range(75)]
        + [(drop, OPP_PLAIN, True) for _ in range(90)]
        + [(drop, OPP_SKARMY, False) for _ in range(60)]
    )
    a = build(tmp_path / "w.db", games)
    view = visuals.swap_view(a, ME, insights.games(a, ME), "musketeer", "pekka")
    m = view["match"]
    assert (m["exact"], m["shared"]) == (False, 6)
    assert [c["id"] for c in m["also"]["dropped"]] == [CANNON]
    assert [c["id"] for c in m["also"]["added"]] == [MK]
    assert m["also"]["share"] == 1.0
    assert [c["id"] for c in view["watch_out"]] == [SKARMY]
    line = voice.swap_caveats(m | {"match": m, "watch_out": view["watch_out"]})
    assert "Cannon for Mega Knight" in line and "Skeleton Army" in line


def test_decks_that_changed_half_the_cards_are_not_a_swap(tmp_path: Path) -> None:
    """Sharing only 4 of the other 7 cards is a different deck: no verdict."""
    keep = swap(CYCLE, CANNON, XBOW)
    far = swap(swap(swap(swap(CYCLE, MUSKETEER, PEKKA), CANNON, MK), KNIGHT, SKARMY), HOG, XBOW)
    a = build(
        tmp_path / "f.db",
        [(keep, OPP_PLAIN, k < 40) for k in range(150)]
        + [(far, OPP_PLAIN, True) for _ in range(150)],
    )
    view = visuals.swap_view(a, ME, insights.games(a, ME), "musketeer", "pekka")
    assert view["verdict"] == "unknown"
    assert view["in"]["games"] == 0
