"""The deck model on the server: win chance against the decks a player faces, and why."""

import json
import math
from pathlib import Path

import pytest
from conftest import CYCLE, ME
from test_archetype import CORE, group
from test_swaps import CANNON, HOG, KNIGHT, MK, MUSKETEER, PEKKA, XBOW, build

from crai.analysis import insights
from crai.analysis.analytics import Analyzer
from crai.ml import explain
from crai.ml.data import vocabulary


@pytest.fixture(autouse=True)
def small_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(insights, "MIN_CORE_GAMES", 400)
    monkeypatch.setattr(insights, "MIN_CORE_PLAYERS", 50)
    monkeypatch.setattr(insights, "MIN_PACKAGE_PLAYERS", 10)
    monkeypatch.setattr(explain, "MIN_CORE_CARD_GAMES", 50)


@pytest.fixture
def analyzer(tmp_path: Path) -> Analyzer:
    return build(
        tmp_path / "e.db",
        group((MUSKETEER, CANNON, XBOW), 120, 0.4)
        + group((MUSKETEER, KNIGHT, PEKKA), 120, 0.7)
        + group((CANNON, KNIGHT, MK), 120, 0.8)
        + group((MUSKETEER, CANNON, KNIGHT), 120, 0.45),
    )


@pytest.fixture
def model_path(analyzer: Analyzer, tmp_path: Path) -> Path:
    """A hand-made model: P.E.K.K.A is a strong card, Cannon a weak one, Hog + P.E.K.K.A work
    together, and P.E.K.K.A beats X-Bow."""
    vocab = vocabulary(analyzer.db_path)
    t = {cid: vocab[str(cid)] for cid in (PEKKA, CANNON, HOG, XBOW)}
    card = [0.0] * (max(vocab.values()) + 4)
    card[t[PEKKA]], card[t[CANNON]] = 0.3, -0.2
    path = tmp_path / "ml" / "model.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "about": {"accuracy": 0.645, "strength_accuracy": 0.545, "battles": 100},
                "vocab": vocab,
                "strength": [1.0, 0.1],
                "card": card,
                "pair": [[min(t[HOG], t[PEKKA]), max(t[HOG], t[PEKKA]), 0.4]],
                "vs": [[t[PEKKA], t[XBOW], 0.25]],
            }
        )
    )
    return path


def test_no_model_file_means_no_model(tmp_path: Path) -> None:
    assert explain.load(tmp_path / "missing.json") is None


def test_card_shares_add_up_to_the_decks_edge(analyzer: Analyzer, model_path: Path) -> None:
    model = explain.load(model_path)
    assert model is not None
    items = insights.games(analyzer, ME)
    f = explain.field_for(analyzer, model, items)
    assert f is not None
    deck = [model.token(c * 10) for c in (*CORE, MUSKETEER, KNIGHT, PEKKA)]
    edge = sum(sum(s.values()) for s in explain.shares(model, deck, f))
    mean_logit = sum(
        model.own(deck) - own + sum(model.counter(x, y) for x in deck for y in d)
        for d, own in zip(f.decks, f.own, strict=True)
    ) / len(f.decks)
    assert math.isclose(edge, mean_logit, abs_tol=1e-9)


def test_the_report_explains_the_deck_and_each_swap(analyzer: Analyzer, model_path: Path) -> None:
    items = insights.games(analyzer, ME)
    replace = insights.replace_card(analyzer, ME, items)
    view = explain.report(analyzer, model_path, ME, items, replace)
    assert view is not None
    assert 0 < view["win"] < 1
    assert len(view["cards"]) == len(CYCLE)
    assert view["cards"][-1]["id"] == CANNON  # the weakest card in the deck
    assert view["about"]["accuracy"] == 0.645
    [top] = [c for c in replace["candidates"] if [x["id"] for x in c["in"]] == [PEKKA]]
    verdict = top["model"]
    assert verdict["gain"] > 0  # P.E.K.K.A is better than Cannon, and works with Hog
    synergy = next(r for r in verdict["reasons"] if r["kind"] == "synergy")
    assert {c["id"] for c in synergy["cards"]} == {HOG, PEKKA}
    assert synergy["how"] == "gained"


def test_the_model_picks_its_own_swap_and_battle_data_checks_it(
    analyzer: Analyzer, model_path: Path
) -> None:
    """Cannon is the weakest card and P.E.K.K.A the strongest one the player owns that works
    with Hog: the model swaps them, only them (no other swap helps), and never a core card."""
    items = insights.games(analyzer, ME)
    replace = insights.replace_card(analyzer, ME, items)
    view = explain.report(analyzer, model_path, ME, items, replace)
    assert view is not None
    pick = view["pick"]
    assert [c["id"] for c in pick["out"]] == [CANNON]
    assert [c["id"] for c in pick["in"]] == [PEKKA]
    assert pick["gain"] > 0 and pick["win_after"] > pick["win"]
    assert not {c["id"] for c in pick["out"]} & set(CORE)
    assert pick["check"]["verdict"] == "better"  # players who made that swap did win more
    assert pick["singles"][0]["in"]["id"] == PEKKA


def test_the_model_only_suggests_cards_it_has_seen_enough(
    analyzer: Analyzer, model_path: Path
) -> None:
    raw = json.loads(model_path.read_text())
    raw["games"] = [0] * len(raw["card"])  # never seen anything: nothing to suggest
    model_path.write_text(json.dumps(raw))
    items = insights.games(analyzer, ME)
    view = explain.report(
        analyzer, model_path, ME, items, insights.replace_card(analyzer, ME, items)
    )
    assert view is not None
    assert view["pick"] is None


def test_the_model_only_suggests_cards_players_with_the_core_run(
    analyzer: Analyzer, model_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P.E.K.K.A is the model's favorite, but if too few decks with the core run it, it's out."""
    monkeypatch.setattr(explain, "MIN_CORE_CARD_GAMES", 1000)
    items = insights.games(analyzer, ME)
    view = explain.report(
        analyzer, model_path, ME, items, insights.replace_card(analyzer, ME, items)
    )
    assert view is not None
    assert view["pick"] is None


def test_replaying_the_last_opponents_with_the_suggested_deck(
    analyzer: Analyzer, model_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The player's own 5 games (all won), played again with Cannon -> P.E.K.K.A: the same
    opponents, and the model expects more wins."""
    monkeypatch.setattr(explain, "MIN_REPLAY_GAMES", 3)
    items = insights.games(analyzer, ME)
    view = explain.report(
        analyzer, model_path, ME, items, insights.replace_card(analyzer, ME, items)
    )
    assert view is not None
    r = view["replay"]
    assert r["games"] == 5 and r["wins"] == 5
    assert 0 < r["expected"] < 5
    assert r["expected_pick"] > r["expected"]
    assert all(len(b["deck"]) == 8 and b["pick"] > b["now"] for b in r["battles"])


def test_a_battle_is_the_same_whichever_side_is_asked(model_path: Path) -> None:
    model = explain.load(model_path)
    assert model is not None
    a = [model.token(c * 10) for c in CYCLE]
    b = [model.token(c * 10) for c in (*CORE, MUSKETEER, KNIGHT, PEKKA)]
    p = explain.chance(model, a, b, (0.5, 0.2))
    assert p == pytest.approx(1 - explain.chance(model, b, a, (-0.5, -0.2)))
