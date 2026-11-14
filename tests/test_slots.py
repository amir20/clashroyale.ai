"""Evolution and Hero slots: a swap has to fit the slot the card leaves."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import CYCLE, ME, START, card, card_list, profile

from crai.analysis import insights, visuals
from crai.analysis.analytics import Analyzer
from crai.coach import voice
from crai.collect.models import ApiBattle
from crai.storage import database as db

MUSKETEER, KNIGHT, SKARMY, XBOW, CANNON, PEKKA = (
    26000014,
    26000000,
    26000012,
    27000008,
    27000000,
    26000004,
)
EVO_CAPABLE = {MUSKETEER, KNIGHT, SKARMY}  # X-Bow has no Evolution in this fixture
OTHERS = tuple(c for c in CYCLE if c != MUSKETEER)


def deck(cards: tuple[int, ...], evo: int) -> list[Any]:
    return [card(c) | {"evolutionLevel": 1 if c == evo else 0} for c in cards]


def fight(i: int, tag: str, cards: tuple[int, ...], evo: int, *, win: bool) -> ApiBattle:
    def side(t: str, d: list[Any], crowns: int) -> Any:
        return {
            "tag": t,
            "name": t[1:],
            "crowns": crowns,
            "startingTrophies": 9000,
            "trophyChange": 30,
            "cards": d,
            "supportCards": [],
            "kingTowerHitPoints": 1000,
        }

    return {
        "type": "pathOfLegend",
        "battleTime": (START + timedelta(seconds=i)).strftime("%Y%m%dT%H%M%S.000Z"),
        "deckSelection": "collection",
        "gameMode": {"id": 72000464, "name": "Ranked1v1"},
        "arena": {"name": "Legendary Arena"},
        "team": [side(tag, deck(cards, evo), 1 if win else 0)],
        # Opponents run no Evolution, so they never match the player's one-special deck.
        "opponent": [side(f"#Q{i}", deck(CYCLE, 0), 0 if win else 1)],
    }


def family() -> list[ApiBattle]:
    """The player runs Evo Musketeer. Other players' decks like theirs, one Evolution each:
    - 200 with Evo Musketeer (and P.E.K.K.A for Cannon: copies of their deck aren't compared),
      winning 30%
    - 150 with Evo Skeleton Army instead, winning 70%
    - 150 with plain X-Bow instead (and Evo Knight), winning 90%: the plain card looks best,
      but it can't fill the Evolution slot Musketeer leaves."""
    out: list[ApiBattle] = []
    groups = [
        (200, (*(c for c in OTHERS if c != CANNON), PEKKA, MUSKETEER), MUSKETEER, 0.3),
        (150, (*OTHERS, SKARMY), SKARMY, 0.7),
        (150, (*OTHERS, XBOW), KNIGHT, 0.9),
    ]
    for n, cards, evo, rate in groups:
        for j in range(n):
            i = len(out)
            out.append(fight(i, f"#P{i}", cards, evo, win=j < n * rate))
    out += [fight(len(out) + k, ME, CYCLE, MUSKETEER, win=True) for k in range(5)]
    return out


def setup(path: Path, skarmy_evo_unlocked: bool = True) -> Analyzer:
    cards = card_list()
    for c in cards["items"] or []:
        if c["id"] in EVO_CAPABLE:
            c["iconUrls"] = {"medium": "x", "evolutionMedium": "evo"}
    me = profile()
    me["cards"] = [
        *(me["cards"] or []),
        card(SKARMY) | {"evolutionLevel": int(skarmy_evo_unlocked)},
    ]
    with db.session(path) as conn:
        db.upsert_cards(conn, cards)
        db.ingest_battlelog(conn, family())
        db.upsert_player(conn, me, is_seed=True)
    return Analyzer(path)


@pytest.fixture
def slots_analyzer(tmp_path: Path) -> Analyzer:
    return setup(tmp_path / "s.db")


def test_snapshot_keeps_which_cards_were_special(slots_analyzer: Analyzer) -> None:
    mine = slots_analyzer.decks().filter(player_tag=ME)
    assert set(mine["specials"].cast(str)) == {str(MUSKETEER * 10 + 1)}


def test_replacement_fits_the_evolution_slot(slots_analyzer: Analyzer) -> None:
    items = insights.games(slots_analyzer, ME)
    r = insights.replace_card(slots_analyzer, ME, items)
    assert r["weakest"]["id"] == MUSKETEER
    assert r["weakest"]["status"] == "evolution"
    assert r["slot"] == "evolution"
    names = [c["id"] for c in r["candidates"]]
    assert names == [SKARMY]  # not X-Bow, which wins more but has no Evolution
    assert r["candidates"][0]["status"] == "evolution"
    line = voice.try_line(r)
    assert "Evo Skeleton Army" in line and "Evo Musketeer" in line


def test_locked_evolutions_are_not_suggested(tmp_path: Path) -> None:
    a = setup(tmp_path / "l.db", skarmy_evo_unlocked=False)
    r = insights.replace_card(a, ME, insights.games(a, ME))
    assert r["candidates"] == []


def test_swap_view_compares_in_the_same_slot(slots_analyzer: Analyzer) -> None:
    items = insights.games(slots_analyzer, ME)
    view = visuals.swap_view(slots_analyzer, ME, items, "musketeer", "skeleton army")
    assert view["out"]["status"] == view["in"]["status"] == "evolution"
    assert (view["out"]["games"], view["in"]["games"]) == (200, 150)
    assert view["verdict"] == "better"


def test_swap_view_refuses_a_card_without_that_form(slots_analyzer: Analyzer) -> None:
    items = insights.games(slots_analyzer, ME)
    view = visuals.swap_view(slots_analyzer, ME, items, "musketeer", "xbow")
    assert "no evolution form" in view["error"]
