"""MCP server exposing the battle database to Claude.

Tabular results are returned columnar (`{"columns": [...], "rows": [[...], ...]}`): about half
the tokens of a list of objects, since keys aren't repeated on every row. Card ids are dropped
(names are what the model reasons with) and floats are rounded.
"""

from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

from mcp.server.mcpserver import MCPServer
lazy import polars as pl

from crai.analysis.analytics import (
    Analyzer,
    CardSort,
    CompareResult,
    DeckSort,
    Filters,
    MatchupMode,
    MatchupSummary,
    Overview,
    PairSort,
    PairsSummary,
    Row,
    SqlResult,
    records,
)
from crai.collect.client import ClashClient, normalize_tag
from crai.config import get_settings
from crai.storage import database as db
from crai.storage import snapshot

settings = get_settings()
analyzer = Analyzer(settings.db_path)

INSTRUCTIONS = f"""\
Clash Royale battle database built from the battle logs of top ladder players, the user, and
everyone they played against (two hops out). Use it to answer deck-building questions with data.

- The user's own player tag is {settings.my_tag or "not configured"}; "me"/"my" means this player.
- Card names are fuzzy-matched ("pekka", "xbow", "mk", "hog" work). Use find_card if unsure.
- Stats are over 1v1 battles with normal (collection) decks, excluding friendlies/boat battles,
  unless battle_types is given: "pathOfLegend" = ranked, "trail" = trophy-road ladder.
- Tables come back columnar: {{"columns": [...], "rows": [[...], ...]}}.
- win_rate counts draws as half. adj_win_rate is smoothed toward 50% so small samples rank lower;
  prefer it for ranking. `margin` is the 95% margin of error on win_rate: a difference smaller
  than the margin may be noise, so say so instead of recommending on it. Always mention games
  (and players, when given: few players = one person's skill may drive the result).
- Stats cover the last 7 days by default (the current meta); pass since_days=0 for all time
  or a larger window when a sample is too small. Say which window you used.
- top_only=true restricts to decks played by seed (top-ranked) players: closer to "the meta".
- Compute counts and rates with tools/run_sql rather than estimating them from raw rows.
- For deck/card aggregates the tools don't cover, use run_snapshot_sql (fast, polars SQL over
  the `decks` and `cards` tables). For levels, evolutions, crowns or player data, read
  get_schema then use run_sql (read-only SQLite, slower on big aggregates).
- Card levels are on the in-game scale (16 = max).
"""

mcp = MCPServer(name="clash-royale", instructions=INSTRUCTIONS)

DROP = frozenset({"card_id", "card_ids", "is_support"})


class Table(TypedDict):
    columns: list[str]
    rows: list[list[Any]]


def _round(v: Any) -> Any:
    return round(v, 3) if isinstance(v, float) else v


def table(data: pl.DataFrame | Sequence[Mapping[str, Any]], limit: int | None = None) -> Table:
    """Columnar, rounded, without ids."""
    if isinstance(data, pl.DataFrame):
        data = records(data, limit)
    elif limit is not None:
        data = data[:limit]
    if not data:
        return {"columns": [], "rows": []}
    cols = [c for c in data[0] if c not in DROP]
    return {"columns": cols, "rows": [[_round(r[c]) for c in cols] for r in data]}


def _tag(tag: str | None) -> str:
    if t := tag or settings.my_tag:
        return normalize_tag(t)
    raise ValueError("No tag given and CR_MY_TAG is not configured.")


class PairsResult(TypedDict):
    summary: PairsSummary
    partners: Table


class MatchupsResult(TypedDict):
    summary: MatchupSummary
    opponent_cards: Table


class CompleteDeckResult(TypedDict):
    given: list[str]
    matching_decks: int
    win_rate_with_given: float | None
    suggested_cards: Table
    full_decks: Table


class PlayerProfileResult(TypedDict):
    profile: Row
    current_deck: Table
    record_by_battle_type: Table
    most_used_decks: Table
    recent_battles: Table


class RefreshResult(TypedDict):
    tag: str
    name: str | None
    battles_in_log: int
    new_battles: int


@mcp.tool()
def db_overview() -> Overview:
    """Row counts, date range, battle type breakdown and recent crawl runs. Call first to gauge
    sample size."""
    return analyzer.overview()


@mcp.tool()
def find_card(query: str) -> Row:
    """Resolve a (fuzzy) card name to the canonical card with id, elixir and rarity."""
    cards = analyzer.cards()
    return records(cards.filter(cards["card_id"] == analyzer.resolve_card(query)))[0]


@mcp.tool()
def list_cards() -> Table:
    """All cards in the game: name, elixir, rarity, max_level (tower troops excluded)."""
    cards = analyzer.cards()
    return table(cards.filter(cards["is_support"] == 0))


@mcp.tool()
def card_stats(
    sort_by: CardSort = "use_rate",
    ascending: bool = False,
    min_games: int = 30,
    limit: int = 40,
    battle_types: Sequence[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> Table:
    """Meta overview: usage rate and win rate of every card."""
    df = analyzer.card_stats(
        Filters.of(battle_types, since_days),
        top_only=top_only,
        min_games=min_games,
        sort_by=sort_by,
        descending=not ascending,
    )
    return table(df, limit)


@mcp.tool()
def card_pairs(
    card: str,
    sort_by: PairSort = "games",
    min_games: int = 20,
    limit: int = 25,
    battle_types: Sequence[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> PairsResult:
    """Which cards pair well with `card`.

    pair_rate = share of decks with the card that also run the partner; lift > 1 means
    deliberately paired (not just generally popular); synergy = win rate of decks with both
    minus the card's overall win rate. Sort by games for "what do people pair it with",
    adj_win_rate or synergy for "what works best".
    """
    summary, df = analyzer.card_pairs(
        card,
        Filters.of(battle_types, since_days),
        top_only=top_only,
        min_games=min_games,
        sort_by=sort_by,
    )
    return {"summary": summary, "partners": table(df, limit)}


@mcp.tool()
def card_matchups(
    card: str,
    mode: MatchupMode = "counters",
    min_games: int = 20,
    limit: int = 25,
    battle_types: Sequence[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> MatchupsResult:
    """How decks containing `card` fare against each opponent card. 'counters' = worst
    matchups first (cards that counter it); 'favorable' = best matchups first.
    delta = win rate vs that card minus the card's overall win rate."""
    summary, df = analyzer.card_matchups(
        card,
        Filters.of(battle_types, since_days),
        top_only=top_only,
        min_games=min_games,
        mode=mode,
    )
    return {"summary": summary, "opponent_cards": table(df, limit)}


@mcp.tool()
def compare_cards(
    card_a: str,
    card_b: str,
    battle_types: Sequence[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> CompareResult:
    """Head-to-head: win rate of decks with card_a (and not card_b) against decks with card_b."""
    return analyzer.compare_cards(
        card_a, card_b, Filters.of(battle_types, since_days), top_only=top_only
    )


@mcp.tool()
def top_decks(
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    sort_by: DeckSort = "adj_win_rate",
    min_games: int = 10,
    limit: int = 20,
    battle_types: Sequence[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> Table:
    """Best exact 8-card decks, optionally required to include/exclude certain cards."""
    decks = analyzer.top_decks(
        Filters.of(battle_types, since_days),
        include=include,
        exclude=exclude,
        top_only=top_only,
        min_games=min_games,
        sort_by=sort_by,
        limit=limit,
    )
    return table(decks)


@mcp.tool()
def complete_deck(
    cards: Sequence[str],
    min_games: int = 10,
    limit: int = 15,
    battle_types: Sequence[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> CompleteDeckResult:
    """Given 1-7 cards, suggest which cards winning decks add, plus proven full decks with them."""
    r = analyzer.complete_deck(
        cards,
        Filters.of(battle_types, since_days),
        top_only=top_only,
        min_games=min_games,
        limit=limit,
    )
    return {
        "given": r["given"],
        "matching_decks": r["matching_decks"],
        "win_rate_with_given": r["win_rate_with_given"],
        "suggested_cards": table(r["suggested_cards"]),
        "full_decks": table(r["full_decks"]),
    }


@mcp.tool()
def player_profile(tag: str | None = None, recent: int = 15) -> PlayerProfileResult:
    """Profile, current deck, record by battle type, most-used decks and recent battles (with
    both decks). Reads the local database: call refresh_player first for live data. Defaults to
    the user's own tag."""
    p = analyzer.player_profile(_tag(tag), recent)
    return {
        "profile": {k: _round(v) for k, v in p["profile"].items()},
        "current_deck": table(p["current_deck"]),
        "record_by_battle_type": table(p["record_by_battle_type"]),
        "most_used_decks": table(p["most_used_decks"]),
        "recent_battles": table(p["recent_battles"]),
    }


@mcp.tool()
def player_cards(tag: str | None = None) -> Table:
    """A player's card collection with levels (level_normalized = in-game level). Defaults to
    the user."""
    return table(analyzer.player_cards(_tag(tag)))


@mcp.tool()
def decks_for_player(
    tag: str | None = None,
    min_games: int = 15,
    min_level: int | None = None,
    limit: int = 15,
    battle_types: Sequence[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> Table:
    """Proven decks the player can build entirely from cards they own. `levels` lists the
    player's in-game level for each card, in the same order as `cards`. min_level restricts to
    cards at or above that level. Defaults to the user."""
    decks = analyzer.decks_for_player(
        _tag(tag),
        Filters.of(battle_types, since_days),
        top_only=top_only,
        min_games=min_games,
        min_level=min_level,
        limit=limit,
    )
    return table(decks)


@mcp.tool()
async def refresh_player(tag: str | None = None) -> RefreshResult:
    """Fetch a player's latest profile and battle log from the live API into the database
    (e.g. before analysing the user's recent games). Defaults to the user."""
    t = _tag(tag)
    if not settings.api_tokens:
        raise ValueError("CR_API_TOKEN not configured.")
    async with ClashClient(settings.api_tokens, settings.api_base) as c:
        profile = await c.player(t)
        battles = await c.battlelog(t)
    if profile is None:
        raise ValueError(f"Player {t} not found.")
    my_tag = normalize_tag(settings.my_tag) if settings.my_tag else None
    with db.session(settings.db_path) as conn:
        db.upsert_player(conn, profile, is_seed=t == my_tag)
        new = db.ingest_battlelog(conn, battles)
        db.mark_battlelog_fetched(conn, t)
    snapshot.sync(settings.db_path)  # append the new battles now, not on the next question
    return {
        "tag": t,
        "name": profile.get("name"),
        "battles_in_log": len(battles),
        "new_battles": new,
    }


@mcp.tool()
def get_schema() -> str:
    """SQLite schema (CREATE TABLE statements with comments) for writing run_sql queries."""
    return analyzer.schema()


@mcp.tool()
def run_sql(query: str, limit: int = 200) -> SqlResult:
    """Run one read-only SQL statement against the SQLite database. Join cards on card_id for
    names. deck_key = sorted card ids joined by '-'. Already columnar."""
    return analyzer.sql(query, limit)


@mcp.tool()
def run_snapshot_sql(query: str, limit: int = 200) -> SqlResult:
    """Fast SQL (polars dialect) over the deck snapshot: milliseconds where run_sql over SQLite
    takes seconds. Tables: `decks` (battle_id, player_tag, side, deck_key, starting_trophies,
    battle_type, deck_selection, battle_time, score 1/0.5/0, cards list of card ids, is_top) and
    `cards` (card_id, name, elixir, rarity, max_level, is_support). Filter decks by card with
    `array_contains(cards, <card_id>)`. Use for aggregate deck questions the tools don't cover;
    use run_sql for levels, evolutions, crowns and player data (SQLite only)."""
    return analyzer.snapshot_sql(query, limit)


def run(transport: str = "stdio", port: int = 8765) -> None:
    match transport:
        case "stdio":
            mcp.run("stdio")
        case "streamable-http":
            mcp.run("streamable-http", host="127.0.0.1", port=port)
        case _:
            raise ValueError(f"Unknown transport: {transport}")
