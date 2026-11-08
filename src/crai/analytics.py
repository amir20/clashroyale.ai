"""Deck/card analytics over the battle database, built on polars.

Unit of analysis is a *deck appearance*: one player's 8-card deck in one 1v1 battle, with a
score of 1 (win), 0.5 (draw) or 0 (loss). Win rates are also reported Bayesian-smoothed
toward 50% (`adj_win_rate`) so tiny samples don't dominate rankings.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl

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

SortKey = Literal["games", "win_rate", "adj_win_rate", "use_rate", "lift", "synergy"]


@dataclass(frozen=True)
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


DEFAULT_FILTERS = Filters()


class CardNotFound(ValueError):
    pass


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _adj(wins: pl.Expr, games: pl.Expr) -> pl.Expr:
    return (wins + 0.5 * PRIOR_GAMES) / (games + PRIOR_GAMES)


def records(df: pl.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
    if limit is not None:
        df = df.head(limit)
    df = df.with_columns(pl.col(pl.Float64, pl.Float32).round(3))
    return df.to_dicts()


class Analyzer:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._cache: dict[Any, Any] = {}

    # ------------------------------------------------------------ plumbing

    def _version(self) -> tuple[float, float]:
        wal = self.db_path.with_name(self.db_path.name + "-wal")
        return (
            self.db_path.stat().st_mtime if self.db_path.exists() else 0.0,
            wal.stat().st_mtime if wal.exists() else 0.0,
        )

    def _cached(self, key: Any, fn):
        full = (key, self._version())
        if full not in self._cache:
            if len(self._cache) > 32:
                self._cache.clear()
            self._cache[full] = fn()
        return self._cache[full]

    def connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"No database at {self.db_path}. Run `crai crawl` first."
            )
        return db.connect(self.db_path, read_only=True)

    def query(self, sql: str, params: tuple | dict = ()) -> pl.DataFrame:
        with self.connect() as conn:
            cur = conn.execute(sql, params)
            cols = [d[0] for d in cur.description or []]
            rows = [tuple(r) for r in cur.fetchall()]
        if not rows:
            return pl.DataFrame({c: [] for c in cols})
        return pl.DataFrame(rows, schema=cols, orient="row", infer_schema_length=None)

    # ------------------------------------------------------------ cards

    def cards(self) -> pl.DataFrame:
        return self._cached(
            "cards",
            lambda: self.query(
                "SELECT id AS card_id, name, elixir, rarity, max_level, is_support FROM cards ORDER BY name"
            ),
        )

    def card_names(self) -> dict[int, str]:
        c = self.cards()
        return dict(zip(c["card_id"].to_list(), c["name"].to_list(), strict=True))

    def resolve_card(self, query: str | int) -> int:
        """Fuzzy card lookup: id, exact name, 'pekka', 'xbow', 'mk', 'hog', ..."""
        cards = self.cards()
        ids = cards["card_id"].to_list()
        if isinstance(query, int) or str(query).strip().isdigit():
            if int(query) in ids:
                return int(query)
            raise CardNotFound(f"No card with id {query}")
        q = _norm(str(query))
        rows = [r for r in cards.iter_rows(named=True) if not r["is_support"]] or list(
            cards.iter_rows(named=True)
        )
        names = {r["card_id"]: _norm(r["name"]) for r in rows}
        initials = {
            r["card_id"]: "".join(
                w[0] for w in re.findall(r"[a-z0-9]+", r["name"].lower())
            )
            for r in rows
        }
        for matcher in (
            lambda n, i: n == q,
            lambda n, i: n.startswith(q),
            lambda n, i: i == q and len(q) >= 2,
            lambda n, i: q in n,
        ):
            hits = [cid for cid in names if matcher(names[cid], initials[cid])]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                opts = ", ".join(sorted(self.card_names()[h] for h in hits))
                raise CardNotFound(f"'{query}' is ambiguous: {opts}")
        close = difflib.get_close_matches(q, list(names.values()), n=1, cutoff=0.75)
        if close:
            return next(cid for cid, n in names.items() if n == close[0])
        suggestions = difflib.get_close_matches(
            q, list(names.values()), n=5, cutoff=0.4
        )
        by_norm = {n: self.card_names()[cid] for cid, n in names.items()}
        raise CardNotFound(
            f"No card matching '{query}'. Did you mean: {[by_norm[s] for s in suggestions]}"
        )

    def resolve_cards(self, queries: list[str | int]) -> list[int]:
        return [self.resolve_card(q) for q in queries]

    def _named(self, df: pl.DataFrame, col: str = "card_id") -> pl.DataFrame:
        info = (
            self.cards()
            .select("card_id", "name", "elixir", "rarity")
            .rename({"card_id": col})
        )
        if col != "card_id":
            info = info.rename({"name": f"{col}_name"}).drop("elixir", "rarity")
        return df.join(info, on=col, how="left")

    def _deck_names(self, ids: list[int]) -> list[str]:
        names = self.card_names()
        return [names.get(i, str(i)) for i in ids]

    def _avg_elixir(self, ids: list[int]) -> float | None:
        el = dict(
            zip(
                self.cards()["card_id"].to_list(),
                self.cards()["elixir"].to_list(),
                strict=True,
            )
        )
        vals = [el[i] for i in ids if el.get(i) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    # ------------------------------------------------------------ deck appearances

    def decks(self, f: Filters = DEFAULT_FILTERS) -> pl.DataFrame:
        """One row per (battle, player) with an 8-card deck in a 1v1 battle."""

        def load() -> pl.DataFrame:
            where, params = (
                ["b.team_size = 1", "bp.deck_key IS NOT NULL", "bp.result IS NOT NULL"],
                [],
            )
            if f.battle_types:
                where.append(
                    f"b.battle_type IN ({','.join('?' * len(f.battle_types))})"
                )
                params += list(f.battle_types)
            else:
                where.append(
                    f"coalesce(b.battle_type,'') NOT IN ({','.join('?' * len(EXCLUDED_TYPES))})"
                )
                params += list(EXCLUDED_TYPES)
            if f.deck_selection:
                where.append("coalesce(b.deck_selection, 'collection') = ?")
                params.append(f.deck_selection)
            if f.since_days:
                cutoff = (datetime.now(UTC) - timedelta(days=f.since_days)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                where.append("b.battle_time >= ?")
                params.append(cutoff)
            df = self.query(
                f"""SELECT bp.battle_id, bp.player_tag, bp.side, bp.result, bp.deck_key,
                           bp.starting_trophies, b.battle_type, b.battle_time,
                           coalesce(p.is_seed, 0) AS is_top
                    FROM battle_participants bp
                    JOIN battles b ON b.id = bp.battle_id
                    LEFT JOIN players p ON p.tag = bp.player_tag
                    WHERE {" AND ".join(where)}""",
                tuple(params),
            )
            if df.is_empty():
                return pl.DataFrame(
                    schema={
                        "battle_id": pl.String,
                        "player_tag": pl.String,
                        "side": pl.Int64,
                        "deck_key": pl.String,
                        "starting_trophies": pl.Int64,
                        "battle_type": pl.String,
                        "battle_time": pl.String,
                        "is_top": pl.Int64,
                        "score": pl.Float64,
                        "cards": pl.List(pl.Int64),
                    }
                )
            return df.with_columns(
                score=pl.when(pl.col("result") == "win")
                .then(1.0)
                .when(pl.col("result") == "draw")
                .then(0.5)
                .otherwise(0.0),
                cards=pl.col("deck_key").str.split("-").cast(pl.List(pl.Int64)),
                starting_trophies=pl.col("starting_trophies").cast(pl.Int64),
            ).drop("result")

        return self._cached(("decks", f), load)

    def _subject(self, f: Filters, top_only: bool) -> pl.DataFrame:
        d = self.decks(f)
        if top_only:
            d = d.filter(pl.col("is_top") == 1)
        if f.min_trophies is not None:
            d = d.filter(pl.col("starting_trophies") >= f.min_trophies)
        return d

    @staticmethod
    def _with_rates(df: pl.DataFrame) -> pl.DataFrame:
        return df.with_columns(
            win_rate=pl.col("wins") / pl.col("games"),
            adj_win_rate=_adj(pl.col("wins"), pl.col("games")),
        )

    # ------------------------------------------------------------ questions

    def card_stats(
        self,
        f: Filters = DEFAULT_FILTERS,
        *,
        top_only: bool = False,
        min_games: int = 30,
        sort_by: SortKey = "use_rate",
        descending: bool = True,
    ) -> pl.DataFrame:
        """Usage and win rate of every card."""
        d = self._subject(f, top_only)
        total = max(d.height, 1)
        s = (
            d.explode("cards")
            .group_by(pl.col("cards").alias("card_id"))
            .agg(games=pl.len(), wins=pl.col("score").sum())
            .pipe(self._with_rates)
            .with_columns(use_rate=pl.col("games") / total)
            .filter(pl.col("games") >= min_games)
        )
        s = self._named(s).select(
            "name",
            "card_id",
            "elixir",
            "rarity",
            "games",
            "use_rate",
            "win_rate",
            "adj_win_rate",
        )
        return s.sort(sort_by, descending=descending)

    def card_pairs(
        self,
        card: str | int,
        f: Filters = DEFAULT_FILTERS,
        *,
        top_only: bool = False,
        min_games: int = 20,
        sort_by: SortKey = "games",
    ) -> tuple[dict[str, Any], pl.DataFrame]:
        """Cards that appear in decks alongside `card`, and how those decks perform.

        pair_rate: share of decks with X that also run Y.
        lift: pair_rate / Y's overall use rate (>1 = deliberately paired, not just popular).
        synergy: win rate of X+Y decks minus win rate of all X decks.
        """
        cid = self.resolve_card(card)
        d = self._subject(f, top_only)
        total = max(d.height, 1)
        with_x = d.filter(pl.col("cards").list.contains(cid))
        n_x = with_x.height
        wr_x = with_x["score"].mean() if n_x else None
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
            .pipe(self._with_rates)
            .join(usage, on="card_id", how="left")
            .with_columns(pair_rate=pl.col("games") / max(n_x, 1))
            .with_columns(
                lift=pl.col("pair_rate") / pl.col("overall_rate"),
                synergy=pl.col("win_rate") - (wr_x or 0.0),
            )
            .filter(pl.col("games") >= min_games)
        )
        p = self._named(p).select(
            "name",
            "card_id",
            "elixir",
            "games",
            "pair_rate",
            "lift",
            "win_rate",
            "adj_win_rate",
            "synergy",
        )
        summary = {
            "card": self.card_names()[cid],
            "card_id": cid,
            "decks_with_card": n_x,
            "use_rate": n_x / total,
            "win_rate": wr_x,
            "total_decks_analyzed": d.height,
        }
        return summary, p.sort(sort_by, descending=True)

    def card_matchups(
        self,
        card: str | int,
        f: Filters = DEFAULT_FILTERS,
        *,
        top_only: bool = False,
        min_games: int = 20,
        mode: Literal["counters", "favorable"] = "counters",
    ) -> tuple[dict[str, Any], pl.DataFrame]:
        """How decks containing `card` do when the opponent runs card Y.

        mode='counters' sorts by lowest win rate (Y counters X); 'favorable' by highest.
        delta: win rate vs Y minus overall win rate of X decks.
        """
        cid = self.resolve_card(card)
        subject = self._subject(f, top_only).filter(pl.col("cards").list.contains(cid))
        wr_x = subject["score"].mean() if subject.height else None
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
            .pipe(self._with_rates)
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
        summary = {
            "card": self.card_names()[cid],
            "decks_with_card": subject.height,
            "overall_win_rate": wr_x,
            "note": "win_rate = how often the deck WITH the card wins when the opponent has opponent_card",
        }
        return summary, m.sort("adj_win_rate", descending=(mode == "favorable"))

    def top_decks(
        self,
        f: Filters = DEFAULT_FILTERS,
        *,
        include: list[str | int] | None = None,
        exclude: list[str | int] | None = None,
        top_only: bool = False,
        min_games: int = 10,
        sort_by: Literal["games", "win_rate", "adj_win_rate"] = "adj_win_rate",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Most successful/popular exact 8-card decks, optionally containing/excluding cards."""
        d = self._subject(f, top_only)
        for cid in self.resolve_cards(include or []):
            d = d.filter(pl.col("cards").list.contains(cid))
        for cid in self.resolve_cards(exclude or []):
            d = d.filter(~pl.col("cards").list.contains(cid))
        g = (
            d.group_by("deck_key")
            .agg(
                games=pl.len(),
                wins=pl.col("score").sum(),
                players=pl.col("player_tag").n_unique(),
                top_players=pl.col("player_tag")
                .filter(pl.col("is_top") == 1)
                .n_unique(),
                cards=pl.col("cards").first(),
            )
            .pipe(self._with_rates)
            .filter(pl.col("games") >= min_games)
            .sort(sort_by, "games", descending=True)
            .head(limit)
        )
        out = []
        for r in g.iter_rows(named=True):
            out.append(
                {
                    "cards": self._deck_names(r["cards"]),
                    "avg_elixir": self._avg_elixir(r["cards"]),
                    "games": r["games"],
                    "win_rate": round(r["win_rate"], 3),
                    "adj_win_rate": round(r["adj_win_rate"], 3),
                    "players": r["players"],
                    "top_players": r["top_players"],
                    "card_ids": r["cards"],
                }
            )
        return out

    def complete_deck(
        self,
        cards: list[str | int],
        f: Filters = DEFAULT_FILTERS,
        *,
        top_only: bool = False,
        min_games: int = 10,
        limit: int = 15,
    ) -> dict[str, Any]:
        """Given a partial deck, which cards do winning decks add, and which full decks exist?"""
        ids = self.resolve_cards(cards)
        d = self._subject(f, top_only)
        for cid in ids:
            d = d.filter(pl.col("cards").list.contains(cid))
        n = d.height
        cand = (
            d.explode("cards")
            .filter(~pl.col("cards").is_in(ids))
            .group_by(pl.col("cards").alias("card_id"))
            .agg(games=pl.len(), wins=pl.col("score").sum())
            .pipe(self._with_rates)
            .with_columns(pair_rate=pl.col("games") / max(n, 1))
            .filter(pl.col("games") >= min_games)
            .sort("adj_win_rate", descending=True)
        )
        cand = self._named(cand).select(
            "name", "elixir", "games", "pair_rate", "win_rate", "adj_win_rate"
        )
        return {
            "given": self._deck_names(ids),
            "matching_decks": n,
            "win_rate_with_given": d["score"].mean() if n else None,
            "suggested_cards": records(cand, limit),
            "full_decks": self.top_decks(
                f,
                include=ids,
                top_only=top_only,
                min_games=max(3, min_games // 3),
                limit=10,
            ),
        }

    def compare_cards(
        self,
        a: str | int,
        b: str | int,
        f: Filters = DEFAULT_FILTERS,
        *,
        top_only: bool = False,
    ) -> dict[str, Any]:
        """Head to head: when deck with A meets deck with B (and not both), who wins?"""
        ia, ib = self.resolve_card(a), self.resolve_card(b)
        d = self.decks(f)
        sub = self._subject(f, top_only)
        has_a = sub.filter(
            pl.col("cards").list.contains(ia) & ~pl.col("cards").list.contains(ib)
        )
        opp_b = d.filter(
            pl.col("cards").list.contains(ib) & ~pl.col("cards").list.contains(ia)
        ).select("battle_id", pl.col("side").alias("opp_side"))
        m = has_a.join(opp_b, on="battle_id").filter(
            pl.col("side") != pl.col("opp_side")
        )
        return {
            "a": self.card_names()[ia],
            "b": self.card_names()[ib],
            "games": m.height,
            "a_win_rate": m["score"].mean() if m.height else None,
        }

    # ------------------------------------------------------------ players

    def player_cards(self, tag: str) -> pl.DataFrame:
        tag = normalize_tag(tag)
        max_common = (
            self.query("SELECT max(max_level) FROM cards WHERE is_support = 0").item()
            or 16
        )
        df = self.query(
            """SELECT c.name, pc.card_id, c.rarity, c.elixir, pc.level, pc.max_level, pc.evolution_level,
                      pc.star_level, pc.count, c.is_support
               FROM player_cards pc JOIN cards c ON c.id = pc.card_id WHERE pc.tag = ?""",
            (tag,),
        )
        if df.is_empty():
            return df
        return df.with_columns(
            level_normalized=pl.col("level") + (max_common - pl.col("max_level"))
        ).sort("level_normalized", descending=True)

    def player_profile(self, tag: str, recent: int = 15) -> dict[str, Any]:
        tag = normalize_tag(tag)
        prof = self.query("SELECT * FROM players WHERE tag = ?", (tag,))
        if prof.is_empty():
            raise ValueError(
                f"Player {tag} not in database. Fetch it with `crai crawl --tag {tag} --top 0`."
            )
        deck = self.query(
            """SELECT c.name, d.level + ((SELECT max(max_level) FROM cards WHERE is_support = 0) - d.max_level)
                      AS level, d.evolution_level FROM player_current_deck d
               JOIN cards c ON c.id = d.card_id WHERE d.tag = ? ORDER BY d.slot""",
            (tag,),
        )
        by_type = self.query(
            """SELECT b.battle_type, count(*) AS games,
                      round(avg(CASE bp.result WHEN 'win' THEN 1.0 WHEN 'draw' THEN 0.5 ELSE 0 END), 3)
                        AS win_rate
               FROM battle_participants bp JOIN battles b ON b.id = bp.battle_id
               WHERE bp.player_tag = ? GROUP BY 1 ORDER BY 2 DESC""",
            (tag,),
        )
        battles = self.query(
            """SELECT b.battle_time, b.battle_type, b.game_mode, me.result, me.crowns, me.opponent_crowns,
                      me.trophy_change, me.deck_key AS my_deck, o.name AS opponent, o.player_tag AS opponent_tag,
                      o.deck_key AS opponent_deck
               FROM battle_participants me
               JOIN battles b ON b.id = me.battle_id
               LEFT JOIN battle_participants o ON o.battle_id = me.battle_id AND o.side <> me.side
               WHERE me.player_tag = ? ORDER BY b.battle_time DESC LIMIT ?""",
            (tag, recent),
        )

        def names(key: str | None) -> list[str] | None:
            return self._deck_names([int(x) for x in key.split("-")]) if key else None

        recent_rows = []
        for r in battles.iter_rows(named=True):
            r["my_deck"] = names(r["my_deck"])
            r["opponent_deck"] = names(r["opponent_deck"])
            recent_rows.append(r)

        my_decks = (
            self.decks(Filters(deck_selection=None))
            .filter(pl.col("player_tag") == tag)
            .group_by("deck_key")
            .agg(
                games=pl.len(),
                wins=pl.col("score").sum(),
                cards=pl.col("cards").first(),
            )
            .pipe(self._with_rates)
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
    ) -> list[dict[str, Any]]:
        """Proven decks the player can build from their collection, with their card levels."""
        pc = self.player_cards(tag)
        if pc.is_empty():
            raise ValueError(
                f"No card collection stored for {tag}; crawl that player first."
            )
        if min_level is not None:
            pc = pc.filter(pl.col("level_normalized") >= min_level)
        levels = dict(
            zip(pc["card_id"].to_list(), pc["level_normalized"].to_list(), strict=True)
        )
        owned = list(levels)
        d = self._subject(f, top_only).filter(
            pl.col("cards").list.eval(pl.element().is_in(owned)).list.all()
        )
        g = (
            d.group_by("deck_key")
            .agg(
                games=pl.len(),
                wins=pl.col("score").sum(),
                cards=pl.col("cards").first(),
            )
            .pipe(self._with_rates)
            .filter(pl.col("games") >= min_games)
            .sort("adj_win_rate", descending=True)
            .head(limit)
        )
        out = []
        for r in g.iter_rows(named=True):
            lv = [levels[c] for c in r["cards"]]
            out.append(
                {
                    "cards": self._deck_names(r["cards"]),
                    "your_levels": dict(
                        zip(self._deck_names(r["cards"]), lv, strict=True)
                    ),
                    "avg_level": round(sum(lv) / len(lv), 2),
                    "avg_elixir": self._avg_elixir(r["cards"]),
                    "games": r["games"],
                    "win_rate": round(r["win_rate"], 3),
                    "adj_win_rate": round(r["adj_win_rate"], 3),
                }
            )
        return out

    # ------------------------------------------------------------ meta

    def overview(self) -> dict[str, Any]:
        q = self.query
        counts = {
            t: q(f"SELECT count(*) FROM {t}").item()
            for t in (
                "cards",
                "players",
                "battles",
                "battle_participants",
                "participant_cards",
            )
        }
        counts["players_with_battlelog"] = q(
            "SELECT count(*) FROM players WHERE battlelog_fetched_at IS NOT NULL"
        ).item()
        counts["seed_players"] = q(
            "SELECT count(*) FROM players WHERE is_seed = 1"
        ).item()
        span = q(
            "SELECT min(battle_time) AS first, max(battle_time) AS last FROM battles"
        )
        types = q(
            "SELECT battle_type, team_size, count(*) AS n FROM battles GROUP BY 1, 2 ORDER BY 3 DESC"
        )
        runs = q("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 5")
        return {
            "db_path": str(self.db_path),
            "counts": counts,
            "battle_time_span": records(span)[0] if not span.is_empty() else None,
            "battle_types": records(types),
            "recent_crawls": records(runs),
            "analyzable_1v1_decks": self.decks().height,
        }

    def sql(self, sql: str, limit: int = 200) -> dict[str, Any]:
        """Run a read-only SQL statement."""
        with self.connect() as conn:
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
