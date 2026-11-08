"""MCP server exposing the battle database to Claude."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from . import db
from .analytics import Analyzer, Filters, records
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
  unless battle_types is given (e.g. ["pathOfLegend"] for ranked ladder only).
- win_rate counts draws as half. adj_win_rate is smoothed toward 50% so small samples rank lower;
  prefer it for ranking and always mention sample size (games).
- top_only=true restricts to decks played by seed (top-ranked) players: closer to "the meta".
- For anything the tools don't cover, read get_schema then use run_sql (read-only SQLite).
- Card levels: level_normalized is on the in-game scale (e.g. 14/15/16 max).
"""

mcp = MCPServer(name="clash-royale", instructions=INSTRUCTIONS)


def _f(battle_types: list[str] | None, since_days: int | None) -> Filters:
    return Filters(
        battle_types=tuple(battle_types) if battle_types else None,
        since_days=since_days,
    )


def _tag(tag: str | None) -> str:
    t = tag or settings.my_tag
    if not t:
        raise ValueError("No tag given and CR_MY_TAG is not configured.")
    return normalize_tag(t)


@mcp.tool()
def db_overview() -> dict[str, Any]:
    """Row counts, date range, battle type breakdown and recent crawl runs. Call first to gauge sample size."""
    return analyzer.overview()


@mcp.tool()
def find_card(query: str) -> dict[str, Any]:
    """Resolve a (fuzzy) card name to the canonical card with id, elixir and rarity."""
    cid = analyzer.resolve_card(query)
    return records(analyzer.cards().filter(analyzer.cards()["card_id"] == cid))[0]


@mcp.tool()
def list_cards() -> list[dict[str, Any]]:
    """All cards in the game (name, id, elixir, rarity, is_support for tower troops)."""
    return records(analyzer.cards())


@mcp.tool()
def card_stats(
    sort_by: Literal["use_rate", "win_rate", "adj_win_rate", "games"] = "use_rate",
    ascending: bool = False,
    min_games: int = 30,
    limit: int = 40,
    battle_types: list[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> list[dict[str, Any]]:
    """Meta overview: usage rate and win rate of every card."""
    df = analyzer.card_stats(
        _f(battle_types, since_days),
        top_only=top_only,
        min_games=min_games,
        sort_by=sort_by,
        descending=not ascending,
    )
    return records(df, limit)


@mcp.tool()
def card_pairs(
    card: str,
    sort_by: Literal["games", "adj_win_rate", "lift", "synergy"] = "games",
    min_games: int = 20,
    limit: int = 25,
    battle_types: list[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> dict[str, Any]:
    """Which cards pair well with `card`.

    pair_rate = share of decks with the card that also run the partner; lift > 1 means deliberately
    paired (not just generally popular); synergy = partner-deck win rate minus the card's overall
    win rate. Sort by games for "what do people pair it with", adj_win_rate/synergy for "what works best".
    """
    summary, df = analyzer.card_pairs(
        card,
        _f(battle_types, since_days),
        top_only=top_only,
        min_games=min_games,
        sort_by=sort_by,
    )
    return {"summary": summary, "partners": records(df, limit)}


@mcp.tool()
def card_matchups(
    card: str,
    mode: Literal["counters", "favorable"] = "counters",
    min_games: int = 20,
    limit: int = 25,
    battle_types: list[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> dict[str, Any]:
    """How decks containing `card` fare against each opponent card. 'counters' = worst matchups
    first (cards that counter it); 'favorable' = best matchups first."""
    summary, df = analyzer.card_matchups(
        card,
        _f(battle_types, since_days),
        top_only=top_only,
        min_games=min_games,
        mode=mode,
    )
    return {"summary": summary, "opponent_cards": records(df, limit)}


@mcp.tool()
def compare_cards(
    card_a: str,
    card_b: str,
    battle_types: list[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> dict[str, Any]:
    """Head-to-head: win rate of decks with card_a (and not card_b) against decks with card_b."""
    return analyzer.compare_cards(
        card_a, card_b, _f(battle_types, since_days), top_only=top_only
    )


@mcp.tool()
def top_decks(
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    sort_by: Literal["adj_win_rate", "win_rate", "games"] = "adj_win_rate",
    min_games: int = 10,
    limit: int = 20,
    battle_types: list[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> list[dict[str, Any]]:
    """Best exact 8-card decks, optionally required to include/exclude certain cards."""
    return analyzer.top_decks(
        _f(battle_types, since_days),
        include=include,
        exclude=exclude,
        top_only=top_only,
        min_games=min_games,
        sort_by=sort_by,
        limit=limit,
    )


@mcp.tool()
def complete_deck(
    cards: list[str],
    min_games: int = 10,
    limit: int = 15,
    battle_types: list[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> dict[str, Any]:
    """Given 1-7 cards, suggest which cards winning decks add, plus full proven decks containing them."""
    return analyzer.complete_deck(
        cards,
        _f(battle_types, since_days),
        top_only=top_only,
        min_games=min_games,
        limit=limit,
    )


@mcp.tool()
def player_profile(tag: str | None = None, recent: int = 15) -> dict[str, Any]:
    """Profile, current deck, record by battle type, most-used decks and recent battles (with both
    decks). Defaults to the user's own tag."""
    return analyzer.player_profile(_tag(tag), recent)


@mcp.tool()
def player_cards(tag: str | None = None) -> list[dict[str, Any]]:
    """A player's card collection with levels (level_normalized = in-game level). Defaults to the user."""
    return records(analyzer.player_cards(_tag(tag)))


@mcp.tool()
def decks_for_player(
    tag: str | None = None,
    min_games: int = 15,
    min_level: int | None = None,
    limit: int = 15,
    battle_types: list[str] | None = None,
    since_days: int | None = None,
    top_only: bool = False,
) -> list[dict[str, Any]]:
    """Proven decks the player can build entirely from cards they own, with their levels per card.
    min_level restricts to cards at or above that in-game level. Defaults to the user."""
    return analyzer.decks_for_player(
        _tag(tag),
        _f(battle_types, since_days),
        top_only=top_only,
        min_games=min_games,
        min_level=min_level,
        limit=limit,
    )


@mcp.tool()
async def refresh_player(tag: str | None = None) -> dict[str, Any]:
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
    conn = db.connect(settings.db_path)
    try:
        db.upsert_player(
            conn,
            profile,
            is_seed=t == (settings.my_tag and normalize_tag(settings.my_tag)),
        )
        new = db.ingest_battlelog(conn, battles)
        db.mark_battlelog_fetched(conn, t)
        conn.commit()
    finally:
        conn.close()
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
def run_sql(query: str, limit: int = 200) -> dict[str, Any]:
    """Run one read-only SQL statement against the SQLite database. Join cards on card_id for names.
    deck_key = sorted card ids joined by '-'."""
    return analyzer.sql(query, limit)


def run(transport: str = "stdio", port: int = 8765) -> None:
    if transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host="127.0.0.1", port=port)  # type: ignore[call-overload]
