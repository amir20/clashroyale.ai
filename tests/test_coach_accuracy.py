"""Guards found by playing the product: Rook must not draw or say things the games don't support."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from conftest import HOG, ME, PEKKA

from crai.analysis import insights, visuals
from crai.analysis.analytics import Analyzer, CardNotFound
from crai.coach import voice
from crai.coach.tools import CoachContext, player_tools


def game(result: str, **kw: Any) -> insights.Game:
    base = {
        "battle_time": "2026-11-07T10:00:00Z",
        "result": result,
        "crowns": 1 if result == "win" else 0,
        "opponent_crowns": 0 if result == "win" else 1,
        "elixir_leaked": 5.0,
        "trophy_change": 30 if result == "win" else -30,
        "deck_key": f"{HOG}",
        "opponent_deck": f"{PEKKA}",
    }
    return base | kw


def test_a_card_cannot_be_swapped_with_itself(analyzer: Analyzer) -> None:
    items = insights.games(analyzer, ME)
    assert "error" in visuals.swap_view(analyzer, ME, items, "hog", "Hog Rider")


def test_the_card_the_player_runs_goes_on_the_left(analyzer: Analyzer) -> None:
    items = insights.games(analyzer, ME)
    view = visuals.swap_view(analyzer, ME, items, "pekka", "hog")
    assert view["out"]["name"] == "Hog Rider"
    assert view["in"]["name"] == "P.E.K.K.A"
    assert view["in"]["player_own_record_with_it"] == "never played it"


@pytest.mark.parametrize("fake", ["Fire Golem", "Lava Knight", "Mega Wizard"])
def test_made_up_cards_are_not_matched_to_real_ones(analyzer: Analyzer, fake: str) -> None:
    with pytest.raises(CardNotFound):
        analyzer.resolve_card(fake)


@pytest.mark.parametrize(
    ("typo", "name"),
    [("Muskateer", "Musketeer"), ("Mega Knigt", "Mega Knight"), ("xbow", "X-Bow")],
)
def test_typos_still_find_the_card(analyzer: Analyzer, typo: str, name: str) -> None:
    assert analyzer.card_names()[analyzer.resolve_card(typo)] == name


def test_trend_compares_one_deck_with_itself(analyzer: Analyzer) -> None:
    items = [game("loss", battle_time="2026-11-08T10:00:00Z") for _ in range(30)]
    items += [game("win", battle_time="2026-11-06T10:00:00Z") for _ in range(30)]
    t = insights.trend(analyzer, items)
    assert (t["verdict"], t["same_deck"], t["more_games"]) == ("slipping", True, 0)
    assert (t["older"]["wins"], t["newer"]["wins"]) == (30, 0)
    assert t["current_streak"] == {"kind": "loss", "length": 30}
    assert [d["day"] for d in t["days"]] == ["2026-11-06", "2026-11-08"]


def test_changing_decks_is_not_called_a_trend(analyzer: Analyzer) -> None:
    newer = [game("loss", deck_key=f"{PEKKA}") for _ in range(30)]
    t = insights.trend(analyzer, newer + [game("win") for _ in range(30)])
    assert t["verdict"] == "changed_decks"
    assert [d["games"] for d in t["decks"]] == [30, 30]
    assert t["decks"][-1]["current"]
    first = t["decks"][0]
    assert ([c["name"] for c in first["added"]], [c["name"] for c in first["removed"]]) == (
        ["Hog Rider"],
        ["P.E.K.K.A"],
    )
    assert t["more_games"] == insights._games_to_settle(insights.SWING_WORTH_SETTLING) - 30
    assert "changing decks" in voice.trend(t)


def test_a_small_trend_says_what_would_settle_it(analyzer: Analyzer) -> None:
    alternating = [game("win" if i % 2 else "loss") for i in range(40)]
    flat = insights.trend(analyzer, alternating)
    assert (flat["verdict"], flat["more_games"]) == ("steady", None)
    assert "no change" in voice.trend(flat)
    tilted = [game("win" if i % 3 else "loss") for i in range(20)]
    tilted += [game("win" if i % 2 else "loss") for i in range(20)]
    assert insights.trend(analyzer, tilted)["more_games"] > 0
    assert insights.trend(analyzer, alternating[:5]) == {"enough": False, "games": 5}


def test_tougher_opponents_are_measured_from_their_trophies(analyzer: Analyzer) -> None:
    newer = [game("win", opponent_trophies=15000) for _ in range(15)]
    older = [game("win", opponent_trophies=14000) for _ in range(15)]
    assert insights.trend(analyzer, newer + older)["lobbies"] == {
        "older": 14000,
        "newer": 15000,
        "harder": True,
    }


def test_swaps_report_a_gap_not_inflated_win_rates(analyzer: Analyzer) -> None:
    view = visuals.swap_view(analyzer, ME, insights.games(analyzer, ME), "hog", "pekka")
    assert "win_rate" not in view["out"] and "win_rate" not in view["in"]
    assert "gap" in view


def test_blowouts_do_not_skew_the_elixir_figures() -> None:
    blowouts = [game("loss", opponent_crowns=3, elixir_leaked=20.0) for _ in range(3)]
    close_losses = [game("loss", elixir_leaked=5.0) for _ in range(5)]
    w = insights.wins_and_losses([game("win", elixir_leaked=4.0), *blowouts, *close_losses])
    assert w["elixir_leaked_losses"] == 5.0
    assert w["elixir_leaked_close_losses"] == 5.0
    assert w["elixir_leaked_three_crowned"] == 20.0


def test_elixir_standing(analyzer: Analyzer) -> None:
    items = insights.games(analyzer, ME)
    e = insights.elixir_standing(analyzer, items)
    assert 0 <= e["higher_than"] <= 1
    assert sum(b["yours"] for b in e["bands"]) == 1


def test_worst_matchups_are_only_cards_below_the_overall_rate(analyzer: Analyzer) -> None:
    items = insights.games(analyzer, ME)
    assert "error" in visuals.worst_matchups_view(analyzer, items)  # every game had the same deck


def test_trusted_matchups_lead_the_list(analyzer: Analyzer) -> None:
    pekka, musketeer = PEKKA, 26000014
    decks = [f"{pekka}"] * 20 + [f"{musketeer}"] * 6 + ["26000000"] * 20
    played = [game("loss", opponent_deck=d) for d in decks[:26]]
    played += [game("win", opponent_deck="26000000") for _ in range(20)]
    played[6:9] = [game("win", opponent_deck=f"{pekka}") for _ in range(3)]
    view = visuals.worst_matchups_view(analyzer, played)
    assert view["rows"][0]["name"] == "P.E.K.K.A" and view["rows"][0]["real"]
    assert view["rows"][1]["name"] == "Musketeer" and not view["rows"][1]["real"]


def test_decks_mostly_played_by_three_people_are_not_proven(analyzer: Analyzer) -> None:
    assert analyzer.top_decks(include=["hog"], min_games=1, max_top3_share=0.01) == []
    assert analyzer.top_decks(include=["hog"], min_games=1, min_players=10_000) == []
    assert analyzer.top_decks(include=["hog"], min_games=1, sort_by="lower_bound")


def test_rankings_show_how_far_each_card_is_from_the_baseline(analyzer: Analyzer) -> None:
    view = visuals.ranking_view(analyzer, "hog", "pairs")
    assert view["rows"]
    for row in view["rows"]:
        assert row["gain"] == pytest.approx(row["win_rate"] - view["baseline"], abs=0.002)


def test_the_model_gets_a_streak_it_cannot_misread(analyzer: Analyzer) -> None:
    svc = cast(CoachContext, SimpleNamespace(analyzer=analyzer))
    o = player_tools(svc, ME)["get_overview"]({})
    assert "recent" not in o and "streak" not in o
    assert set(o["current_streak"]) == {"kind", "length"}
    assert set(o["last_20_games"]) == {"wins", "losses"}


def test_records_say_whether_the_card_was_faced_or_played(analyzer: Analyzer) -> None:
    items = insights.games(analyzer, ME)
    faced = insights.record_against(analyzer, items, "pekka")
    played = insights.record_with(analyzer, items, "hog")
    assert faced["scope"] == insights.AGAINST_SCOPE
    assert played["scope"] == insights.WITH_SCOPE
    assert insights.record_with(analyzer, items, "pekka")["games"] == 0


@pytest.mark.parametrize(
    ("said", "shown"),
    [
        ("Swap draw is up on screen. You should try it.", "You should try it."),
        ("Try it. The card is on screen under my reply.", "Try it."),
        ("The tool found nothing.", "I found nothing."),
        ("The tool says it's even.", "I see it's even."),
        ("That gap is smaller than the margin, so luck.", "That gap is small, so luck."),
        ("Those margins are small.", "Those gaps are small."),
        ("Check the data first.", "Check the games first."),
        ("Only on screen.", "Only on screen."),
        ("Win rate is below 50%.", "Win rate is below 50%."),
    ],
)
def test_tidy_enforces_the_voice_rules(said: str, shown: str) -> None:
    assert voice.tidy(said) == shown


def test_swaps_are_never_described_as_all_players() -> None:
    said = "Musketeer wins 61% across all players."
    assert voice.tidy(said, only_similar_decks=True) == "Musketeer wins 61% in decks like yours."
    assert voice.tidy(said) == said


def test_story_lines_use_the_computed_numbers() -> None:
    period = {"win_rate": 0.5, "wins": 1, "losses": 1, "games": 2}
    t = {
        "older": period | {"win_rate": 0.65},
        "newer": period,
        "verdict": "steady",
        "more_games": 72,
        "lobbies": None,
    }
    assert voice.trend(t) == (
        "Same deck: you won 65% of your older games and 50% of your newer ones. "
        "Too close to call. About 70 more games would settle it."
    )
    e = {"avg_elixir": 4.12, "higher_than": 0.72, "costs_wins": False}
    assert "more than 72% of decks" in voice.elixir(e)


def test_replace_line_quotes_a_gap(analyzer: Analyzer) -> None:
    weak = {"name": "Knight", "gain": -0.03, "real": False}
    line = voice.replace_card(
        {"enough": True, "family_decks": 900, "weakest": weak, "candidates": []}
    )
    assert "3.0 fewer games per 100" in line


def test_rook_does_not_judge_thin_records() -> None:
    assert "No recent games" in voice.replace_card({"enough": False, "no_games": True})
    assert "unusual" in voice.replace_card({"enough": False, "family_decks": 3})
    close = {"close_games": {"wins": 0, "games": 3, "win_rate": 0.0}}
    assert "Too few" in voice.close_games(close)
    best = {"wins": 4, "losses": 4, "games": 8, "win_rate": 0.5, "margin": 0.35}
    other = {"wins": 0, "losses": 4, "games": 4, "win_rate": 0.0, "margin": 0.49}
    assert "clearly ahead" not in voice.best_deck({"decks": [best, other]})


def test_only_deck_cards_can_be_picked(analyzer: Analyzer) -> None:
    names = [c["name"] for c in insights.playable_cards(analyzer)]
    assert names == sorted(names)
    assert "Hog Rider" in names
