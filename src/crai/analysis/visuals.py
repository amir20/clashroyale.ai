"""Data behind the visual cards Rook can show in an answer.

Each function returns a dict with a `type` the web app knows how to draw, or an `error` the model
reads instead of drawing anything. The model picks which one to show and names the cards; every
figure on screen comes from here. `scope` tells the model whose numbers they are.
"""

from collections.abc import Iterator, Sequence
from typing import Any

import polars as pl

from crai.analysis.analytics import Analyzer, CardRef, DeckResult
from crai.analysis.insights import (
    CLOSE_SHARED_CARDS,
    MEANINGFUL_GAIN,
    Game,
    _cards,
    _ids,
    _record,
    current_deck_key,
    elixir_standing,
    near_player,
    nemesis,
    record_against,
    trend,
    wins_and_losses,
)
from crai.collect.client import normalize_tag

MIN_VISUAL_GAMES = 100
MIN_DECK_PLAYERS = 20
MAX_TOP3_SHARE = 0.5  # a deck mostly played by three people says more about them than the deck
MIN_WORST_MATCHUP_GAMES = 5
MIN_TOP_CARD_USE = 0.01
COPY_DECK_URL = (
    "https://link.clashroyale.com/en?clashroyale://copyDeck?deck={deck}&l=Royals&tt=159000000"
)
ALL_PLAYERS = "All players' decks, not this player's and not only decks like theirs."


def _swap_pools(
    a: Analyzer, tag: str, items: list[Game], others: list[int], first: int, second: int
) -> Iterator[tuple[pl.DataFrame, bool, int]]:
    """Comparable decks running exactly one of the two cards, strictest selection first."""
    has_first, has_second = (pl.col("cards").list.contains(c) for c in (first, second))
    rest = (
        a.decks()
        .filter(pl.col("player_tag") != tag, has_first != has_second)
        .with_columns(shared=pl.col("cards").list.set_intersection(others).list.len())
    )
    near = near_player(rest, items)
    for pool, is_near in [*([(near, True)] if near is not None else []), (rest, False)]:
        for needed in (CLOSE_SHARED_CARDS, 4):
            yield pool.filter(pl.col("shared") >= needed), is_near, needed


def swap_view(
    a: Analyzer, tag: str, items: list[Game], out_card: str, in_card: str
) -> dict[str, Any]:
    """Like-for-like: decks sharing the player's other cards, running one card or the other."""
    key = current_deck_key(items)
    if key is None:
        return {"error": "This player has no recent games."}
    out_id, in_id = a.resolve_card(out_card), a.resolve_card(in_card)
    cards = _cards(a)
    if out_id == in_id:
        name = cards[out_id]["name"]
        return {"error": f"Both cards are {name}, so there is nothing to compare or draw."}
    mine = _ids(key)
    if out_id not in mine and in_id in mine:  # the card they already run goes on the left
        out_id, in_id = in_id, out_id
    others = [c for c in mine if c not in (out_id, in_id)]

    def side(pool: pl.DataFrame, cid: int) -> dict[str, Any]:
        running = pool.filter(pl.col("cards").list.contains(cid))
        rate = running["score"].mean() if running.height else None
        played = [g for g in items if cid in _ids(g["deck_key"])]
        return cards[cid] | {
            "games": running.height,
            "players": running["player_tag"].n_unique() if running.height else 0,
            "rate": rate,
            "player_own_record_with_it": _record(played) if played else "never played it",
        }

    def enough(pool: pl.DataFrame) -> bool:
        running = [pool.filter(pl.col("cards").list.contains(c)).height for c in (out_id, in_id)]
        return min(running) >= MIN_VISUAL_GAMES

    pools = list(_swap_pools(a, normalize_tag(tag), items, others, out_id, in_id))
    pool, near, needed = next((p for p in pools if enough(p[0])), pools[-1])
    out_side, in_side = side(pool, out_id), side(pool, in_id)
    verdict, gap = "unknown", None
    out_rate, in_rate = out_side.pop("rate"), in_side.pop("rate")
    if min(out_side["games"], in_side["games"]) >= MIN_VISUAL_GAMES:
        diff = in_rate - out_rate
        gap = round(diff, 3)
        margin = 1.96 * (0.25 / out_side["games"] + 0.25 / in_side["games"]) ** 0.5
        verdict = "even"
        if abs(diff) > max(margin, MEANINGFUL_GAIN):
            verdict = "better" if diff > 0 else "worse"
    range_note = "within the player's trophy range" if near else "at all trophy levels"
    return {
        "type": "swap",
        "out": out_side,
        "in": in_side,
        "verdict": verdict,
        "gap": gap,
        "family_decks": pool.height,
        "near": near,
        "min_shared_cards": needed,
        "scope": (
            f"Other players' decks sharing at least {needed} of this player's other cards, "
            f"{range_note}. Say 'decks like yours' and quote only `gap`, as wins per 100 games. "
            "The verdict is the conclusion: agree with it."
        ),
    }


def matchup_view(a: Analyzer, items: list[Game], card: str) -> dict[str, Any]:
    record = record_against(a, items, card)
    return record | {"type": "matchup", "card": _cards(a)[a.resolve_card(card)]}


def ranking_view(a: Analyzer, card: str, kind: str) -> dict[str, Any]:
    """Top partners for a card, or the opponent cards that beat decks with it."""
    cards = _cards(a)
    scope = ALL_PLAYERS
    if kind == "counters":
        summary, df = a.card_matchups(card, min_games=MIN_VISUAL_GAMES)
        baseline = summary["overall_win_rate"]
        name = cards[a.resolve_card(card)]["name"]
        scope += (
            f" win_rate is how often decks WITH {name} win against the listed card, so a low "
            f"number means the listed card beats {name}. It is not the listed card's own win rate."
        )
    else:
        kind = "pairs"
        summary, df = a.card_pairs(card, min_games=MIN_VISUAL_GAMES, sort_by="adj_win_rate")
        baseline = summary["win_rate"]
    return {
        "type": "ranking",
        "kind": kind,
        "card": cards[a.resolve_card(card)],
        "baseline": baseline,
        "rows": _ranked(df.head(5).to_dicts(), cards, baseline or 0.5),
        "scope": scope,
    }


def _ranked(
    rows: list[dict[str, Any]], cards: dict[int, dict[str, Any]], baseline: float
) -> list[dict[str, Any]]:
    return [
        cards[r["card_id"]]
        | {
            "win_rate": round(r["win_rate"], 3),
            "games": r["games"],
            "gain": round(r["win_rate"] - baseline, 3),
        }
        for r in rows
        if r["card_id"] in cards
    ]


def top_cards_view(a: Analyzer) -> dict[str, Any]:
    """The cards winning the most right now, among those people actually play."""
    df = (
        a.card_stats(min_games=MIN_VISUAL_GAMES, sort_by="adj_win_rate")
        .filter(pl.col("use_rate") >= MIN_TOP_CARD_USE)
        .head(5)
    )
    return {
        "type": "ranking",
        "kind": "top",
        "baseline": 0.5,
        "rows": _ranked(df.to_dicts(), _cards(a), 0.5),
        "scope": ALL_PLAYERS,
    }


def proven_decks(a: Analyzer, wanted: Sequence[CardRef], limit: int = 1) -> list[DeckResult]:
    """Decks winning across many players, ranked by what their sample size can vouch for."""
    return a.top_decks(
        include=wanted,
        min_games=MIN_VISUAL_GAMES,
        min_players=MIN_DECK_PLAYERS,
        max_top3_share=MAX_TOP3_SHARE,
        sort_by="lower_bound",
        limit=limit,
    )


def deck_view(a: Analyzer, wanted: list[str]) -> dict[str, Any]:
    """The best proven deck containing the requested cards."""
    decks = proven_decks(a, wanted)
    if not decks:
        return {"error": "No deck with those cards has been won with by enough different players."}
    d = decks[0]
    by_name = {c["name"]: c for c in _cards(a).values()}
    shown = [by_name[n] for n in d["cards"]]
    ids = ";".join(str(c["id"]) for c in shown)
    return {
        "type": "deck",
        "cards": shown,
        "games": d["games"],
        "players": d["players"],
        "win_rate": d["win_rate"],
        "avg_elixir": d["avg_elixir"],
        "copy_url": COPY_DECK_URL.format(deck=ids),
        "scope": ALL_PLAYERS,
    }


def worst_matchups_view(a: Analyzer, items: list[Game]) -> dict[str, Any]:
    n = nemesis(a, items, min_games=MIN_WORST_MATCHUP_GAMES)
    # The cards with enough games to trust go first, so the list leads with the real problem.
    rows = sorted((r for r in n["worst"] if r["diff"] < 0), key=lambda r: not r["real"])
    if not rows:
        return {"error": "No card has beaten this player often enough to name one."}
    return {
        "type": "worst_matchups",
        "rows": rows,
        "overall_win_rate": n["overall_win_rate"],
        "scope": n["scope"],
    }


def trend_view(a: Analyzer, items: list[Game]) -> dict[str, Any]:
    t = trend(a, items)
    if not t["enough"]:
        return {"error": f"Only {t['games']} games so far: too few to see a trend."}
    scope = "This player's own games on one deck, older vs newer."
    if t["verdict"] == "changed_decks":
        scope = "They changed decks, so older and newer games are not comparable. Don't quote them."
    return t | {"type": "trend", "scope": scope}


def close_games_view(items: list[Game]) -> dict[str, Any]:
    w = wins_and_losses(items)
    if not w["close_games"]["games"]:
        return {"error": "This player has no one-crown games yet."}
    return w | {"type": "close_games", "scope": "This player's own games."}


def elixir_view(a: Analyzer, items: list[Game]) -> dict[str, Any]:
    e = elixir_standing(a, items)
    return e if "error" in e else e | {"type": "elixir", "scope": ALL_PLAYERS}
