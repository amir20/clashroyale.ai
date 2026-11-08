"""Player-facing insights: what the web app shows for one player's tag.

Everything here is computed from data, with a margin of error on every comparison, so free
answers need no AI call and can't be hallucinated. `coach.py` turns these results into words.
"""

from collections import Counter, defaultdict
from typing import Any

import polars as pl

from .analytics import DEFAULT_SINCE_DAYS, EXCLUDED_TYPES, Analyzer, _margin
from .api import normalize_tag

type Game = dict[str, Any]

MIN_FAMILY_DECKS = 200
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
                   o.name AS opponent, o.deck_key AS opponent_deck
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

    close = [g for g in items if abs(g["crowns"] - g["opponent_crowns"]) == 1]
    return {
        "wins": len(wins),
        "losses": len(losses),
        "close_games": _record(close),
        "three_crown_wins": sum(g["crowns"] == 3 for g in wins),
        "three_crowned_losses": sum(g["opponent_crowns"] == 3 for g in losses),
        "crowns_for": avg(items, "crowns"),
        "crowns_against": avg(items, "opponent_crowns"),
        "elixir_leaked_wins": avg(wins, "elixir_leaked"),
        "elixir_leaked_losses": avg(losses, "elixir_leaked"),
    }


def _family(a: Analyzer, tag: str, mine: list[int]) -> pl.DataFrame:
    """Other players' recent decks sharing 4-7 cards with this deck."""
    return (
        a.decks()
        .filter(pl.col("player_tag") != tag)
        .with_columns(shared=pl.col("cards").list.set_intersection(mine).list.len())
        .filter(pl.col("shared").is_between(4, 7))
    )


def similar_battles(a: Analyzer, tag: str, items: list[Game]) -> dict[str, Any]:
    """How much comparable evidence there is for this player's current deck."""
    fam = _family(a, normalize_tag(tag), _ids(current_deck_key(items)))
    return {
        "battles": fam.height,
        "players": fam["player_tag"].n_unique() if fam.height else 0,
        "days": DEFAULT_SINCE_DAYS,
        "enough": fam.height >= MIN_FAMILY_DECKS,
    }


def replace_card(a: Analyzer, tag: str, items: list[Game]) -> dict[str, Any]:
    """Compare each card, with vs without, across other players' similar decks (last 7 days)."""
    tag = normalize_tag(tag)
    key = current_deck_key(items)
    if key is None:
        return {"enough": False, "family_decks": 0}
    mine = _ids(key)
    fam = _family(a, tag, mine)
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
            "win_rate_with": round(r["wins"] / r["games"], 3),
            "win_rate_without": round((wins - r["wins"]) / (n - r["games"]), 3),
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
        "family_players": fam["player_tag"].n_unique(),
        "family_win_rate": round(wins / n, 3),
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
        return {"card": info["name"], "games": 0, "total_games_checked": len(items)}
    overall, rate = _rate(items), _rate(faced)
    return _record(faced) | {
        "card": info["name"],
        "overall_win_rate": round(overall, 3),
        "diff": round(rate - overall, 3),
        "real": len(faced) >= MIN_MATCHUP_GAMES and abs(rate - overall) > _margin(len(faced)),
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
