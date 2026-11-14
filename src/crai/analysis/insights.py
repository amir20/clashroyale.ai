"""Player-facing insights: what the web app shows for one player's tag.

Everything here is computed from data, with a margin of error on every comparison, so free
answers need no AI call and can't be hallucinated. `coach.py` turns these results into words.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Hashable, Iterator
from dataclasses import dataclass, field
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
    plays,
    shared_cards,
    special_count,
    specials,
)
from crai.collect.client import normalize_tag

type Game = dict[str, Any]

MIN_FAMILY_DECKS = 200
MIN_CLOSE_FAMILY_DECKS = 1000  # enough decks to afford requiring a closer match
CLOSE_SHARED_CARDS = 5
NEAR_TROPHIES_SHARE = 0.07  # "your trophy range" is within this share of the player's trophies
STICKY_TROPHIES = 100  # cached stats about a player's range survive this much trophy movement
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


def slots(a: Analyzer, tag: str, items: list[Game]) -> dict[int, int]:
    """How each card of their current deck was played in their newest game: card -> status
    (0 plain, 1 Evolution, 2 Hero, 3 both). A swap has to fit the slot the card leaves."""
    if not items:
        return {}
    rows = a.query(
        """SELECT card_id, coalesce(evolution_level, 0) AS status FROM participant_cards
           WHERE battle_id = ? AND player_tag = ? AND kind = 'deck'""",
        (items[0]["battle_id"], normalize_tag(tag)),
    )
    return {r["card_id"]: r["status"] for r in rows.to_dicts()}


def unlocked(a: Analyzer, tag: str) -> dict[int, int] | None:
    """The Evolution/Hero modes the player has unlocked per card (same bits as `slots`), or None
    when their collection hasn't been fetched."""
    rows = a.query(
        "SELECT card_id, coalesce(evolution_level, 0) AS status FROM player_cards WHERE tag = ?",
        (normalize_tag(tag),),
    )
    return {r["card_id"]: r["status"] for r in rows.to_dicts()} or None


def can_play(card: dict[str, Any], status: int) -> bool:
    """Whether the card has the Evolution/Hero form a slot needs."""
    return (not status & 1 or bool(card["evo_icon"])) and (
        not status & 2 or bool(card["hero_icon"])
    )


def _variants(decks: pl.DataFrame) -> pl.DataFrame:
    """One row per card per deck, with how it was played (`status`, 0 = plain)."""
    d = decks.select("player_tag", "score", "cards", sp=specials()).with_row_index("deck")
    special = (
        d.select("deck", v=pl.col("sp"))
        .explode("v")
        .drop_nulls("v")
        .select("deck", card_id=pl.col("v") // 10, status=pl.col("v") % 10)
    )
    return (
        d.drop("sp")
        .explode("cards")
        .rename({"cards": "card_id"})
        .join(special, on=["deck", "card_id"], how="left")
        .with_columns(pl.col("status").fill_null(0))
    )


def games(a: Analyzer, tag: str) -> list[Game]:
    """The player's real 1v1 games with normal decks, newest first."""
    excluded = ",".join("?" * len(EXCLUDED_TYPES))
    return a.query(
        f"""SELECT b.id AS battle_id, b.battle_time, b.battle_type, me.result, me.crowns,
                   me.opponent_crowns, me.trophy_change, me.starting_trophies,
                   me.elixir_leaked, me.deck_key,
                   o.name AS opponent, o.player_tag AS opponent_tag,
                   o.deck_key AS opponent_deck,
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


RECENT_BATTLES = 10


def recent_battles(a: Analyzer, items: list[Game], n: int = RECENT_BATTLES) -> list[dict[str, Any]]:
    """The last few games with who they were against and the deck the opponent brought."""
    latest = items[:n]
    if not latest:
        return []
    cards = _cards(a)
    marks = ",".join("?" * len(latest))
    rows = a.query(
        f"""SELECT battle_id, player_tag, card_id, evolution_level FROM participant_cards
            WHERE battle_id IN ({marks}) AND kind = 'deck' ORDER BY battle_id, slot""",
        tuple(g["battle_id"] for g in latest),
    ).to_dicts()
    decks: defaultdict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["card_id"] in cards:
            card = _played_as(cards[r["card_id"]], r["evolution_level"])
            decks[(r["battle_id"], r["player_tag"])].append(card)
    return [
        {
            "battle_id": g["battle_id"],
            "time": g["battle_time"],
            "type": g["battle_type"],
            "result": g["result"],
            "crowns": g["crowns"],
            "opponent_crowns": g["opponent_crowns"],
            "trophy_change": g["trophy_change"],
            "opponent": {
                "tag": g["opponent_tag"],
                "name": g["opponent"],
                "trophies": g["opponent_trophies"],
                "deck": decks.get((g["battle_id"], g["opponent_tag"]))
                or [cards[i] for i in _ids(g["opponent_deck"]) if i in cards],
            },
        }
        for g in latest
    ]


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
            "cards": compared(cards, _ids(key), now),
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


def elixir_table(a: Analyzer) -> pl.DataFrame:
    """Games and wins at each average deck cost, over everyone's battles."""
    cost = {c: e for c, e in a._elixir().items() if e is not None}

    def table() -> pl.DataFrame:
        each = pl.element().replace_strict(cost, default=None, return_dtype=pl.Float64)
        return (
            # Each distinct deck once: about half the work of pricing every row.
            a.decks()
            .group_by("deck_key")
            .agg(cards=pl.col("cards").first(), games=pl.len(), wins=pl.col("score").sum())
            .with_columns(avg=pl.col("cards").list.eval(each).list.mean())
            .group_by("avg")
            .agg(pl.col("games").sum(), pl.col("wins").sum())
        )

    return a._field_cached("elixir_table", table)


def elixir_standing(a: Analyzer, items: list[Game]) -> dict[str, Any]:
    """How this deck's cost compares with every deck played, and how each cost range does."""
    key = current_deck_key(items)
    if key is None:
        return {"error": "This player has no recent games."}
    cost = {c: e for c, e in a._elixir().items() if e is not None}
    mine = sum(cost[c] for c in _ids(key) if c in cost) / 8

    costs = elixir_table(a)
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


def _near_stat[T](a: Analyzer, key: Hashable, items: list[Game], fn: Callable[[], T]) -> T:
    """Cache a stat about players near this one, keyed by the player (not their exact trophies).

    It's reused until their trophies move more than STICKY_TROPHIES from when it was computed:
    every game shifts the range a little, and recomputing takes seconds on a big table, while
    a ~2,000-trophy-wide range barely changes who's in it.
    """
    scope = _trophy_range(items)

    def fits(cached: tuple[tuple[str, int, int] | None, T]) -> bool:
        was = cached[0]
        if was is None or scope is None:
            return was == scope
        moved = abs((was[1] + was[2]) - (scope[1] + scope[2])) / 2
        return was[0] == scope[0] and moved <= STICKY_TROPHIES

    return a._field_cached(key, lambda: (scope, fn()), fits)[1]


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


def _family(a: Analyzer, tag: str, mine: list[int], items: list[Game], n_special: int) -> Family:
    """Comparable decks: near the player's trophies and sharing 5+ cards, when there are enough.

    Every deck fills as many Evolution/Hero slots as theirs: an extra Evolution wins more games
    on its own, and would credit whichever cards happen to come with it.
    """

    def alike(decks: pl.DataFrame) -> pl.DataFrame:
        return (
            decks.filter(pl.col("player_tag") != tag)
            .with_columns(shared=shared_cards(mine))
            .filter(pl.col("shared").is_between(4, 7))
            .filter(special_count() == n_special)
        )

    def pick() -> Family:
        # Players at their trophies first: a much smaller table to compare decks in.
        field = near_player(a.decks(), items)
        near = alike(field) if field is not None else None
        is_near = near is not None and near.height >= MIN_FAMILY_DECKS
        pool = near if is_near and near is not None else alike(a.decks())
        closer = pool.filter(pl.col("shared") >= CLOSE_SHARED_CARDS)
        if closer.height >= MIN_CLOSE_FAMILY_DECKS:
            return Family(closer, is_near, CLOSE_SHARED_CARDS)
        return Family(pool, is_near, 4)

    return _near_stat(a, ("family", tag, tuple(sorted(mine)), n_special), items, pick)


MIN_SWAP_GAMES = 100  # decks running each card, before a head-to-head says anything
SHORTLIST = 8  # candidates checked head-to-head
MIN_COMPANION_SHARE = 0.25  # "most of these decks also changed X" needs this share of them
MIN_WEAK_SPOT_GAMES = 30  # games against an opponent card, per side, to call out a weak spot


@dataclass(frozen=True)
class HeadToHead:
    """Decks sharing the player's other cards, running one of two cards in the same slot."""

    out_games: int
    in_games: int
    out_players: int
    in_players: int
    out_rate: float | None
    in_rate: float | None
    gap: float | None  # in_rate - out_rate, once both sides have enough games
    margin: float | None
    verdict: str  # better / worse / even / unknown
    decks: int
    near: bool
    needed: int  # of the player's other cards, every deck has at least this many
    exact: bool  # every deck is the player's deck with only this card changed
    # When decks changed more than this card: the most common other change (ids), and its share.
    also: tuple[tuple[int, ...], tuple[int, ...], float] | None
    sides: tuple[pl.DataFrame, pl.DataFrame] = field(repr=False, compare=False)


class SwapField:
    """Other players' decks a swap in the player's deck is judged on, built once per deck so
    several candidates can be compared cheaply."""

    def __init__(
        self, a: Analyzer, tag: str, items: list[Game], others: list[int], n_special: int
    ) -> None:
        self._a, self._tag, self._items = a, normalize_tag(tag), items
        self._others, self._n_special = others, n_special
        self._near: pl.DataFrame | None = None
        self._all: pl.DataFrame | None = None

    def _alike(self, d: pl.DataFrame) -> pl.DataFrame:
        return (
            d.filter(pl.col("player_tag") != self._tag)
            .with_columns(shared=shared_cards(self._others))
            .filter(pl.col("shared") >= min(len(self._others), CLOSE_SHARED_CARDS))
            .filter(special_count() == self._n_special)  # after: parsing specials is slower
        )

    def _pool(self, near: bool) -> pl.DataFrame:
        """Small once filtered, but finding them scans every deck: kept like the family is."""
        key = ("swap_pool", self._tag, tuple(sorted(self._others)), self._n_special, near)

        def build() -> pl.DataFrame:
            if not near:
                return self._alike(self._a.decks())
            field = near_player(self._a.decks(), self._items)
            return self._alike(field) if field is not None else pl.DataFrame()

        return _near_stat(self._a, key, self._items, build)

    def pools(self) -> Iterator[tuple[pl.DataFrame, bool]]:
        """Near the player's trophies first, then at every trophy level (built only if needed)."""
        if self._near is None:
            self._near = self._pool(near=True)
        if self._near.height:
            yield self._near, True
        if self._all is None:
            self._all = self._pool(near=False)
        yield self._all, False

    def compare(self, out_id: int, in_id: int, slot: int) -> HeadToHead:
        """Both cards judged in the slot the leaving card fills.

        Decks as close to theirs as the games allow: their exact deck with only this card
        changed, then one more card changed, and so on. A deck that drops a card usually has to
        cover for it (losing Musketeer means losing air defense), so the closer the decks, the
        more the gap is about this swap alone.
        """
        has_out, has_in = (pl.col("cards").list.contains(c) for c in (out_id, in_id))
        top = len(self._others)
        floor = min(top, CLOSE_SHARED_CARDS)  # fewer shared cards is another deck, not a swap
        chosen = None
        for pool, near in self.pools():  # near first: far smaller, and the full one is costly
            one = pool.filter(has_out != has_in, pl.col("shared") >= floor)
            one = one.with_columns(is_out=plays(out_id, slot), is_in=plays(in_id, slot))
            for needed in range(top, floor - 1, -1):
                p = one.filter(pl.col("shared") >= needed)
                sides = (p.filter("is_out"), p.filter("is_in"))
                fewest = min(s.height for s in sides)
                enough = fewest >= MIN_SWAP_GAMES
                # Without enough anywhere, show the closest it got (the most games a side).
                if chosen is None or enough or fewest > min(s.height for s in chosen[1]):
                    chosen = (p, sides, near, needed)
                if enough:
                    break
            else:
                continue
            break
        assert chosen is not None
        p, (o, i), near, needed = chosen
        out_rate = o["score"].mean() if o.height else None
        in_rate = i["score"].mean() if i.height else None
        verdict, gap, margin = "unknown", None, None
        if min(o.height, i.height) >= MIN_SWAP_GAMES:
            diff = float(in_rate) - float(out_rate)  # ty: ignore[invalid-argument-type]
            gap = round(diff, 3)
            margin = round(1.96 * (0.25 / o.height + 0.25 / i.height) ** 0.5, 3)
            verdict = "even"
            if abs(diff) > max(margin, MEANINGFUL_GAIN):
                verdict = "better" if diff > 0 else "worse"
        return HeadToHead(
            out_games=o.height,
            in_games=i.height,
            out_players=o["player_tag"].n_unique() if o.height else 0,
            in_players=i["player_tag"].n_unique() if i.height else 0,
            out_rate=out_rate,  # ty: ignore[invalid-argument-type]
            in_rate=in_rate,  # ty: ignore[invalid-argument-type]
            gap=gap,
            margin=margin,
            verdict=verdict,
            decks=p.height,
            near=near,
            needed=needed,
            exact=needed == top,
            also=self._also(i, in_id) if needed < top else None,
            sides=(o, i),
        )

    def _also(
        self, decks: pl.DataFrame, in_id: int
    ) -> tuple[tuple[int, ...], tuple[int, ...], float] | None:
        """What else the decks with the new card changed, if most of them changed the same."""
        if not decks.height:
            return None
        others = self._others
        changes = (
            decks.select(
                dropped=pl.lit(others).list.set_difference(pl.col("cards")).list.sort(),
                added=pl.col("cards").list.set_difference(pl.lit([*others, in_id])).list.sort(),
            )
            .group_by("dropped", "added")
            .len()
            .sort("len", descending=True)
        )
        top = changes.row(0, named=True)
        share = top["len"] / decks.height
        if not top["dropped"] or share < MIN_COMPANION_SHARE:
            return None
        return (tuple(top["dropped"]), tuple(top["added"]), round(share, 2))


def weak_spots(a: Analyzer, h2h: HeadToHead, limit: int = 3) -> list[dict[str, Any]]:
    """Opponent cards the swap does clearly worse against: what the leaving card was covering
    (Musketeer out, and air decks start winning)."""
    o, i = h2h.sides
    if min(o.height, i.height) < MIN_SWAP_GAMES:
        return []
    ids = pl.concat([o["battle_id"], i["battle_id"]]).unique()
    opp = (
        a.decks()
        .filter(pl.col("battle_id").is_in(ids.implode()))
        .select("battle_id", pl.col("side").alias("opp_side"), pl.col("cards").alias("opp"))
    )

    def against(d: pl.DataFrame) -> pl.DataFrame:
        return (
            d.select("battle_id", "side", "score")
            .join(opp, on="battle_id")
            .filter(pl.col("side") != pl.col("opp_side"))
            .explode("opp")
            .group_by("opp")
            .agg(n=pl.len(), rate=pl.col("score").mean())
        )

    rows = (
        against(o)
        .join(against(i), on="opp", suffix="_in")
        .filter(pl.col("n") >= MIN_WEAK_SPOT_GAMES, pl.col("n_in") >= MIN_WEAK_SPOT_GAMES)
        .with_columns(
            gap=pl.col("rate_in") - pl.col("rate"),
            margin=1.96 * (0.25 / pl.col("n") + 0.25 / pl.col("n_in")).sqrt(),
        )
        .filter(pl.col("gap") < -pl.max_horizontal(pl.col("margin"), pl.lit(MEANINGFUL_GAIN)))
        .sort("gap")
        .head(limit)
        .to_dicts()
    )
    cards = _cards(a)
    return [
        cards[r["opp"]]
        | {
            "gap": round(r["gap"], 3),
            "margin": round(r["margin"], 3),
            "games": r["n_in"],
            "out_games": r["n"],
        }
        for r in rows
        if r["opp"] in cards
    ]


def match(h2h: HeadToHead, cards: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """How closely the compared decks match the player's, for the UI and Rook's wording."""
    also = None
    if h2h.also:
        dropped, added, share = h2h.also
        also = {
            "dropped": [cards[c] for c in dropped if c in cards],
            "added": [cards[c] for c in added if c in cards],
            "share": share,
        }
    return {"exact": h2h.exact, "shared": h2h.needed, "near": h2h.near, "also": also}


def similar_battles(a: Analyzer, tag: str, items: list[Game]) -> dict[str, Any]:
    """How much comparable evidence there is for this player's current deck."""
    tag = normalize_tag(tag)
    played = slots(a, tag, items)
    n_special = sum(1 for s in played.values() if s)
    family = _family(a, tag, _ids(current_deck_key(items)), items, n_special)
    fam = family.decks
    return {
        "battles": fam.height,
        "players": fam["player_tag"].n_unique() if fam.height else 0,
        "days": DEFAULT_SINCE_DAYS,
        "enough": fam.height >= MIN_FAMILY_DECKS,
        "near": family.near,
    }


def replace_card(a: Analyzer, tag: str, items: list[Game]) -> dict[str, Any]:
    """Compare each card, with vs without, across other players' similar decks (last 7 days).

    Cards are judged as they're played: a Hero Giant and a plain Giant are different cards. A
    replacement has to fit the slot it takes: plain for a plain card, and for an Evolution or Hero
    slot, a card with that form the player has unlocked.
    """
    tag = normalize_tag(tag)
    key = current_deck_key(items)
    if key is None:
        return {"enough": False, "family_decks": 0, "no_games": True}
    mine = _ids(key)
    played = slots(a, tag, items)
    family = _family(a, tag, mine, items, sum(1 for s in played.values() if s))
    fam = family.decks
    n, wins = fam.height, float(fam["score"].sum() or 0)
    if n < MIN_FAMILY_DECKS:
        return {"enough": False, "family_decks": n}
    cards = _cards(a)
    v = _variants(fam)
    per_card = v.group_by("card_id").agg(card_games=pl.len(), card_wins=pl.col("score").sum())
    # A variant against decks without the card at all, so another form of it doesn't muddy both.
    per = (
        v.group_by("card_id", "status")
        .agg(
            games=pl.len(),
            players=pl.col("player_tag").n_unique(),
            wins=pl.col("score").sum(),
        )
        .join(per_card, on="card_id")
        .filter(pl.col("card_games") < n)
        .with_columns(
            gain=pl.col("wins") / pl.col("games")
            - (wins - pl.col("card_wins")) / (n - pl.col("card_games")),
            margin=1.96 * (0.25 / pl.col("games") + 0.25 / (n - pl.col("card_games"))).sqrt(),
        )
        .to_dicts()
    )

    def row(r: dict[str, Any]) -> dict[str, Any]:
        return _played_as(cards[r["card_id"]], r["status"]) | {
            "games": r["games"],
            "players": r["players"],
            "gain": round(r["gain"], 3),
            "margin": round(r["margin"], 3),
            "real": abs(r["gain"]) > max(r["margin"], MEANINGFUL_GAIN),
        }

    yours = sorted(
        (
            row(r)
            for r in per
            if r["card_id"] in cards
            and r["card_id"] in mine
            and r["status"] == played.get(r["card_id"], 0)
        ),
        key=lambda r: r["gain"],
    )
    weakest = yours[0] if yours else None
    slot = played.get(weakest["id"], 0) if weakest else 0
    owned = unlocked(a, tag) if slot else None

    def fits(cid: int) -> bool:
        return can_play(cards[cid], slot) and (owned is None or owned.get(cid, 0) & slot == slot)

    # Cards that do well in decks like theirs. "With vs without" only says a card's decks win;
    # whether it beats the card it would replace is the head-to-head below.
    shortlist = sorted(
        (
            row(r)
            for r in per
            if r["card_id"] not in mine
            and r["card_id"] in cards
            and r["status"] == slot
            and fits(r["card_id"])
            and r["games"] >= 100
            and r["players"] >= MIN_CANDIDATE_PLAYERS
            and r["gain"] > max(r["margin"], MEANINGFUL_GAIN)
        ),
        key=lambda r: r["gain"] - r["margin"],
        reverse=True,
    )[:SHORTLIST]
    checked: list[tuple[dict[str, Any], HeadToHead]] = []
    if weakest and shortlist:
        others = [c for c in mine if c != weakest["id"]]
        field = SwapField(a, tag, items, others, sum(1 for s in played.values() if s))
        for c in shortlist:
            h2h = field.compare(weakest["id"], c["id"], slot)
            if h2h.verdict != "better" or h2h.gap is None or h2h.margin is None:
                continue
            row_ = c | {
                "gain": h2h.gap,  # the same number the swap chart shows
                "margin": h2h.margin,
                "games": h2h.in_games,
                "players": h2h.in_players,
                "real": True,
                "match": match(h2h, cards),
            }
            checked.append((row_, h2h))
        checked.sort(key=lambda x: x[0]["gain"] - x[0]["margin"], reverse=True)
    candidates = [r for r, _ in checked]
    if checked:  # where the best swap loses, for the one Rook suggests
        candidates[0]["watch_out"] = weak_spots(a, checked[0][1])
    return {
        "enough": True,
        "family_decks": n,
        "near": family.near,
        "min_shared_cards": family.min_shared,
        "family_players": fam["player_tag"].n_unique(),
        "yours": yours,
        "candidates": candidates[:4],  # gain = head-to-head against `weakest`
        "tested": len({r["card_id"] for r in per if r["card_id"] not in mine}),
        "weakest": weakest,
        "slot": STATUS.get(slot),  # the weakest card's slot, which every candidate fits
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


def compared(
    cards: dict[int, dict[str, Any]], ids: list[int], now: set[int]
) -> list[dict[str, Any]]:
    """All 8 cards of a deck, each marked with whether the player's current deck has it.

    Cards they already run come first, so the different ones line up at the end.
    """
    shown = [cards[c] | {"yours": c in now} for c in ids if c in cards]
    return sorted(shown, key=lambda c: not c["yours"])


def best_deck(a: Analyzer, items: list[Game], min_games: int = 3) -> dict[str, Any]:
    cards = _cards(a)
    now = set(_ids(current_deck_key(items)))
    grouped: defaultdict[str, list[Game]] = defaultdict(list)
    for g in items:
        grouped[g["deck_key"]].append(g)
    current = current_deck_key(items)
    rows = [
        _record(gs)
        | {
            "cards": compared(cards, _ids(key), now),
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


# ---------------------------------------------------------------- things they didn't know

MIN_SIGNATURE_GAMES = 20
MIN_SIGNATURE_GAP = 0.08  # beat players at their trophies by at least this much with a card
MIN_FREE_WIN_GAMES = 8
FREE_WIN_RATE = 0.8
UNIQUE_DECK_SHARED = 6
MAX_LOOKALIKES = 10  # at most this many players at their trophies run a near-copy
MIN_UNIQUE_FIELD = 10_000
MIN_EVO_GAMES = 15
MIN_EVO_GAP = 0.1
MAX_SURPRISES = 4


def _z(diff: float, n1: int, n2: int | None = None) -> float:
    """How many standard errors a gap is: the ranking for 'most surprising'."""
    se = (0.25 / n1 + (0.25 / n2 if n2 else 0.0)) ** 0.5
    return abs(diff) / se if se else 0.0


def signature_card(a: Analyzer, tag: str, items: list[Game]) -> dict[str, Any] | None:
    """The card they win with far more often than players at their trophies do."""
    cards = _cards(a)
    played: defaultdict[int, list[Game]] = defaultdict(list)
    for g in items:
        for cid in _ids(g["deck_key"]):
            played[cid].append(g)
    used = [c for c, gs in played.items() if len(gs) >= MIN_SIGNATURE_GAMES and c in cards]
    if not used:
        return None

    def card_rates() -> pl.DataFrame:
        field = near_player(a.decks(), items)
        if field is None or field.height < MIN_FAMILY_DECKS:
            field = a.decks()
        return (
            field.explode("cards")
            .filter(pl.col("cards").is_in(used))
            .group_by("cards")
            .agg(games=pl.len(), rate=pl.col("score").mean())
        )

    rates = _near_stat(a, ("card_rates", tag, tuple(sorted(used))), items, card_rates)
    best: dict[str, Any] | None = None
    best_z = 0.0
    for r in rates.iter_rows(named=True):
        gs = played[r["cards"]]
        diff = _rate(gs) - r["rate"]
        if diff >= MIN_SIGNATURE_GAP and diff > _margin(len(gs)):
            z = _z(diff, len(gs))
            if z > best_z:
                best_z = z
                best = {
                    "kind": "signature_card",
                    "score": round(z, 2),
                    "card": cards[r["cards"]],
                    "record": _record(gs),
                    "field_win_rate": round(r["rate"], 3),
                    "field_games": r["games"],
                }
    return best


def unique_deck(a: Analyzer, tag: str, items: list[Game]) -> dict[str, Any] | None:
    """Almost nobody at their trophies plays anything like their deck."""
    key, scope = current_deck_key(items), _trophy_range(items)
    if key is None or scope is None:
        return None
    mine = _ids(key)

    def count() -> tuple[int, int]:
        field = near_player(a.decks(), items)
        assert field is not None  # scope is known
        others = field.filter(pl.col("player_tag") != tag)
        shared = others.with_columns(s=shared_cards(mine))
        alike = shared.filter(pl.col("s") >= UNIQUE_DECK_SHARED)["player_tag"].n_unique()
        return alike, others["player_tag"].n_unique()

    alike, total = _near_stat(a, ("unique", tag, key), items, count)
    if total < MIN_UNIQUE_FIELD or alike > MAX_LOOKALIKES:
        return None
    return {
        "kind": "unique_deck",
        "score": 4.0,  # rare and fun, but it's a fact about the deck, not a result
        "players_alike": alike,
        "players_total": total,
        "shared_cards": UNIQUE_DECK_SHARED,
        "deck": overview(a, tag, items)["deck"][:4],
    }


def free_wins(a: Analyzer, items: list[Game]) -> dict[str, Any] | None:
    """Opponent cards they almost always beat."""
    rows = [
        r
        for r in nemesis(a, items, min_games=MIN_FREE_WIN_GAMES)["best"]
        if r["win_rate"] >= FREE_WIN_RATE and r["diff"] > r["margin"]
    ]
    if not rows:
        return None
    return {
        "kind": "free_wins",
        "score": round(max(_z(r["diff"], r["games"]) for r in rows), 2),
        "cards": rows[:3],
    }


def evolution_gap(a: Analyzer, tag: str, items: list[Game]) -> dict[str, Any] | None:
    """How they do when the opponent has as many evolved cards as they do, or more."""
    ids = [g["battle_id"] for g in items]
    if not ids:
        return None
    rows = a.query(
        f"""SELECT battle_id, player_tag = ? AS me, sum(coalesce(evolution_level, 0) > 0) AS evo
            FROM participant_cards WHERE kind = 'deck' AND battle_id IN ({",".join("?" * len(ids))})
            GROUP BY 1, 2""",
        (normalize_tag(tag), *ids),
    ).to_dicts()
    evos: defaultdict[int, dict[int, int]] = defaultdict(dict)
    for r in rows:
        evos[r["battle_id"]][r["me"]] = r["evo"]
    known = [g for g in items if len(evos[g["battle_id"]]) == 2]
    matched = [g for g in known if evos[g["battle_id"]][0] >= evos[g["battle_id"]][1]]
    fewer = [g for g in known if evos[g["battle_id"]][0] < evos[g["battle_id"]][1]]
    if min(len(matched), len(fewer)) < MIN_EVO_GAMES:
        return None
    diff = _rate(fewer) - _rate(matched)
    margin = 1.96 * (0.25 / len(matched) + 0.25 / len(fewer)) ** 0.5
    if diff < MIN_EVO_GAP or diff <= margin:
        return None
    return {
        "kind": "evolution_gap",
        "score": round(_z(diff, len(matched), len(fewer)), 2),
        "your_evolutions": median(evos[g["battle_id"]][1] for g in known),
        "matched": _record(matched),
        "fewer": _record(fewer),
    }


def surprises(a: Analyzer, tag: str, items: list[Game]) -> list[dict[str, Any]]:
    """Up to four things about their games they couldn't see in the game, most striking first.

    Each one is only included when the games prove it.
    """
    tag = normalize_tag(tag)
    if not items:
        return []
    found = [
        signature_card(a, tag, items),
        unique_deck(a, tag, items),
        free_wins(a, items),
        evolution_gap(a, tag, items),
    ]
    return sorted((f for f in found if f), key=lambda f: f["score"], reverse=True)[:MAX_SURPRISES]
