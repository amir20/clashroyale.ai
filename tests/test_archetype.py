"""The player's core (staples) is found from the data, and swaps only change flex cards, judged
against decks built on the same core."""

from pathlib import Path

import pytest
from conftest import CYCLE, ME
from test_swaps import CANNON, HOG, KNIGHT, MK, MUSKETEER, PEKKA, SKARMY, XBOW, Deck, build

from crai.analysis import insights, visuals
from crai.analysis.analytics import Analyzer
from crai.coach import voice

CORE = tuple(c for c in CYCLE if c not in (MUSKETEER, CANNON, KNIGHT))  # Hog, Log, Fireball, ...
# Opponents share only Hog and the flex cards, so no core of theirs is mistaken for the player's.
OPP: Deck = (PEKKA, XBOW, MK, SKARMY, MUSKETEER, CANNON, KNIGHT, HOG)


@pytest.fixture(autouse=True)
def small_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(insights, "MIN_CORE_GAMES", 400)
    monkeypatch.setattr(insights, "MIN_CORE_PLAYERS", 50)
    monkeypatch.setattr(insights, "MIN_PACKAGE_PLAYERS", 10)


def group(flex: tuple[int, ...], n: int, rate: float) -> list[tuple[Deck, Deck, bool]]:
    return [((*CORE, *flex), OPP, k < n * rate) for k in range(n)]


@pytest.fixture
def arch_analyzer(tmp_path: Path) -> Analyzer:
    """Every deck keeps the 5 core cards and varies the 3 flex ones:
    - Knight -> X-Bow loses more (40%)
    - Cannon -> P.E.K.K.A wins more (70%)
    - Musketeer -> Mega Knight wins most (80%), but the player doesn't own Mega Knight
    - exact copies of the player's deck win 45%."""
    return build(
        tmp_path / "a.db",
        group((MUSKETEER, CANNON, XBOW), 120, 0.4)
        + group((MUSKETEER, KNIGHT, PEKKA), 120, 0.7)
        + group((CANNON, KNIGHT, MK), 120, 0.8)
        + group((MUSKETEER, CANNON, KNIGHT), 120, 0.45),
    )


def test_the_core_is_what_everyone_keeps(arch_analyzer: Analyzer) -> None:
    arch = insights.archetype(arch_analyzer, ME, insights.games(arch_analyzer, ME))
    assert arch is not None
    assert sorted(v // 10 for v in arch.core) == sorted(CORE)
    assert sorted(v // 10 for v in arch.flex) == sorted((MUSKETEER, CANNON, KNIGHT))


def test_only_flex_packages_that_win_and_are_owned(arch_analyzer: Analyzer) -> None:
    r = insights.replace_card(arch_analyzer, ME, insights.games(arch_analyzer, ME))
    swaps = [([c["id"] for c in p["out"]], [c["id"] for c in p["in"]]) for p in r["candidates"]]
    assert swaps == [([CANNON], [PEKKA])]  # not Mega Knight: they don't own it
    assert {c["id"] for c in r["core"]} == set(CORE)
    assert r["weakest"] is None or r["weakest"]["id"] not in CORE
    assert "P.E.K.K.A for Cannon" in voice.try_line(r)


def test_the_swap_chart_matches_the_suggestion(arch_analyzer: Analyzer) -> None:
    items = insights.games(arch_analyzer, ME)
    top = insights.replace_card(arch_analyzer, ME, items)["candidates"][0]
    view = visuals.swap_view(arch_analyzer, ME, items, "cannon", "pekka")
    assert view["gap"] == top["gain"]
    assert view["in"]["games"] == top["games"]
    assert {c["id"] for c in view["match"]["core"]} == set(CORE)


def test_staples_are_not_swapped(arch_analyzer: Analyzer) -> None:
    view = visuals.swap_view(arch_analyzer, ME, insights.games(arch_analyzer, ME), "hog", "pekka")
    assert "staple" in view["error"]


def test_when_nobody_keeps_their_flex_cards_compare_with_the_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Players with this core change all three flex cards, so there are no decks keeping the
    player's to compare with: variations are judged against every other deck with the core."""
    monkeypatch.setattr(insights, "unlocked", lambda *_: None)  # collection unknown
    a = build(
        tmp_path / "n.db",
        group((XBOW, PEKKA, MK), 160, 0.7)
        + group((SKARMY, PEKKA, XBOW), 150, 0.3)
        + group((MK, SKARMY, XBOW), 150, 0.4),
    )
    r = insights.replace_card(a, ME, insights.games(a, ME))
    [top] = r["candidates"]
    assert sorted(c["id"] for c in top["in"]) == sorted((XBOW, PEKKA, MK))
    assert sorted(c["id"] for c in top["out"]) == sorted((MUSKETEER, CANNON, KNIGHT))
    assert top["baseline"] == "core"
    assert r["your_fill_games"] == 0
    assert sorted(c["id"] for c in r["popular"]["cards"]) == sorted((XBOW, PEKKA, MK))
    assert "than other decks with your core" in voice.try_line(r)
