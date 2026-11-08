"""Deck/card analytics over the battle database, built on polars.

Unit of analysis is a *deck appearance*: one player's 8-card deck in one 1v1 battle, with a
score of 1 (win), 0.5 (draw) or 0 (loss). Win rates are also reported Bayesian-smoothed
toward 50% (`adj_win_rate`) so tiny samples don't dominate rankings.
"""

import difflib
import re
import sqlite3
from collections.abc import Callable, Hashable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cached_property
from pathlib import Path
from typing import Any, Literal, TypedDict

lazy import polars as pl

from . import db
from .api import normalize_tag

# Battle types that don't reflect real deck strength.
EXCLUDED_TYPES = (
    "friendly",
    "boatBattle",
    "clanMate",
    "riverRaceBoatBattle",
    "unknown",
    "tutorial",
)
PRIOR_GAMES = 20  # strength of the 50% prior in adj_win_rate

type CardRef = str | int
type Row = dict[str, Any]
type SqlParams = Sequence[object] | dict[str, object]
# Plain assignments, not PEP 695 `type`: typer and pydantic read these at runtime, and typer
# can't unwrap TypeAliasType yet.
CardSort = Literal["use_rate", "win_rate", "adj_win_rate", "games"]
PairSort = Literal["games", "adj_win_rate", "lift", "synergy"]
DeckSort = Literal["adj_win_rate", "win_rate", "games"]
MatchupMode = Literal["counters", "favorable"]


# ---------------------------------------------------------------- result shapes


class DeckResult(TypedDict):
    cards: list[str]
    avg_elixir: float | None
    games: int
    win_rate: float
    adj_win_rate: float
    players: int
    top_players: int
    card_ids: list[int]


class PlayerDeckResult(TypedDict):
    cards: list[str]
    your_levels: dict[str, int]
    avg_level: float
    avg_elixir: float | None
    games: int
    win_rate: float
    adj_win_rate: float


class PairsSummary(TypedDict):
    card: str
    card_id: int
    decks_with_card: int
    use_rate: float
    win_rate: float | None
    total_decks_analyzed: int


class MatchupSummary(TypedDict):
    card: str
    decks_with_card: int
    overall_win_rate: float | None
    note: str


class CompleteDeckResult(TypedDict):
    given: list[str]
    matching_decks: int
    win_rate_with_given: float | None
    suggested_cards: list[Row]
    full_decks: list[DeckResult]


class CompareResult(TypedDict):
    a: str
    b: str
    games: int
    a_win_rate: float | None


class UsedDeck(TypedDict):
    cards: list[str]
    games: int
    win_rate: float


class PlayerProfile(TypedDict):
    profile: Row
    current_deck: list[Row]
    record_by_battle_type: list[Row]
    most_used_decks: list[UsedDeck]
    recent_battles: list[Row]


class Overview(TypedDict):
    db_path: str
    counts: dict[str, int]
    battle_time_span: Row | None
    battle_types: list[Row]
    recent_crawls: list[Row]
    analyzable_1v1_decks: int


class SqlResult(TypedDict):
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


# ---------------------------------------------------------------- helpers


@dataclass(frozen=True, slots=True)
class Filters:
    """Which battles to analyse.

    battle_types: only these types (e.g. ("pathOfLegend",)); default = everything competitive.
    since_days: only battles from the last N days.
    deck_selection: 'collection' (normal decks) by default; None to include draft/special modes.
    min_trophies: subject deck's starting trophies >= N (ladder only; Path of Legend has none).
    """

    battle_types: tuple[str, ...] | None = None
    since_days: int | None = None
    deck_selection: str | None = "collection"
    min_trophies: int | None = None

    @classmethod
    def of(
        cls, battle_types: Sequence[str] | None = None, since_days: int | None = None
    ) -> Filters:
        return cls(
            battle_types=tuple(battle_types) if battle_types else None, since_days=since_days
        )


DEFAULT_FILTERS = Filters()


class CardNotFound(ValueError):
    pass


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _adj(wins: pl.Expr, games: pl.Expr) -> pl.Expr:
    return (wins + 0.5 * PRIOR_GAMES) / (games + PRIOR_GAMES)


def _with_rates(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        win_rate=pl.col("wins") / pl.col("games"),
        adj_win_rate=_adj(pl.col("wins"), pl.col("games")),
    )


def _mean(df: pl.DataFrame, col: str = "score") -> float | None:
    m = df[col].mean() if df.height else None
    return float(m) if isinstance(m, int | float) else None


def records(df: pl.DataFrame, limit: int | None = None) -> list[Row]:
    if limit is not None:
        df = df.head(limit)
    return df.with_columns(pl.col(pl.Float64, pl.Float32).round(3)).to_dicts()


def _decks_schema() -> frozendict[str, pl.DataType]:
    # A function, not a module constant, so `lazy import polars` stays lazy until first query.
    return frozendict(
        battle_id=pl.String(),
        player_tag=pl.String(),
        side=pl.Int64(),
        deck_key=pl.String(),
        starting_trophies=pl.Int64(),
        battle_type=pl.String(),
        battle_time=pl.String(),
        is_top=pl.Int64(),
        score=pl.Float64(),
        cards=pl.List(pl.Int64()),
    )


class Analyzer:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._cache: dict[Hashable, Any] = {}

    # ------------------------------------------------------------ plumbing

    def _version(self) -> tuple[float, float]:
        wal = self.db_path.with_name(self.db_path.name + "-wal")
        return (
            self.db_path.stat().st_mtime if self.db_path.exists() else 0.0,
            wal.stat().st_mtime if wal.exists() else 0.0,
        )

    def _cached[T](self, key: Hashable, fn: Callable[[], T]) -> T:
        """Memoize per DB version, so new crawl data invalidates the cache."""
        full = (key, self._version())
        if full not in self._cache:
            if len(self._cache) > 32:
                self._cache.clear()
            self._cache[full] = fn()
        return self._cache[full]

    def session(self) -> AbstractContextManager[sqlite3.Connection]:
        if not self.db_path.exists():
            raise FileNotFoundError(f"No database at {self.db_path}. Run `crai crawl` first.")
        return db.session(self.db_path, read_only=True)

    def query(self, sql: str, params: SqlParams = ()) -> pl.DataFrame:
        with self.session() as conn:
            cur = conn.execute(sql, params)
            cols = [d[0] for d in cur.description or []]
            rows = [tuple(r) for r in cur.fetchall()]
        if not rows:
            return pl.DataFrame({c: [] for c in cols})
        return pl.DataFrame(rows, schema=cols, orient="row", infer_schema_length=None)

    def scalar(self, sql: str, params: SqlParams = ()) -> Any:
        with self.session() as conn:
            row = conn.execute(sql, params).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------ cards

    def cards(self) -> pl.DataFrame:
        return self._cached(
            "cards",
            lambda: self.query(
                "SELECT id AS card_id, name, elixir, rarity, max_level, is_support"
                " FROM cards ORDER BY name"
            ),
        )

    def card_names(self) -> dict[int, str]:
        c = self.cards()
        return dict(zip(c["card_id"].to_list(), c["name"].to_list(), strict=True))

    def _elixir(self) -> dict[int, int | None]:
        c = self.cards()
        return dict(zip(c["card_id"].to_list(), c["elixir"].to_list(), strict=True))

    def resolve_card(self, query: CardRef) -> int:
        """Fuzzy card lookup: id, exact name, 'pekka', 'xbow', 'mk', 'hog', ..."""
        cards = self.cards()
        all_names = self.card_names()
        if isinstance(query, int) or query.strip().isdigit():
            if int(query) in all_names:
                return int(query)
            raise CardNotFound(f"No card with id {query}")
        q = _norm(query)
        rows = [r for r in cards.iter_rows(named=True) if not r["is_support"]] or list(
            cards.iter_rows(named=True)
        )
        names = {r["card_id"]: _norm(r["name"]) for r in rows}
        initials = {
            r["card_id"]: "".join(w[0] for w in re.findall(r"[a-z0-9]+", r["name"].lower()))
            for r in rows
        }
        matchers: list[Callable[[str, str], bool]] = [
            lambda n, _: n == q,
            lambda n, _: n.startswith(q),
            lambda _, i: i == q and len(q) >= 2,
            lambda n, _: q in n,
        ]
        for matcher in matchers:
            hits = [cid for cid in names if matcher(names[cid], initials[cid])]
            if len(hits) == 1:
                return hits[0]
            if hits:
                opts = ", ".join(sorted(all_names[h] for h in hits))
                raise CardNotFound(f"'{query}' is ambiguous: {opts}")
        by_norm = {n: cid for cid, n in names.items()}
        if close := difflib.get_close_matches(q, list(by_norm), n=1, cutoff=0.75):
            return by_norm[close[0]]
        suggestions = difflib.get_close_matches(q, list(by_norm), n=5, cutoff=0.4)
        hints = [all_names[by_norm[s]] for s in suggestions]
        raise CardNotFound(f"No card matching '{query}'. Did you mean: {hints}")

    def resolve_cards(self, queries: Sequence[CardRef]) -> list[int]:
        return [self.resolve_card(q) for q in queries]

    def _named(self, df: pl.DataFrame) -> pl.DataFrame:
        return df.join(
            self.cards().select("card_id", "name", "elixir", "rarity"), on="card_id", how="left"
        )

    def _deck_names(self, ids: Sequence[int]) -> list[str]:
        names = self.card_names()
        return [names.get(i, str(i)) for i in ids]

    def _avg_elixir(self, ids: Sequence[int]) -> float | None:
        el = self._elixir()
        vals = [e for i in ids if (e := el.get(i)) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    def _deck_result(self, r: Row) -> DeckResult:
        return {
            "cards": self._deck_names(r["cards"]),
            "avg_elixir": self._avg_elixir(r["cards"]),
            "games": r["games"],
            "win_rate": round(r["win_rate"], 3),
            "adj_win_rate": round(r["adj_win_rate"], 3),
            "players": r["players"],
            "top_players": r["top_players"],
            "card_ids": r["cards"],
        }

    # ------------------------------------------------------------ deck appearances

    def decks(self, f: Filters = DEFAULT_FILTERS) -> pl.DataFrame:
        """One row per (battle, player) with an 8-card deck in a 1v1 battle."""
        return self._cached(("decks", f), lambda: self._load_decks(f))

    def _load_decks(self, f: Filters) -> pl.DataFrame:
        where = ["b.team_size = 1", "bp.deck_key IS NOT NULL", "bp.result IS NOT NULL"]
        params: list[object] = []
        types = f.battle_types or EXCLUDED_TYPES
        op = "IN" if f.battle_types else "NOT IN"
        where.append(f"coalesce(b.battle_type, '') {op} ({','.join('?' * len(types))})")
        params += types
        if f.deck_selection:
            where.append("coalesce(b.deck_selection, 'collection') = ?")
            params.append(f.deck_selection)
        if f.since_days:
            cutoff = datetime.now(UTC) - timedelta(days=f.since_days)
            where.append("b.battle_time >= ?")
            params.append(cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"))
        df = self.query(
            f"""SELECT bp.battle_id, bp.player_tag, bp.side, bp.result, bp.deck_key,
                       bp.starting_trophies, b.battle_type, b.battle_time,
                       coalesce(p.is_seed, 0) AS is_top
                FROM battle_participants bp
                JOIN battles b ON b.id = bp.battle_id
                LEFT JOIN players p ON p.tag = bp.player_tag
                WHERE {" AND ".join(where)}""",
            params,
        )
        if df.is_empty():
            return pl.DataFrame(schema=_decks_schema())
        return df.with_columns(
            score=pl.when(pl.col("result") == "win")
            .then(1.0)
            .when(pl.col("result") == "draw")
            .then(0.5)
            .otherwise(0.0),
            cards=pl.col("deck_key").str.split("-").cast(pl.List(pl.Int64)),
            starting_trophies=pl.col("starting_trophies").cast(pl.Int64),
        ).drop("result")

    def _subject(self, f: Filters, top_only: bool) -> pl.DataFrame:
        d = self.decks(f)
        if top_only:
            d = d.filter(pl.col("is_top") == 1)
        if f.min_trophies is not None:
            d = d.filter(pl.col("starting_trophies") >= f.min_trophies)
        return d

    @staticmethod
    def _containing(d: pl.DataFrame, ids: Sequence[int]) -> pl.DataFrame:
        for cid in ids:
            d = d.filter(pl.col("cards").list.contains(cid))
        return d

    # ------------------------------------------------------------ questions

    def card_stats(
        self,
        f: Filters = DEFAULT_FILTERS,
        *,
        top_only: bool = False,
        min_games: int = 30,
        sort_by: CardSort = "use_rate",
        descending: bool = True,
    ) -> pl.DataFrame:
        """Usage and win rate of every card."""
        d = self._subject(f, top_only)
        total = max(d.height, 1)
        s = (
            d.explode("cards")
            .group_by(pl.col("cards").alias("card_id"))
            .agg(games=pl.len(), wins=pl.col("score").sum())
            .pipe(_with_rates)
            .with_columns(use_rate=pl.col("games") / total)
            .filter(pl.col("games") >= min_games)
        )
        cols = [
            "name",
            "card_id",
            "elixir",
            "rarity",
            "games",
            "use_rate",
            "win_rate",
            "adj_win_rate",
        ]
        return self._named(s).select(cols).sort(sort_by, descending=descending)

    def card_pairs(
        self,
        card: CardRef,
        f: Filters = DEFAULT_FILTERS,
        *,
        top_only: bool = False,
        min_games: int = 20,
        sort_by: PairSort = "games",
    ) -> tuple[PairsSummary, pl.DataFrame]:
        """Cards that appear in decks alongside `card`, and how those decks perform.

        pair_rate: share of decks with X that also run Y.
        lift: pair_rate / Y's overall use rate (>1 = deliberately paired, not just popular).
        synergy: win rate of X+Y decks minus win rate of all X decks.
        """
        cid = self.resolve_card(card)
        d = self._subject(f, top_only)
        total = max(d.height, 1)
        with_x = self._containing(d, [cid])
        n_x = with_x.height
        wr_x = _mean(with_x)
        usage = (
            d.explode("cards")
            .group_by(pl.col("cards").alias("card_id"))
            .agg(overall_rate=pl.len() / total)
        )
        p = (
            with_x.explode("cards")
            .filter(pl.col("cards") != cid)
            .group_by(pl.col("cards").alias("card_id"))
            .agg(games=pl.len(), wins=pl.col("score").sum())
            .pipe(_with_rates)
            .join(usage, on="card_id", how="left")
            .with_columns(pair_rate=pl.col("games") / max(n_x, 1))
            .with_columns(
                lift=pl.col("pair_rate") / pl.col("overall_rate"),
                synergy=pl.col("win_rate") - (wr_x or 0.0),
            )
            .filter(pl.col("games") >= min_games)
        )
        cols = ["name", "card_id", "elixir", "games", "pair_rate", "lift"]
        cols += ["win_rate", "adj_win_rate", "synergy"]
        summary: PairsSummary = {
            "card": self.card_names()[cid],
            "card_id": cid,
            "decks_with_card": n_x,
            "use_rate": n_x / total,
            "win_rate": wr_x,
            "total_decks_analyzed": d.height,
        }
        return summary, self._named(p).select(cols).sort(sort_by, descending=True)

    def card_matchups(
        self,
        card: CardRef,
        f: Filters = DEFAULT_FILTERS,
        *,
        top_only: bool = False,
        min_games: int = 20,
        mode: MatchupMode = "counters",
    ) -> tuple[MatchupSummary, pl.DataFrame]:
        """How decks containing `card` do when the opponent runs card Y.

        mode='counters' sorts by lowest win rate (Y counters X); 'favorable' by highest.
        delta: win rate vs Y minus overall win rate of X decks.
        """
        cid = self.resolve_card(card)
        subject = self._containing(self._subject(f, top_only), [cid])
        wr_x = _mean(subject)
        opp = self.decks(f).select(
            "battle_id", pl.col("side").alias("opp_side"), pl.col("cards").alias("opp")
        )
        m = (
            subject.select("battle_id", "side", "score")
            .join(opp, on="battle_id")
            .filter(pl.col("side") != pl.col("opp_side"))
            .explode("opp")
            .group_by(pl.col("opp").alias("card_id"))
            .agg(games=pl.len(), wins=pl.col("score").sum())
            .pipe(_with_rates)
            .with_columns(delta=pl.col("win_rate") - (wr_x or 0.0))
            .filter(pl.col("games") >= min_games)
        )
        m = self._named(m).select(
            pl.col("name").alias("opponent_card"),
            "card_id",
            "games",
            "win_rate",
            "adj_win_rate",
            "delta",
        )
        summary: MatchupSummary = {
            "card": self.card_names()[cid],
            "decks_with_card": subject.height,
            "overall_win_rate": wr_x,
            "note": "win_rate = how often the deck WITH the card wins vs opponent_card",
        }
        return summary, m.sort("adj_win_rate", descending=mode == "favorable")

    def top_decks(
        self,
        f: Filters = DEFAULT_FILTERS,
        *,
        include: Sequence[CardRef] = (),
        exclude: Sequence[CardRef] = (),
        top_only: bool = False,
        min_games: int = 10,
        sort_by: DeckSort = "adj_win_rate",
        limit: int = 20,
    ) -> list[DeckResult]:
        """Most successful/popular exact 8-card decks, optionally containing/excluding cards."""
        d = self._containing(self._subject(f, top_only), self.resolve_cards(include))
        for cid in self.resolve_cards(exclude):
            d = d.filter(~pl.col("cards").list.contains(cid))
        g = (
            d.group_by("deck_key")
            .agg(
                games=pl.len(),
                wins=pl.col("score").sum(),
                players=pl.col("player_tag").n_unique(),
                top_players=pl.col("player_tag").filter(pl.col("is_top") == 1).n_unique(),
                cards=pl.col("cards").first(),
            )
            .pipe(_with_rates)
            .filter(pl.col("games") >= min_games)
            .sort(sort_by, "games", descending=True)
            .head(limit)
        )
        return [self._deck_result(r) for r in g.iter_rows(named=True)]

    def complete_deck(
        self,
        cards: Sequence[CardRef],
        f: Filters = DEFAULT_FILTERS,
        *,
        top_only: bool = False,
        min_games: int = 10,
        limit: int = 15,
    ) -> CompleteDeckResult:
        """Given a partial deck, which cards do winning decks add, and which full decks exist?"""
        ids = self.resolve_cards(cards)
        d = self._containing(self._subject(f, top_only), ids)
        n = d.height
        cand = (
            d.explode("cards")
            .filter(~pl.col("cards").is_in(ids))
            .group_by(pl.col("cards").alias("card_id"))
            .agg(games=pl.len(), wins=pl.col("score").sum())
            .pipe(_with_rates)
            .with_columns(pair_rate=pl.col("games") / max(n, 1))
            .filter(pl.col("games") >= min_games)
            .sort("adj_win_rate", descending=True)
        )
        cols = ["name", "elixir", "games", "pair_rate", "win_rate", "adj_win_rate"]
        return {
            "given": self._deck_names(ids),
            "matching_decks": n,
            "win_rate_with_given": _mean(d),
            "suggested_cards": records(self._named(cand).select(cols), limit),
            "full_decks": self.top_decks(
                f, include=ids, top_only=top_only, min_games=max(3, min_games // 3), limit=10
            ),
        }

    def compare_cards(
        self, a: CardRef, b: CardRef, f: Filters = DEFAULT_FILTERS, *, top_only: bool = False
    ) -> CompareResult:
        """Head to head: when deck with A meets deck with B (and not both), who wins?"""
        ia, ib = self.resolve_card(a), self.resolve_card(b)
        has = pl.col("cards").list.contains
        has_a = self._subject(f, top_only).filter(has(ia) & ~has(ib))
        opp_b = (
            self.decks(f)
            .filter(has(ib) & ~has(ia))
            .select("battle_id", pl.col("side").alias("opp_side"))
        )
        m = has_a.join(opp_b, on="battle_id").filter(pl.col("side") != pl.col("opp_side"))
        names = self.card_names()
        return {"a": names[ia], "b": names[ib], "games": m.height, "a_win_rate": _mean(m)}

    # ------------------------------------------------------------ players

    @cached_property
    def _max_common_level(self) -> int:
        return int(self.scalar("SELECT max(max_level) FROM cards WHERE is_support = 0") or 16)

    def player_cards(self, tag: str) -> pl.DataFrame:
        df = self.query(
            """SELECT c.name, pc.card_id, c.rarity, c.elixir, pc.level, pc.max_level,
                      pc.evolution_level, pc.star_level, pc.count, c.is_support
               FROM player_cards pc JOIN cards c ON c.id = pc.card_id WHERE pc.tag = ?""",
            (normalize_tag(tag),),
        )
        if df.is_empty():
            return df
        return df.with_columns(
            level_normalized=pl.col("level") + (self._max_common_level - pl.col("max_level"))
        ).sort("level_normalized", descending=True)

    def player_profile(self, tag: str, recent: int = 15) -> PlayerProfile:
        tag = normalize_tag(tag)
        prof = self.query("SELECT * FROM players WHERE tag = ?", (tag,))
        if prof.is_empty():
            raise ValueError(
                f"Player {tag} not in database. Fetch it with `crai crawl --tag {tag} --top 0`."
            )
        deck = self.query(
            """SELECT c.name, d.level + (? - d.max_level) AS level, d.evolution_level
               FROM player_current_deck d JOIN cards c ON c.id = d.card_id
               WHERE d.tag = ? ORDER BY d.slot""",
            (self._max_common_level, tag),
        )
        by_type = self.query(
            """SELECT b.battle_type, count(*) AS games,
                      round(avg(CASE bp.result WHEN 'win' THEN 1.0 WHEN 'draw' THEN 0.5 ELSE 0 END),
                            3) AS win_rate
               FROM battle_participants bp JOIN battles b ON b.id = bp.battle_id
               WHERE bp.player_tag = ? GROUP BY 1 ORDER BY 2 DESC""",
            (tag,),
        )
        battles = self.query(
            """SELECT b.battle_time, b.battle_type, b.game_mode, me.result, me.crowns,
                      me.opponent_crowns, me.trophy_change, me.deck_key AS my_deck,
                      o.name AS opponent, o.player_tag AS opponent_tag, o.deck_key AS opponent_deck
               FROM battle_participants me
               JOIN battles b ON b.id = me.battle_id
               LEFT JOIN battle_participants o ON o.battle_id = me.battle_id AND o.side <> me.side
               WHERE me.player_tag = ? ORDER BY b.battle_time DESC LIMIT ?""",
            (tag, recent),
        )

        def names(key: str | None) -> list[str] | None:
            return self._deck_names([int(x) for x in key.split("-")]) if key else None

        recent_rows = [
            r | {"my_deck": names(r["my_deck"]), "opponent_deck": names(r["opponent_deck"])}
            for r in battles.iter_rows(named=True)
        ]
        my_decks = (
            self.decks(Filters(deck_selection=None))
            .filter(pl.col("player_tag") == tag)
            .group_by("deck_key")
            .agg(games=pl.len(), wins=pl.col("score").sum(), cards=pl.col("cards").first())
            .pipe(_with_rates)
            .sort("games", descending=True)
            .head(5)
        )
        return {
            "profile": records(prof)[0],
            "current_deck": records(deck),
            "record_by_battle_type": records(by_type),
            "most_used_decks": [
                {
                    "cards": self._deck_names(r["cards"]),
                    "games": r["games"],
                    "win_rate": round(r["win_rate"], 3),
                }
                for r in my_decks.iter_rows(named=True)
            ],
            "recent_battles": recent_rows,
        }

    def decks_for_player(
        self,
        tag: str,
        f: Filters = DEFAULT_FILTERS,
        *,
        top_only: bool = False,
        min_games: int = 15,
        min_level: int | None = None,
        limit: int = 15,
    ) -> list[PlayerDeckResult]:
        """Proven decks the player can build from their collection, with their card levels."""
        pc = self.player_cards(tag)
        if pc.is_empty():
            raise ValueError(f"No card collection stored for {tag}; crawl that player first.")
        if min_level is not None:
            pc = pc.filter(pl.col("level_normalized") >= min_level)
        levels: dict[int, int] = dict(
            zip(pc["card_id"].to_list(), pc["level_normalized"].to_list(), strict=True)
        )
        owned = list(levels)
        d = self._subject(f, top_only).filter(
            pl.col("cards").list.eval(pl.element().is_in(owned)).list.all()
        )
        g = (
            d.group_by("deck_key")
            .agg(games=pl.len(), wins=pl.col("score").sum(), cards=pl.col("cards").first())
            .pipe(_with_rates)
            .filter(pl.col("games") >= min_games)
            .sort("adj_win_rate", descending=True)
            .head(limit)
        )
        out: list[PlayerDeckResult] = []
        for r in g.iter_rows(named=True):
            card_names = self._deck_names(r["cards"])
            lv = [levels[c] for c in r["cards"]]
            out.append(
                {
                    "cards": card_names,
                    "your_levels": dict(zip(card_names, lv, strict=True)),
                    "avg_level": round(sum(lv) / len(lv), 2),
                    "avg_elixir": self._avg_elixir(r["cards"]),
                    "games": r["games"],
                    "win_rate": round(r["win_rate"], 3),
                    "adj_win_rate": round(r["adj_win_rate"], 3),
                }
            )
        return out

    # ------------------------------------------------------------ meta

    def overview(self) -> Overview:
        tables = ("cards", "players", "battles", "battle_participants", "participant_cards")
        counts: dict[str, int] = {t: self.scalar(f"SELECT count(*) FROM {t}") for t in tables}
        counts["players_with_battlelog"] = self.scalar(
            "SELECT count(*) FROM players WHERE battlelog_fetched_at IS NOT NULL"
        )
        counts["seed_players"] = self.scalar("SELECT count(*) FROM players WHERE is_seed = 1")
        span = self.query("SELECT min(battle_time) AS first, max(battle_time) AS last FROM battles")
        types = self.query(
            "SELECT battle_type, team_size, count(*) AS n FROM battles"
            " GROUP BY 1, 2 ORDER BY 3 DESC"
        )
        runs = self.query("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 5")
        return {
            "db_path": str(self.db_path),
            "counts": counts,
            "battle_time_span": records(span)[0] if not span.is_empty() else None,
            "battle_types": records(types),
            "recent_crawls": records(runs),
            "analyzable_1v1_decks": self.decks().height,
        }

    def sql(self, sql: str, limit: int = 200) -> SqlResult:
        """Run one read-only SQL statement."""
        with self.session() as conn:
            cur = conn.execute(sql)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(limit + 1)
        return {
            "columns": cols,
            "rows": [list(r) for r in rows[:limit]],
            "truncated": len(rows) > limit,
        }

    def schema(self) -> str:
        return db.SCHEMA
