"""Data behind the visual cards Rook can show in an answer.

Each function returns a dict with a `type` the web app knows how to draw. The model picks which one
to show and names the cards; every figure on screen comes from here.
"""

from typing import Any

import polars as pl

from crai.analysis.analytics import Analyzer
from crai.analysis.insights import (
    MEANINGFUL_GAIN,
    Game,
    _cards,
    _family,
    _ids,
    current_deck_key,
    record_against,
)
from crai.collect.client import normalize_tag

MIN_VISUAL_GAMES = 100
COPY_DECK_URL = (
    "https://link.clashroyale.com/en?clashroyale://copyDeck?deck={deck}&l=Royals&tt=159000000"
)


def swap_view(
    a: Analyzer, tag: str, items: list[Game], out_card: str, in_card: str
) -> dict[str, Any]:
    """Decks like this player's: how those running each of the two cards do."""
    key = current_deck_key(items)
    if key is None:
        return {"error": "This player has no recent games."}
    out_id, in_id = a.resolve_card(out_card), a.resolve_card(in_card)
    cards = _cards(a)
    fam = _family(a, normalize_tag(tag), _ids(key))

    def side(cid: int) -> dict[str, Any]:
        running = fam.filter(pl.col("cards").list.contains(cid))
        rate = running["score"].mean() if running.height else None
        win_rate = round(rate, 3) if isinstance(rate, float) else None
        return cards[cid] | {
            "games": running.height,
            "players": running["player_tag"].n_unique() if running.height else 0,
            "win_rate": win_rate,
        }

    out_side, in_side = side(out_id), side(in_id)
    verdict = "unknown"
    if min(out_side["games"], in_side["games"]) >= MIN_VISUAL_GAMES:
        diff = in_side["win_rate"] - out_side["win_rate"]
        margin = 1.96 * (0.25 / out_side["games"] + 0.25 / in_side["games"]) ** 0.5
        verdict = "even"
        if abs(diff) > max(margin, MEANINGFUL_GAIN):
            verdict = "better" if diff > 0 else "worse"
    return {
        "type": "swap",
        "out": out_side,
        "in": in_side,
        "verdict": verdict,
        "family_decks": fam.height,
    }


def matchup_view(a: Analyzer, items: list[Game], card: str) -> dict[str, Any]:
    record = record_against(a, items, card)
    return record | {"type": "matchup", "card": _cards(a)[a.resolve_card(card)]}


def ranking_view(a: Analyzer, card: str, kind: str) -> dict[str, Any]:
    """Top partners for a card, or the opponent cards that beat decks with it."""
    cards = _cards(a)
    if kind == "counters":
        _, df = a.card_matchups(card, min_games=MIN_VISUAL_GAMES)
    else:
        kind = "pairs"
        _, df = a.card_pairs(card, min_games=MIN_VISUAL_GAMES, sort_by="adj_win_rate")
    rows = [
        cards[r["card_id"]] | {"win_rate": round(r["win_rate"], 3), "games": r["games"]}
        for r in df.head(5).to_dicts()
        if r["card_id"] in cards
    ]
    return {"type": "ranking", "kind": kind, "card": cards[a.resolve_card(card)], "rows": rows}


def deck_view(a: Analyzer, wanted: list[str]) -> dict[str, Any]:
    """The best proven deck containing the requested cards."""
    decks = a.top_decks(include=wanted, min_games=MIN_VISUAL_GAMES, limit=1)
    if not decks:
        return {"error": "No proven deck contains those cards in the last 7 days."}
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
    }
