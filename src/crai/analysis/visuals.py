"""Data behind the visual cards Rook can show in an answer.

Each function returns a dict with a `type` the web app knows how to draw, or an `error` the model
reads instead of drawing anything. The model picks which one to show and names the cards; every
figure on screen comes from here. `scope` tells the model whose numbers they are.
"""

from collections.abc import Sequence
from typing import Any

import polars as pl

from crai.analysis.analytics import (
    DEFAULT_FILTERS,
    Analyzer,
    CardRef,
    DeckResult,
    Filters,
)
from crai.analysis.insights import (
    STATUS,
    Game,
    SwapField,
    _cards,
    _ids,
    _played_as,
    _record,
    archetype,
    can_play,
    compared,
    core_match,
    current_deck_key,
    elixir_standing,
    match,
    near_filters,
    nemesis,
    package,
    record_against,
    slots,
    trend,
    trophy_scope,
    weak_spots,
    wins_and_losses,
)

MIN_VISUAL_GAMES = 100
MIN_DECK_PLAYERS = 20
MAX_TOP3_SHARE = 0.5  # a deck mostly played by three people says more about them than the deck
MIN_WORST_MATCHUP_GAMES = 5
MIN_TOP_CARD_USE = 0.01
COPY_DECK_URL = (
    "https://link.clashroyale.com/en?clashroyale://copyDeck?deck={deck}&l=Royals&tt=159000000"
)
ALL_PLAYERS = "All players' decks, not this player's and not only decks like theirs."
MIN_NEAR_DECKS = 2000  # decks with the card in the player's trophy range, before we trust it


def _where(items: list[Game] | None) -> tuple[Filters, str]:
    """Other players at this player's trophies when we know them, else everyone."""
    f = near_filters(items) if items else None
    if f is None:
        return DEFAULT_FILTERS, ALL_PLAYERS
    return f, (
        f"Other {trophy_scope(items or [])}, not this player's own games. "
        "Say 'players at your trophies'."
    )


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
    played = slots(a, tag, items)
    # The card coming in takes the leaving card's slot, so both are judged in that form.
    slot = played.get(out_id, 0)
    if not can_play(cards[in_id], slot):
        form = STATUS[slot]
        return {
            "error": f"{cards[in_id]['name']} has no {form} form, so it can't take "
            f"{cards[out_id]['name']}'s {form} slot. Compare it with one of their plain cards."
        }

    # Built on the player's core when there is one: the swap changes a flex card, and is judged
    # on decks sharing the core, like the story's suggestion.
    arch = archetype(a, tag, items)
    out_v, in_v = out_id * 10 + slot, in_id * 10 + slot
    if arch and out_v in arch.core:
        core = ", ".join(_played_as(cards[v // 10], v % 10)["name"] for v in arch.core)
        flex = ", ".join(_played_as(cards[v // 10], v % 10)["name"] for v in arch.flex)
        return {
            "error": f"{cards[out_id]['name']} is a staple: it's in the core ({core}) that "
            f"{arch.decks.height:,} decks like theirs are built on, so it isn't swapped. "
            f"Their flex cards are {flex}: compare one of those."
        }
    if arch and out_v in arch.flex:
        h2h = package(arch, items, [out_v], [in_v])
        how = core_match(arch, h2h, cards)
    else:
        n_special = sum(1 for s in played.values() if s)
        h2h = SwapField(a, tag, items, others, n_special).compare(out_id, in_id, slot)
        how = match(h2h, cards)

    def side(cid: int, games: int, players: int) -> dict[str, Any]:
        own = [g for g in items if cid in _ids(g["deck_key"])]
        return _played_as(cards[cid], slot) | {
            "games": games,
            "players": players,
            "player_own_record_with_it": _record(own) if own else "never played it",
        }

    out_side = side(out_id, h2h.out_games, h2h.out_players)
    in_side = side(in_id, h2h.in_games, h2h.in_players)
    verdict, gap, near, needed = h2h.verdict, h2h.gap, h2h.near, h2h.needed
    slot_note = f", each card played as {STATUS[slot]}" if slot else ", both cards played plain"
    range_note = "within the player's trophy range" if near else "at all trophy levels"
    if how.get("core"):
        names = ", ".join(c["name"] for c in how["core"])
        decks_note = f"Other players' decks built on this player's core ({names})"
    elif h2h.exact:
        decks_note = "Other players running this player's exact deck with only this card changed"
    else:
        decks_note = (
            f"Other players' decks sharing at least {needed} of this player's "
            f"{len(others)} other cards (so up to {len(others) - needed} more changed: "
            "`match.also` is the most common other change; mention it, the swap may need it)"
        )
    watch = weak_spots(a, h2h)
    return {
        "type": "swap",
        "out": out_side,
        "in": in_side,
        "verdict": verdict,
        "gap": gap,
        "family_decks": h2h.decks,
        "near": near,
        "min_shared_cards": needed,
        "match": how,
        "watch_out": watch,
        "scope": (
            f"{decks_note}, with as many Evolution/Hero slots{slot_note}, {range_note}. "
            "Say 'decks like yours' and quote only `gap`, as wins per 100 games. "
            "The verdict is the conclusion: agree with it. `watch_out` lists opponent cards "
            "the new card's decks do clearly worse against (gap per 100): warn about them."
        ),
    }


MAX_REPLACEMENTS = 5  # candidates judged head to head; each one is a filter over the pool
# Games a side needs before a replacement's comparison stops widening. The closest decks to a
# Hero Berserker deck held 189 games: a margin of ±7 per 100, so nothing was ever "better".
MIN_REPLACEMENT_GAMES = 1000
NEW_DECK_CHANGES = 3  # a "new deck" changes at least this many of the player's 8 cards


def replacements(
    a: Analyzer, tag: str, items: list[Game], out_card: str, exclude: Sequence[str] = ()
) -> dict[str, Any]:
    """What other players with the player's other cards run in this card's slot, and how each
    one does against the card itself, head to head (the same test as `swap_view`)."""
    key = current_deck_key(items)
    if key is None:
        return {"error": "This player has no recent games."}
    cards = _cards(a)
    mine = _ids(key)
    out_id = a.resolve_card(out_card)
    if out_id not in mine:
        names = ", ".join(cards[c]["name"] for c in mine if c in cards)
        return {"error": f"{cards[out_id]['name']} isn't in their deck ({names})."}
    others = [c for c in mine if c != out_id]
    played = slots(a, tag, items)
    slot = played.get(out_id, 0)
    n_special = sum(1 for s in played.values() if s)
    swaps = SwapField(a, tag, items, others, n_special)
    pool, near = next(swaps.pools())
    # Decks without the card: what fills its slot is whatever isn't one of the player's cards.
    fills = (
        pool.filter(~pl.col("cards").list.contains(out_id))
        .select(pl.col("cards").explode())
        .filter(~pl.col("cards").is_in(mine))
        .group_by("cards")
        .len()
        .sort("len", descending=True)
    )
    banned = set(a.resolve_cards(exclude))
    tried = [
        c
        for c in fills["cards"].to_list()
        if c in cards and c not in banned and can_play(cards[c], slot)
    ]
    order = {"better": 0, "even": 1, "worse": 2, "unknown": 3}
    judged = [
        (in_id, swaps.compare(out_id, in_id, slot, MIN_REPLACEMENT_GAMES))
        for in_id in tried[:MAX_REPLACEMENTS]
    ]
    judged.sort(key=lambda j: (order[j[1].verdict], -(j[1].gap or 0.0)))  # real upgrades first
    found = [
        _played_as(cards[in_id], slot)
        | {
            "games": h2h.in_games,
            "players": h2h.in_players,
            "win_rate": h2h.in_rate,
            "gap": h2h.gap,
            "margin": h2h.margin,
            "verdict": h2h.verdict,
        }
        for in_id, h2h in judged
    ]
    form = f" (played as {STATUS[slot]})" if slot else ""
    # Their deck with the best replacement in, ready to copy: the answer shows it.
    best = next(((i, h) for i, h in judged if h.verdict in {"better", "even"}), None)
    deck = None
    if best:
        in_id, h2h = best
        ids = [in_id if c == out_id else c for c in mine]
        shown = compared(cards, ids, set(mine))
        deck = {
            "type": "deck",
            "cards": shown,
            "games": h2h.in_games,
            "players": h2h.in_players,
            "win_rate": h2h.in_rate,
            "avg_elixir": round(sum(c["elixir"] or 0 for c in shown) / len(shown), 2),
            "copy_url": COPY_DECK_URL.format(deck=";".join(str(c["id"]) for c in shown)),
            "scope": f"Decks like theirs with {cards[in_id]['name']} in place of "
            f"{cards[out_id]['name']}, "
            + ("at their trophies" if near else "at all trophies")
            + ", last 7 days",
        }
    return {
        "card": _played_as(cards[out_id], slot),
        "replacements": found,
        "deck": deck,
        "scope": (
            f"Other players' decks sharing this player's other cards, "
            f"{'within the player' + chr(39) + 's trophy range' if near else 'at all trophies'}. "
            f"Each replacement is compared head to head with {cards[out_id]['name']}{form} in "
            "the same slot: `gap` is its win rate minus the card's, per game; `verdict` says "
            "whether the gap is bigger than the margin. 'better' is a proven upgrade; 'even' "
            "means too close to call yet (not equal): if they want a change anyway, the even one "
            "with the biggest gap and most games is the one to try. None of these cards is in "
            "the player's deck already, and none they ruled out. `deck` is their deck with the "
            "first better-or-even replacement in; it's shown to them with a copy button, so "
            "recommend that one."
        ),
    }


def matchup_view(a: Analyzer, items: list[Game], card: str) -> dict[str, Any]:
    record = record_against(a, items, card)
    cards = _cards(a)
    mine = [cards[c]["name"] for c in _ids(current_deck_key(items)) if c in cards]
    return record | {"type": "matchup", "card": cards[a.resolve_card(card)], "your_deck": mine}


def ranking_view(
    a: Analyzer, card: str, kind: str, items: list[Game] | None = None
) -> dict[str, Any]:
    """Top partners for a card, or the opponent cards that beat decks with it.

    Ranked among players at this player's trophies when there are enough of them.
    """
    f, scope = _where(items)
    if f is not DEFAULT_FILTERS:
        size = a.card_pairs(card, f, min_games=MIN_VISUAL_GAMES)[0]["decks_with_card"]
        if size < MIN_NEAR_DECKS:
            f, scope = DEFAULT_FILTERS, ALL_PLAYERS
    cards = _cards(a)
    if kind == "counters":
        summary, df = a.card_matchups(card, f, min_games=MIN_VISUAL_GAMES)
        baseline = summary["overall_win_rate"]
        name = cards[a.resolve_card(card)]["name"]
        scope += (
            f" win_rate is how often decks WITH {name} win against the listed card, so a low "
            f"number means the listed card beats {name}. It is not the listed card's own win rate."
        )
    else:
        kind = "pairs"
        summary, df = a.card_pairs(card, f, min_games=MIN_VISUAL_GAMES, sort_by="adj_win_rate")
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


def top_cards_view(a: Analyzer, items: list[Game] | None = None) -> dict[str, Any]:
    """The cards winning the most right now at this player's trophies, among those played."""
    f, scope = _where(items)
    df = (
        a.card_stats(f, min_games=MIN_VISUAL_GAMES, sort_by="adj_win_rate")
        .filter(pl.col("use_rate") >= MIN_TOP_CARD_USE)
        .head(5)
    )
    return {
        "type": "ranking",
        "kind": "top",
        "baseline": 0.5,
        "rows": _ranked(df.to_dicts(), _cards(a), 0.5),
        "scope": scope,
    }


def proven_decks(
    a: Analyzer,
    wanted: Sequence[CardRef],
    limit: int = 1,
    f: Filters = DEFAULT_FILTERS,
    exclude: Sequence[CardRef] = (),
) -> list[DeckResult]:
    """Decks winning across many players, ranked by what their sample size can vouch for."""
    decks = a.top_decks(
        f,
        include=wanted,
        exclude=exclude,
        min_games=MIN_VISUAL_GAMES,
        min_players=MIN_DECK_PLAYERS,
        max_top3_share=MAX_TOP3_SHARE,
        sort_by="lower_bound",
        limit=limit,
    )
    # A deck we can't vouch for winning more than it loses is no recommendation.
    return [d for d in decks if d["win_rate"] - d["margin"] > 0.5]


def near_proven_decks(
    a: Analyzer,
    wanted: Sequence[CardRef],
    items: list[Game] | None,
    limit: int = 1,
    exclude: Sequence[CardRef] = (),
    unlike: Sequence[int] = (),
) -> tuple[list[DeckResult], str]:
    """Proven decks at this player's trophies, or across everyone if none are proven there.

    `unlike` is the player's deck when they want a new one: decks keeping more than
    8 - NEW_DECK_CHANGES of its cards are skipped.
    """
    f, scope = _where(items)
    tries = [(f, scope)] if f is not DEFAULT_FILTERS else []
    tries.append((DEFAULT_FILTERS, ALL_PLAYERS))
    ids = {name: cid for cid, name in a.card_names().items()}
    for where, label in tries:
        # A new deck is picked from more candidates, since most good ones resemble theirs.
        found = proven_decks(a, wanted, 50 if unlike else limit, where, exclude)
        if unlike:
            keep = set(unlike)
            found = [
                d
                for d in found
                if len({ids[n] for n in d["cards"] if n in ids} & keep) <= 8 - NEW_DECK_CHANGES
            ]
        if found:
            return found[:limit], label
    return [], ALL_PLAYERS


def proven_for_player(a: Analyzer, items: list[Game]) -> dict[str, Any] | None:
    """A proven deck from players at their trophies that keeps the cards they play most.

    Tries the three cards they've run in the most games, then two, then their single favorite.
    """
    counts: dict[int, int] = {}
    for g in items:
        for cid in _ids(g["deck_key"]):
            counts[cid] = counts.get(cid, 0) + 1
    favorites = sorted(counts, key=lambda c: counts[c], reverse=True)[:3]
    names = _cards(a)
    f, scope = _where(items)
    for where, label in ((f, scope), (DEFAULT_FILTERS, ALL_PLAYERS)):
        for n in (3, 2, 1):
            kept = favorites[:n]
            if decks := proven_decks(a, [str(c) for c in kept], 1, where):
                return _deck_card(a, decks[0], label, items) | {
                    "kept": [names[c]["name"] for c in kept if c in names]
                }
    return None


def deck_view(
    a: Analyzer,
    wanted: list[str],
    items: list[Game] | None = None,
    exclude: Sequence[str] = (),
    new: bool = False,
) -> dict[str, Any]:
    """The best proven deck containing the requested cards, without the excluded ones. With
    `new`, it must change at least NEW_DECK_CHANGES of the player's current cards."""
    mine = _ids(current_deck_key(items)) if new and items else []
    decks, scope = near_proven_decks(a, wanted, items, exclude=exclude, unlike=mine)
    if not decks:
        what = "new deck (3 or more cards different from theirs)" if new else "deck"
        return {
            "error": f"No proven {what} with those cards"
            + (" and without the cards they ruled out" if exclude else "")
            + " at their trophies or across all players. Try fewer cards to keep."
        }
    view = _deck_card(a, decks[0], scope, items)
    if new:
        view["changed_cards"] = sum(1 for c in view["cards"] if not c.get("yours"))
    return view


def _deck_card(
    a: Analyzer, d: DeckResult, scope: str, items: list[Game] | None = None
) -> dict[str, Any]:
    cards = _cards(a)
    by_name = {c["name"]: c for c in cards.values()}
    shown = [by_name[n] for n in d["cards"]]
    if items:  # mark which of these the player already runs
        shown = compared(cards, [c["id"] for c in shown], set(_ids(current_deck_key(items))))
    ids = ";".join(str(c["id"]) for c in shown)
    return {
        "type": "deck",
        "cards": shown,
        "games": d["games"],
        "players": d["players"],
        "win_rate": d["win_rate"],
        "avg_elixir": d["avg_elixir"],
        "copy_url": COPY_DECK_URL.format(deck=ids),
        "scope": scope,
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
