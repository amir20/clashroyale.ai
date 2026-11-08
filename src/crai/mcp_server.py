"""MCP server exposing the battle database to Claude."""

from collections.abc import Sequence
from typing import TypedDict

from mcp.server.mcpserver import MCPServer

from . import db
from .analytics import (
    Analyzer,
    CardSort,
    CompareResult,
    CompleteDeckResult,
    DeckResult,
    DeckSort,
    Filters,
    MatchupMode,
    MatchupSummary,
    Overview,
    PairSort,
    PairsSummary,
    PlayerDeckResult,
    PlayerProfile,
    Row,
    SqlResult,
    records,
)
from .api import ClashClient, normalize_tag
from .config import get_settings

settings = get_settings()
analyzer = Analyzer(settings.db_path)

INSTRUCTIONS = f"""\
Clash Royale battle database built from the battle logs of top ladder players, the user, and
everyone they played against (two hops out). Use it to answer deck-building questions with data.

- The user's own player tag is {settings.my_tag or "not configured"}; "me"/"my" means this player.
- Card names are fuzzy-matched ("pekka", "xbow", "mk", "hog" work). Use find_card if unsure.
- Stats are over 1v1 battles with normal (collection) decks, excluding friendlies/boat battles,
  unless battle_types is given: "pathOfLegend" = ranked, "trail" = trophy-road ladder.
- win_rate counts draws as half. adj_win_rate is smoothed toward 50% so small samples rank lower;
  prefer it for ranking and always mention sample size (games).
- top_only=true restricts to decks played by seed (top-ranked) players: closer to "the meta".
- Compute counts and rates with tools/run_sql rather than estimating them from raw rows.
- For anything the tools don't cover, read get_schema then use run_sql (read-only SQLite).
- Card levels: level_normalized is on the in-game scale (e.g. 14/15/16 max).
"""

mcp = MCPServer(name="clash-royale", instructions=INSTRUCTIONS)


def _tag(tag: str | None) -> str:
    if t := tag or settings.my_tag:
        return normalize_tag(t)
    raise ValueError("No tag given and CR_MY_TAG is not configured.")


class PairsResult(PairsSummary):
    partners: list[Row]


class MatchupsResult(MatchupSummary):
    opponent_cards: list[Row]


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
def list_cards() -> list[Row]:
    """All cards in the game (name, id, elixir, rarity, is_support for tower troops)."""
    return records(analyzer.cards())


@mcp.tool()
def card_stats(
    sort_by: CardSort = "use_rate",
    ascending: bool = False,
    min_games: int = 30,
    limit: int = 40,
    battle_types: Sequence[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> list[Row]:
    """Meta overview: usage rate and win rate of every card."""
    df = analyzer.card_stats(
        Filters.of(battle_types, since_days),
        top_only=top_only,
        min_games=min_games,
        sort_by=sort_by,
        descending=not ascending,
    )
    return records(df, limit)


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
    deliberately paired (not just generally popular); synergy = partner-deck win rate minus the
    card's overall win rate. Sort by games for "what do people pair it with", adj_win_rate or
    synergy for "what works best".
    """
    summary, df = analyzer.card_pairs(
        card,
        Filters.of(battle_types, since_days),
        top_only=top_only,
        min_games=min_games,
        sort_by=sort_by,
    )
    return {**summary, "partners": records(df, limit)}


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
    matchups first (cards that counter it); 'favorable' = best matchups first."""
    summary, df = analyzer.card_matchups(
        card,
        Filters.of(battle_types, since_days),
        top_only=top_only,
        min_games=min_games,
        mode=mode,
    )
    return {**summary, "opponent_cards": records(df, limit)}


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
) -> list[DeckResult]:
    """Best exact 8-card decks, optionally required to include/exclude certain cards."""
    return analyzer.top_decks(
        Filters.of(battle_types, since_days),
        include=include,
        exclude=exclude,
        top_only=top_only,
        min_games=min_games,
        sort_by=sort_by,
        limit=limit,
    )


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
    return analyzer.complete_deck(
        cards,
        Filters.of(battle_types, since_days),
        top_only=top_only,
        min_games=min_games,
        limit=limit,
    )


@mcp.tool()
def player_profile(tag: str | None = None, recent: int = 15) -> PlayerProfile:
    """Profile, current deck, record by battle type, most-used decks and recent battles (with
    both decks). Defaults to the user's own tag."""
    return analyzer.player_profile(_tag(tag), recent)


@mcp.tool()
def player_cards(tag: str | None = None) -> list[Row]:
    """A player's card collection with levels (level_normalized = in-game level). Defaults to
    the user."""
    return records(analyzer.player_cards(_tag(tag)))


@mcp.tool()
def decks_for_player(
    tag: str | None = None,
    min_games: int = 15,
    min_level: int | None = None,
    limit: int = 15,
    battle_types: Sequence[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> list[PlayerDeckResult]:
    """Proven decks the player can build entirely from cards they own, with their level per
    card. min_level restricts to cards at or above that in-game level. Defaults to the user."""
    return analyzer.decks_for_player(
        _tag(tag),
        Filters.of(battle_types, since_days),
        top_only=top_only,
        min_games=min_games,
        min_level=min_level,
        limit=limit,
    )


@mcp.tool()
async def refresh_player(tag: str | None = None) -> RefreshResult:
    """Fetch a player's latest profile and battle log from the live API into the database
    (e.g. before analysing the user's recent games). Defaults to the user."""
    t = _tag(tag)
    if not settings.api_token:
        raise ValueError("CR_API_TOKEN not configured.")
    async with ClashClient(settings.api_token, settings.api_base) as c:
        profile = await c.player(t)
        battles = await c.battlelog(t)
    if profile is None:
        raise ValueError(f"Player {t} not found.")
    my_tag = normalize_tag(settings.my_tag) if settings.my_tag else None
    with db.session(settings.db_path) as conn:
        db.upsert_player(conn, profile, is_seed=t == my_tag)
        new = db.ingest_battlelog(conn, battles)
        db.mark_battlelog_fetched(conn, t)
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
    names. deck_key = sorted card ids joined by '-'."""
    return analyzer.sql(query, limit)


def run(transport: str = "stdio", port: int = 8765) -> None:
    match transport:
        case "stdio":
            mcp.run("stdio")
        case "streamable-http":
            mcp.run("streamable-http", host="127.0.0.1", port=port)
        case _:
            raise ValueError(f"Unknown transport: {transport}")
