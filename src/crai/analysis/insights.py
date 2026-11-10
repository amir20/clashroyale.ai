"""Player-facing insights: what the web app shows for one player's tag.

Everything here is computed from data, with a margin of error on every comparison, so free
answers need no AI call and can't be hallucinated. `coach.py` turns these results into words.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from math import ceil
from statistics import median
from typing import Any

import polars as pl

from crai.analysis.analytics import (
    DEFAULT_SINCE_DAYS,
    EXCLUDED_TYPES,
    Analyzer,
    Filters,
    _margin,
)
from crai.collect.client import normalize_tag

type Game = dict[str, Any]

MIN_FAMILY_DECKS = 200
MIN_CLOSE_FAMILY_DECKS = 1000  # enough decks to afford requiring a closer match
CLOSE_SHARED_CARDS = 5
NEAR_TROPHIES_SHARE = 0.07  # "your trophy range" is within this share of the player's trophies
MIN_TREND_GAMES = 20
MIN_SAME_DECK_GAMES = 10  # per half, to compare a deck with itself
MIN_DECK_TIMELINE_GAMES = 5
MAX_DECK_TIMELINE = 5
HARDER_LOBBY_TROPHIES = 200
SMALLEST_SWING = 0.05  # less than this isn't a change worth chasing
SWING_WORTH_SETTLING = 0.15
AGAINST_SCOPE = "Games where the OPPONENT's deck had this card. The player was not using it."
WITH_SCOPE = "Games where the player's OWN deck had this card."
SCORE = {"win": 1.0, "draw": 0.5, "loss": 0.0}
MEANINGFUL_GAIN = 0.02  # with huge samples, tiny differences clear the margin but don't matter
MIN_CANDIDATE_PLAYERS = 100  # fewer, and one strong player can make a card look good
MIN_MATCHUP_GAMES = 15  # we scan ~60 opponent cards, so a few games will look extreme by chance


def _cards(a: Analyzer) -> dict[int, dict[str, Any]]:
    rows = a.query("SELECT * FROM cards").to_dicts()
    return {
        r["id"]: {
            "id": r["id"],
            "name": r["name"],
            "elixir": r["elixir"],
            "rarity": r["rarity"],
            "icon": r["icon_url"],
            "evo_icon": r.get("evo_icon_url"),
            "hero_icon": r.get("hero_icon_url"),
        }
        for r in rows
    }


def playable_cards(a: Analyzer) -> list[dict[str, Any]]:
    """Every card that can go in a deck, alphabetically."""
    tower = {r["card_id"] for r in a.cards().iter_rows(named=True) if r["is_support"]}
    every = _cards(a).values()
    return sorted((c for c in every if c["id"] not in tower), key=lambda c: c["name"])


# The API's evolutionLevel for a card in a battle. 3 only occurs on cards with both modes.
STATUS = {1: "evolution", 2: "hero", 3: "both"}


def _played_as(card: dict[str, Any], evolution_level: int | None) -> dict[str, Any]:
    """The card as it was actually played: its status and matching official art."""
    status = STATUS.get(evolution_level or 0)
    art = {"evolution": card["evo_icon"], "hero": card["hero_icon"], "both": card["hero_icon"]}.get(
        status or ""
    )
    return card | {"status": status, "icon": art or card["icon"]}


def _ids(key: str | None) -> list[int]:
    return [int(x) for x in key.split("-")] if key else []


def games(a: Analyzer, tag: str) -> list[Game]:
    """The player's real 1v1 games with normal decks, newest first."""
    excluded = ",".join("?" * len(EXCLUDED_TYPES))
    return a.query(
        f"""SELECT b.id AS battle_id, b.battle_time, b.battle_type, me.result, me.crowns,
                   me.opponent_crowns, me.trophy_change, me.starting_trophies,
                   me.elixir_leaked, me.deck_key,
                   o.name AS opponent, o.deck_key AS opponent_deck,
                   o.starting_trophies AS opponent_trophies
            FROM battle_participants me
            JOIN battles b ON b.id = me.battle_id
            JOIN battle_participants o ON o.battle_id = me.battle_id AND o.side <> me.side
            WHERE me.player_tag = ? AND b.team_size = 1 AND me.deck_key IS NOT NULL
              AND o.deck_key IS NOT NULL AND me.result IS NOT NULL
              AND coalesce(b.deck_selection, 'collection') = 'collection'
              AND coalesce(b.battle_type, '') NOT IN ({excluded})
            ORDER BY b.battle_time DESC""",
        (normalize_tag(tag), *EXCLUDED_TYPES),
    ).to_dicts()


def _rate(items: list[Game]) -> float:
    return sum(SCORE[g["result"]] for g in items) / len(items) if items else 0.0


def _record(items: list[Game]) -> dict[str, Any]:
    c = Counter(g["result"] for g in items)
    return {
        "games": len(items),
        "wins": c["win"],
        "losses": c["loss"],
        "draws": c["draw"],
        "win_rate": round(_rate(items), 3),
        "margin": _margin(len(items)),
    }


def _streak(items: list[Game]) -> dict[str, Any]:
    if not items:
        return {"kind": None, "length": 0}
    first = items[0]["result"]
    n = next((i for i, g in enumerate(items) if g["result"] != first), len(items))
    return {"kind": first, "length": n}


def current_deck_key(items: list[Game]) -> str | None:
    """The deck they most recently played (more reliable than the profile's currentDeck)."""
    return items[0]["deck_key"] if items else None


def overview(a: Analyzer, tag: str, items: list[Game] | None = None) -> dict[str, Any]:
    tag = normalize_tag(tag)
    items = games(a, tag) if items is None else items
    rows = a.query("SELECT * FROM players WHERE tag = ?", (tag,)).to_dicts()
    if not rows:
        raise LookupError(tag)
    p = rows[0]
    cards = _cards(a)
    # The newest battle says how each card was really played (slot order, Evolution, Hero).
    played = (
        a.query(
            """SELECT card_id, level + (? - max_level) AS level, evolution_level
               FROM participant_cards
               WHERE battle_id = ? AND player_tag = ? AND kind = 'deck' ORDER BY slot""",
            (a._max_common_level, items[0]["battle_id"], tag),
        ).to_dicts()
        if items
        else []
    )
    deck = [
        _played_as(cards[r["card_id"]], r["evolution_level"]) | {"level": r["level"]}
        for r in played
        if r["card_id"] in cards
    ]
    ladder = next((g for g in items if g["battle_type"] == "trail"), None)
    trophies = p["trophies"]
    if ladder and ladder["starting_trophies"]:  # the profile field stops at the road's cap
        trophies = ladder["starting_trophies"] + (ladder["trophy_change"] or 0)
    known = [g["trophy_change"] for g in items if g["trophy_change"] is not None]
    return {
        "player": {
            "tag": tag,
            "name": p["name"],
            "trophies": trophies,
            "clan": p["clan_name"],
            "arena": p["arena"],
            "exp_level": p["exp_level"],
            "lifetime_wins": p["wins"],
            "lifetime_losses": p["losses"],
            "three_crown_wins": p["three_crown_wins"],
        },
        "deck": deck,
        "avg_elixir": round(sum(c["elixir"] or 0 for c in deck) / len(deck), 1) if deck else None,
        "record": _record(items),
        "streak": _streak(items),
        "net_trophies": sum(known),
        "recent": [g["result"] for g in items[:20]],
    }


def wins_and_losses(items: list[Game]) -> dict[str, Any]:
    wins = [g for g in items if g["result"] == "win"]
    losses = [g for g in items if g["result"] == "loss"]

    def avg(rows: list[Game], f: str) -> float | None:
        vals = [g[f] for g in rows if g[f] is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    def leaked(rows: list[Game]) -> float | None:
        """The typical game, so a few blowouts don't drag the figure up."""
        vals = [g["elixir_leaked"] for g in rows if g["elixir_leaked"] is not None]
        return round(median(vals), 2) if vals else None

    close = [g for g in items if abs(g["crowns"] - g["opponent_crowns"]) == 1]
    blowouts = [g for g in losses if g["opponent_crowns"] == 3]
    return {
        "wins": len(wins),
        "losses": len(losses),
        "close_games": _record(close),
        "three_crown_wins": sum(g["crowns"] == 3 for g in wins),
        "three_crowned_losses": sum(g["opponent_crowns"] == 3 for g in losses),
        "crowns_for": avg(items, "crowns"),
        "crowns_against": avg(items, "opponent_crowns"),
        "elixir_leaked_wins": leaked(wins),
        "elixir_leaked_losses": leaked(losses),
        "elixir_leaked_close_wins": leaked([g for g in close if g["result"] == "win"]),
        "elixir_leaked_close_losses": leaked([g for g in close if g["result"] == "loss"]),
        "elixir_leaked_three_crowned": leaked(blowouts),
    }


def _same_deck_games(older: list[Game], newer: list[Game]) -> tuple[list[Game], list[Game]] | None:
    """The older and newer games of the deck played most in both halves, if one was."""

    def by_deck(games: list[Game]) -> Counter[str]:
        return Counter(g["deck_key"] for g in games)

    before, after = by_deck(older), by_deck(newer)
    shared = [k for k in before if min(before[k], after[k]) >= MIN_SAME_DECK_GAMES]
    if not shared:
        return None
    deck = max(shared, key=lambda k: min(before[k], after[k]))
    return [g for g in older if g["deck_key"] == deck], [g for g in newer if g["deck_key"] == deck]


def _games_to_settle(swing: float) -> int:
    """Total games, split evenly in two, for a change this size to clear the margin of error."""
    return ceil((1.96 / swing) ** 2)


def _lobbies(older: list[Game], newer: list[Game]) -> dict[str, Any] | None:
    """Did the opponents get tougher? Measured by their trophies when the game started."""

    def typical(games: list[Game]) -> float | None:
        vals = [g["opponent_trophies"] for g in games if g.get("opponent_trophies")]
        return median(vals) if vals else None

    before, after = typical(older), typical(newer)
    if before is None or after is None:
        return None
    return {
        "older": round(before),
        "newer": round(after),
        "harder": after - before >= HARDER_LOBBY_TROPHIES,
    }


def _decks_over_time(a: Analyzer, items: list[Game]) -> list[dict[str, Any]]:
    """Each deck played enough to matter, oldest first."""
    cards = _cards(a)
    grouped: defaultdict[str, list[Game]] = defaultdict(list)
    for g in reversed(items):
        grouped[g["deck_key"]].append(g)
    current = current_deck_key(items)
    now = set(_ids(current))

    def named(ids: set[int]) -> list[dict[str, Any]]:
        return sorted((cards[c] for c in ids if c in cards), key=lambda c: c["name"])

    return [
        _record(gs)
        | {
            "current": key == current,
            "added": named(set(_ids(key)) - now),  # compared with the deck they play now
            "removed": named(now - set(_ids(key))),
        }
        for key, gs in grouped.items()
        if len(gs) >= MIN_DECK_TIMELINE_GAMES
    ][-MAX_DECK_TIMELINE:]


def trend(a: Analyzer, items: list[Game]) -> dict[str, Any]:
    """Is the player improving? Only a like-for-like comparison, on one deck, can say."""
    if len(items) < MIN_TREND_GAMES:
        return {"enough": False, "games": len(items)}
    half = len(items) // 2
    newer, older = items[:half], items[half:]
    same = _same_deck_games(older, newer)
    first, second = same if same else (older, newer)
    diff = _rate(second) - _rate(first)
    margin = 1.96 * (0.25 / len(first) + 0.25 / len(second)) ** 0.5
    if same is None:
        verdict = "changed_decks"
        on_this_deck = sum(g["deck_key"] == current_deck_key(items) for g in items)
        more_games = max(0, _games_to_settle(SWING_WORTH_SETTLING) - on_this_deck)
    elif abs(diff) > margin:
        verdict, more_games = ("improving" if diff > 0 else "slipping"), 0
    else:
        verdict = "steady"
        needed = _games_to_settle(abs(diff)) if abs(diff) >= SMALLEST_SWING else None
        more_games = max(0, needed - len(first) - len(second)) if needed else None
    days: defaultdict[str, list[Game]] = defaultdict(list)
    for g in first + second:
        days[g["battle_time"][:10]].append(g)

    def period(rows: list[Game]) -> dict[str, Any]:
        return _record(rows) | {"net_trophies": sum(g["trophy_change"] or 0 for g in rows)}

    return {
        "enough": True,
        "same_deck": same is not None,
        "older": period(first),
        "newer": period(second),
        "diff": round(diff, 3),
        "verdict": verdict,
        "more_games": more_games,
        "lobbies": _lobbies(older, newer),
        "decks": _decks_over_time(a, items),
        "current_streak": _streak(items),
        "days": [
            {"day": day, "games": len(gs), "wins": sum(g["result"] == "win" for g in gs)}
            for day, gs in sorted(days.items())
        ][-7:],
    }


def elixir_standing(a: Analyzer, items: list[Game]) -> dict[str, Any]:
    """How this deck's cost compares with every deck played, and how each cost range does."""
    key = current_deck_key(items)
    if key is None:
        return {"error": "This player has no recent games."}
    cost = {c: e for c, e in a._elixir().items() if e is not None}
    mine = sum(cost[c] for c in _ids(key) if c in cost) / 8

    def table() -> pl.DataFrame:
        each = pl.element().replace_strict(cost, default=None, return_dtype=pl.Float64)
        return (
            a.decks()
            .with_columns(avg=pl.col("cards").list.eval(each).list.mean())
            .group_by("avg")
            .agg(games=pl.len(), wins=pl.col("score").sum())
        )

    costs = a._cached("elixir_table", table)
    total = int(costs["games"].sum()) or 1
    bands = (
        costs.with_columns(start=(pl.col("avg") * 2).floor() / 2)
        .group_by("start")
        .agg(pl.col("games").sum(), pl.col("wins").sum())
        .filter(pl.col("games") >= 0.01 * total)
        .with_columns(win_rate=(pl.col("wins") / pl.col("games")).round(3))
        .sort("start")
        .to_dicts()
    )
    mine_start = int(mine * 2) / 2
    for b in bands:
        b["yours"] = b["start"] == mine_start
    ours = next((b for b in bands if b["yours"]), None)
    best = max((b["win_rate"] for b in bands), default=0.0)
    return {
        "avg_elixir": round(mine, 2),
        "average_deck": round(float((costs["avg"] * costs["games"]).sum()) / total, 2),
        "higher_than": round(int(costs.filter(pl.col("avg") < mine)["games"].sum()) / total, 3),
        "bands": bands,
        "costs_wins": ours is not None and best - ours["win_rate"] > MEANINGFUL_GAIN,
    }


def _trophy_range(items: list[Game]) -> tuple[str, int, int] | None:
    """The player's kind of battle (ladder or ranked) and the trophy range around them."""
    latest = next((g for g in items if g["starting_trophies"]), None)
    if latest is None:
        return None
    level = latest["starting_trophies"]
    band = max(200, level * NEAR_TROPHIES_SHARE)
    return latest["battle_type"], round(level - band), round(level + band)


def near_player(decks: pl.DataFrame, items: list[Game]) -> pl.DataFrame | None:
    """Decks from the player's kind of battle (ladder or ranked) at about their trophies."""
    found = _trophy_range(items)
    if found is None:
        return None
    kind, low, high = found
    return decks.filter(
        pl.col("battle_type") == kind, pl.col("starting_trophies").is_between(low, high)
    )


def near_filters(items: list[Game]) -> Filters | None:
    """Filters for other players' games in the player's trophy range, for the global questions."""
    found = _trophy_range(items)
    if found is None:
        return None
    kind, low, high = found
    return Filters(battle_types=(kind,), min_trophies=low, max_trophies=high)


def trophy_scope(items: list[Game]) -> str:
    """How to describe a near-trophy comparison to the player."""
    found = _trophy_range(items)
    if found is None:
        return "all players"
    kind, low, high = found
    where = "on ladder" if kind == "trail" else "in ranked"
    return f"players {where} at {low:,}-{high:,} trophies, like this player"


@dataclass(frozen=True)
class Family:
    """Other players' decks comparable to this one, and how strictly they were picked."""

    decks: pl.DataFrame
    near: bool  # limited to the player's trophy range
    min_shared: int  # every deck shares at least this many cards


def _family(a: Analyzer, tag: str, mine: list[int], items: list[Game]) -> Family:
    """Comparable decks: near the player's trophies and sharing 5+ cards, when there are enough."""

    def pick() -> Family:
        others = (
            a.decks()
            .filter(pl.col("player_tag") != tag)
            .with_columns(shared=pl.col("cards").list.set_intersection(mine).list.len())
            .filter(pl.col("shared").is_between(4, 7))
        )
        near = near_player(others, items)
        is_near = near is not None and near.height >= MIN_FAMILY_DECKS
        pool = near if is_near and near is not None else others
        closer = pool.filter(pl.col("shared") >= CLOSE_SHARED_CARDS)
        if closer.height >= MIN_CLOSE_FAMILY_DECKS:
            return Family(closer, is_near, CLOSE_SHARED_CARDS)
        return Family(pool, is_near, 4)

    latest = items[0]["battle_id"] if items else None
    return a._cached(("family", tag, tuple(sorted(mine)), latest), pick)


def similar_battles(a: Analyzer, tag: str, items: list[Game]) -> dict[str, Any]:
    """How much comparable evidence there is for this player's current deck."""
    family = _family(a, normalize_tag(tag), _ids(current_deck_key(items)), items)
    fam = family.decks
    return {
        "battles": fam.height,
        "players": fam["player_tag"].n_unique() if fam.height else 0,
        "days": DEFAULT_SINCE_DAYS,
        "enough": fam.height >= MIN_FAMILY_DECKS,
        "near": family.near,
    }


def replace_card(a: Analyzer, tag: str, items: list[Game]) -> dict[str, Any]:
    """Compare each card, with vs without, across other players' similar decks (last 7 days)."""
    tag = normalize_tag(tag)
    key = current_deck_key(items)
    if key is None:
        return {"enough": False, "family_decks": 0, "no_games": True}
    mine = _ids(key)
    family = _family(a, tag, mine, items)
    fam = family.decks
    n, wins = fam.height, float(fam["score"].sum() or 0)
    if n < MIN_FAMILY_DECKS:
        return {"enough": False, "family_decks": n}
    cards = _cards(a)
    per = (
        fam.explode("cards")
        .group_by(pl.col("cards").alias("card_id"))
        .agg(
            games=pl.len(),
            players=pl.col("player_tag").n_unique(),
            wins=pl.col("score").sum(),
        )
        .filter(pl.col("games") < n)
        .with_columns(
            gain=pl.col("wins") / pl.col("games") - (wins - pl.col("wins")) / (n - pl.col("games")),
            margin=1.96 * (0.25 / pl.col("games") + 0.25 / (n - pl.col("games"))).sqrt(),
        )
        .to_dicts()
    )

    def row(r: dict[str, Any]) -> dict[str, Any]:
        return cards[r["card_id"]] | {
            "games": r["games"],
            "players": r["players"],
            "gain": round(r["gain"], 3),
            "margin": round(r["margin"], 3),
            "real": abs(r["gain"]) > max(r["margin"], MEANINGFUL_GAIN),
        }

    yours = sorted(
        (row(r) for r in per if r["card_id"] in mine and r["card_id"] in cards),
        key=lambda r: r["gain"],
    )
    candidates = sorted(
        (
            row(r)
            for r in per
            if r["card_id"] not in mine
            and r["card_id"] in cards
            and r["games"] >= 100
            and r["players"] >= MIN_CANDIDATE_PLAYERS
            and r["gain"] > max(r["margin"], MEANINGFUL_GAIN)
        ),
        key=lambda r: r["gain"] - r["margin"],  # rank by what we can be confident of
        reverse=True,
    )
    return {
        "enough": True,
        "family_decks": n,
        "near": family.near,
        "min_shared_cards": family.min_shared,
        "family_players": fam["player_tag"].n_unique(),
        "yours": yours,
        "candidates": candidates[:4],
        "tested": sum(1 for r in per if r["card_id"] not in mine),
        "weakest": yours[0] if yours else None,
    }


def best_cards(a: Analyzer, items: list[Game]) -> dict[str, Any]:
    """Your win rate with each card vs every player's win rate with it (last 7 days)."""
    key = current_deck_key(items)
    cards = _cards(a)
    field = {r["card_id"]: r for r in a.card_stats(min_games=1).to_dicts()}
    rows = []
    for cid in _ids(key):
        with_card = [g for g in items if cid in _ids(g["deck_key"])]
        if len(with_card) < 5 or cid not in field or cid not in cards:
            continue
        mine = _rate(with_card)
        margin = _margin(len(with_card))
        diff = mine - field[cid]["win_rate"]
        rows.append(
            cards[cid]
            | {
                "games": len(with_card),
                "your_win_rate": round(mine, 3),
                "field_win_rate": round(field[cid]["win_rate"], 3),
                "diff": round(diff, 3),
                "margin": margin,
                "real": abs(diff) > margin,
            }
        )
    rows.sort(key=lambda r: r["diff"], reverse=True)
    return {"cards": rows, "best": rows[0] if rows else None, "worst": rows[-1] if rows else None}


def nemesis(a: Analyzer, items: list[Game], min_games: int = 3) -> dict[str, Any]:
    """Opponent cards you do best and worst against."""
    cards = _cards(a)
    seen: defaultdict[int, list[Game]] = defaultdict(list)
    for g in items:
        for cid in _ids(g["opponent_deck"]):
            seen[cid].append(g)
    overall = _rate(items)
    rows = [
        cards[cid]
        | {
            "games": len(gs),
            "wins": sum(g["result"] == "win" for g in gs),
            "losses": sum(g["result"] == "loss" for g in gs),
            "win_rate": round(_rate(gs), 3),
            "diff": round(_rate(gs) - overall, 3),
            "margin": _margin(len(gs)),
            "real": len(gs) >= MIN_MATCHUP_GAMES and abs(_rate(gs) - overall) > _margin(len(gs)),
        }
        for cid, gs in seen.items()
        if len(gs) >= min_games and cid in cards
    ]
    rows.sort(key=lambda r: (r["win_rate"], -r["games"]))
    return {
        "scope": AGAINST_SCOPE,
        "overall_win_rate": round(overall, 3),
        "worst": rows[:5],
        "best": rows[::-1][:3],
    }


def record_against(a: Analyzer, items: list[Game], card: str | int) -> dict[str, Any]:
    """The player's record in games where the opponent's deck contained `card`."""
    cid = a.resolve_card(card)
    info = _cards(a)[cid]
    faced = [g for g in items if cid in _ids(g["opponent_deck"])]
    if not faced:
        return {
            "card": info["name"],
            "scope": AGAINST_SCOPE,
            "games": 0,
            "total_games_checked": len(items),
        }
    overall, rate = _rate(items), _rate(faced)
    return _record(faced) | {
        "card": info["name"],
        "scope": AGAINST_SCOPE,
        "overall_win_rate": round(overall, 3),
        "diff": round(rate - overall, 3),
        "real": len(faced) >= MIN_MATCHUP_GAMES and abs(rate - overall) > _margin(len(faced)),
    }


def record_with(a: Analyzer, items: list[Game], card: str | int) -> dict[str, Any]:
    """The player's record in games where they were running `card` themselves."""
    cid = a.resolve_card(card)
    name = _cards(a)[cid]["name"]
    played = [g for g in items if cid in _ids(g["deck_key"])]
    if not played:
        return {"card": name, "scope": WITH_SCOPE, "games": 0, "total_games_checked": len(items)}
    return _record(played) | {
        "card": name,
        "scope": WITH_SCOPE,
        "overall_win_rate": round(_rate(items), 3),
    }


def best_deck(a: Analyzer, items: list[Game], min_games: int = 3) -> dict[str, Any]:
    cards = _cards(a)
    grouped: defaultdict[str, list[Game]] = defaultdict(list)
    for g in items:
        grouped[g["deck_key"]].append(g)
    current = current_deck_key(items)
    rows = [
        _record(gs)
        | {
            "cards": [cards[c] for c in _ids(key) if c in cards],
            "net_trophies": sum(g["trophy_change"] or 0 for g in gs),
            "current": key == current,
        }
        for key, gs in grouped.items()
        if len(gs) >= min_games
    ]
    rows.sort(key=lambda r: (r["win_rate"], r["games"]), reverse=True)
    return {"decks": rows[:5]}


# ---------------------------------------------------------------- how to win more

UNDERLEVELED = 0.5  # opponent's deck averages at least half a level above the player's
TILT_STREAK = 2  # losses in a row before we check how the next game goes
RECENT_FOR_SWITCHING = 30
MIN_PLAN_GAMES = 10  # per side of a comparison, before it can count as a finding
MIN_GAMES_TO_JUDGE = 15
PLAN_ORDER = (
    "worst_matchups",
    "card_levels",
    "after_two_losses",
    "close_games",
    "three_crowned",
    "deck_switching",
)


def _split(items: list[Game], test: list[bool]) -> dict[str, Any]:
    """Records for the games that pass a test and the ones that don't, and whether they differ."""
    yes = [g for g, t in zip(items, test, strict=True) if t]
    no = [g for g, t in zip(items, test, strict=True) if not t]
    diff = _rate(yes) - _rate(no) if yes and no else 0.0
    margin = 1.96 * (0.25 / len(yes) + 0.25 / len(no)) ** 0.5 if yes and no else 1.0
    enough = min(len(yes), len(no)) >= MIN_PLAN_GAMES
    return {
        "in_those_games": _record(yes),
        "other_games": _record(no),
        "diff": round(diff, 3),
        "real": enough and abs(diff) > max(margin, MEANINGFUL_GAIN),
    }


def _deck_levels(a: Analyzer, tag: str, items: list[Game]) -> dict[int, tuple[float, float]]:
    """battle_id -> (player's average card level, opponent's), on the in-game scale."""
    ids = [g["battle_id"] for g in items]
    if not ids:
        return {}
    rows = a.query(
        f"""SELECT battle_id, player_tag = ? AS me, avg(level + (? - max_level)) AS lvl
            FROM participant_cards
            WHERE kind = 'deck' AND battle_id IN ({",".join("?" * len(ids))})
            GROUP BY 1, 2""",
        (tag, a._max_common_level, *ids),
    ).to_dicts()
    pairs: defaultdict[int, dict[int, float]] = defaultdict(dict)
    for r in rows:
        pairs[r["battle_id"]][r["me"]] = r["lvl"]
    return {b: (p[1], p[0]) for b, p in pairs.items() if 0 in p and 1 in p}


def levels(a: Analyzer, tag: str, items: list[Game]) -> dict[str, Any]:
    """Are they losing to card levels? Games against higher-level decks vs the rest."""
    tag = normalize_tag(tag)
    known = _deck_levels(a, tag, items)
    games_known = [g for g in items if g["battle_id"] in known]
    if not games_known:
        return {"games": 0}
    gaps = [known[g["battle_id"]][1] - known[g["battle_id"]][0] for g in games_known]
    o = overview(a, tag, items)
    typical_opponent = median(known[g["battle_id"]][1] for g in games_known)
    behind = [
        {"name": c["name"], "level": c["level"]}
        for c in o["deck"]
        if c["level"] is not None and c["level"] < round(typical_opponent)
    ]
    return _split(games_known, [gap >= UNDERLEVELED for gap in gaps]) | {
        "what": "Games where the opponent's deck was at least half a level higher than theirs",
        "your_deck_level": round(median(known[g["battle_id"]][0] for g in games_known), 2),
        "typical_opponent_level": round(typical_opponent, 2),
        "share_underleveled": round(sum(gap >= UNDERLEVELED for gap in gaps) / len(gaps), 2),
        "cards_below_typical_opponent": behind,
    }


def tilt(items: list[Game]) -> dict[str, Any]:
    """How the next game goes after losing two in a row (items are newest first)."""
    after = [
        all(items[j]["result"] == "loss" for j in range(i + 1, i + 1 + TILT_STREAK))
        for i in range(len(items) - TILT_STREAK)
    ]
    return _split(items[: len(after)], after) | {
        "what": f"Games played right after losing {TILT_STREAK} in a row",
    }


def deck_switching(items: list[Game]) -> dict[str, Any]:
    recent = items[:RECENT_FOR_SWITCHING]
    current = current_deck_key(items)
    run = next((i for i, g in enumerate(items) if g["deck_key"] != current), len(items))
    return {
        "recent_games": len(recent),
        "decks_used": len({g["deck_key"] for g in recent}),
        "games_in_a_row_on_current_deck": run,
    }


def game_plan(a: Analyzer, tag: str, items: list[Game]) -> dict[str, Any]:
    """Every common reason players lose, checked against this player's own games.

    Each finding says whether the games show it for real. The model leads with the real ones and
    turns each into one thing to do next.
    """
    if len(items) < MIN_TREND_GAMES:
        return {"enough": False, "games": len(items)}
    w = wins_and_losses(items)
    worst = [r for r in nemesis(a, items)["worst"] if r["real"]]
    close = w["close_games"]
    losses = max(w["losses"], 1)
    switching = deck_switching(items)
    findings = {
        "card_levels": levels(a, tag, items),
        "after_two_losses": tilt(items),
        "deck_switching": switching
        | {"real": switching["decks_used"] >= 3 and switching["recent_games"] >= 20},
        "worst_matchups": {
            "cards": worst[:3],
            "your_usual_win_rate": round(_rate(items), 3),
            "real": bool(worst),
        },
        "close_games": close
        | {"real": close["games"] >= MIN_GAMES_TO_JUDGE and close["win_rate"] <= 0.45},
        "three_crowned": {
            "losses": w["losses"],
            "three_crowned_losses": w["three_crowned_losses"],
            "share_of_losses": round(w["three_crowned_losses"] / losses, 2),
            "real": w["losses"] >= MIN_PLAN_GAMES and w["three_crowned_losses"] / losses >= 0.4,
        },
    }
    deck = [c["name"] for c in overview(a, tag, items)["deck"]]
    return {
        "enough": True,
        "games": len(items),
        "record": _record(items),
        "current_deck": deck,
        "games_on_current_deck": sum(g["deck_key"] == current_deck_key(items) for g in items),
        # Most specific and fixable first: a card that beats them, then levels, then habits.
        "real_problems": [k for k in PLAN_ORDER if findings[k].get("real")],
        "findings": findings,
        "scope": "This player's own games. Each finding's `real` says if the games prove it.",
    }


def field_size(a: Analyzer, items: list[Game]) -> dict[str, Any]:
    """How many games Rook can learn from, overall and in this player's trophy range."""
    every = a.decks()
    out: dict[str, Any] = {
        "days": DEFAULT_SINCE_DAYS,
        "battles_all_players": every["battle_id"].n_unique(),
        "players_all": every["player_tag"].n_unique(),
        "your_games": len(items),
    }
    near = near_player(every, items)
    if near is not None:
        out |= {
            "battles_near_your_trophies": near["battle_id"].n_unique(),
            "players_near_your_trophies": near["player_tag"].n_unique(),
            "near_means": trophy_scope(items),
        }
    return out
