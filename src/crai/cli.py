"""`crai` command line: crawl data, then ask questions of it."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
from typing import Annotated

import polars as pl
import typer

from . import db
from .analytics import Analyzer, Filters
from .api import ClashClient
from .config import get_settings
from .crawl import CrawlParams, crawl, sync_cards

app = typer.Typer(
    no_args_is_help=True, help="Clash Royale crawler, analytics and MCP server."
)

TypesOpt = Annotated[
    list[str] | None,
    typer.Option("--type", help="Battle type(s), e.g. pathOfLegend, PvP"),
]
SinceOpt = Annotated[int | None, typer.Option(help="Only battles from the last N days")]
TopOpt = Annotated[
    bool, typer.Option("--top-only", help="Only decks played by seed (top) players")
]


def _filters(types: list[str] | None, since: int | None) -> Filters:
    return Filters(battle_types=tuple(types) if types else None, since_days=since)


def _analyzer() -> Analyzer:
    return Analyzer(get_settings().db_path)


def _show(df: pl.DataFrame, limit: int) -> None:
    with pl.Config(
        tbl_rows=limit,
        tbl_cols=20,
        fmt_str_lengths=60,
        tbl_width_chars=200,
        float_precision=3,
    ):
        print(df.head(limit))


def _json(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _client():
    s = get_settings()
    if not s.api_token:
        raise typer.BadParameter("CR_API_TOKEN is not set (put it in .env).")
    return ClashClient(s.api_token, s.api_base)


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command("sync-cards")
def sync_cards_cmd() -> None:
    """Fetch the full card list into the database."""

    async def run() -> int:
        async with _client() as c:
            return await sync_cards(c, db.connect(get_settings().db_path))

    print(f"synced {asyncio.run(run())} cards")


@app.command("crawl")
def crawl_cmd(
    top: Annotated[
        int, typer.Option(help="How many top-ranked players to seed with (0 = none)")
    ] = 200,
    tag: Annotated[
        list[str] | None,
        typer.Option(help="Extra seed tag(s); your CR_MY_TAG is always added"),
    ] = None,
    depth: Annotated[
        int,
        typer.Option(
            help="0 = seeds only, 1 = + their opponents, 2 = + opponents' opponents"
        ),
    ] = 2,
    max_players: Annotated[
        int, typer.Option(help="Cap on players fetched from the API this run")
    ] = 3000,
    refresh_hours: Annotated[
        float, typer.Option(help="Skip players fetched more recently than this")
    ] = 12.0,
    location: Annotated[
        str, typer.Option(help="'global' or a location id for the seed ranking")
    ] = "global",
    concurrency: Annotated[int, typer.Option(help="Parallel API requests")] = 8,
) -> None:
    """Crawl seeds -> opponents -> opponents' opponents. Safe to re-run any time."""
    s = get_settings()
    tags = list(tag or [])
    if s.my_tag:
        tags.append(s.my_tag)
    params = CrawlParams(
        top=top,
        tags=tags,
        depth=depth,
        max_players=max_players,
        refresh_hours=refresh_hours,
        location=location,
    )

    async def run():
        if not s.api_token:
            raise typer.BadParameter("CR_API_TOKEN is not set (put it in .env).")
        async with ClashClient(s.api_token, s.api_base, concurrency=concurrency) as c:
            conn = db.connect(s.db_path)
            n = await sync_cards(c, conn)
            logging.getLogger("crai").info("synced %d cards", n)
            return await crawl(c, conn, params)

    # Only one crawl at a time: they'd contend for the DB write lock and the API rate limit.
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    lock = (s.db_path.parent / ".crawl.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        typer.echo(
            "Another `crai crawl` is already running; wait for it to finish.", err=True
        )
        raise typer.Exit(1) from None
    stats = asyncio.run(run())
    _json(stats.__dict__)


@app.command()
def overview() -> None:
    """What's in the database."""
    _json(_analyzer().overview())


@app.command()
def card(query: str) -> None:
    """Look up a card by (fuzzy) name."""
    a = _analyzer()
    cid = a.resolve_card(query)
    _show(a.cards().filter(pl.col("card_id") == cid), 1)


@app.command()
def stats(
    sort: Annotated[
        str, typer.Option(help="use_rate | win_rate | adj_win_rate | games")
    ] = "use_rate",
    min_games: int = 30,
    limit: int = 40,
    type: TypesOpt = None,
    since: SinceOpt = None,
    top_only: TopOpt = False,
) -> None:
    """Card usage and win rates."""
    _show(
        _analyzer().card_stats(
            _filters(type, since), top_only=top_only, min_games=min_games, sort_by=sort
        ),
        limit,
    )  # type: ignore[arg-type]


@app.command()
def pairs(
    card: str,
    sort: Annotated[
        str, typer.Option(help="games | adj_win_rate | lift | synergy")
    ] = "games",
    min_games: int = 20,
    limit: int = 25,
    type: TypesOpt = None,
    since: SinceOpt = None,
    top_only: TopOpt = False,
) -> None:
    """Which cards pair with CARD."""
    summary, df = _analyzer().card_pairs(
        card,
        _filters(type, since),
        top_only=top_only,
        min_games=min_games,
        sort_by=sort,  # type: ignore[arg-type]
    )
    _json(summary)
    _show(df, limit)


@app.command()
def counters(
    card: str,
    favorable: Annotated[
        bool, typer.Option(help="Show best matchups instead of counters")
    ] = False,
    min_games: int = 20,
    limit: int = 25,
    type: TypesOpt = None,
    since: SinceOpt = None,
    top_only: TopOpt = False,
) -> None:
    """Opponent cards that beat (or lose to) decks with CARD."""
    summary, df = _analyzer().card_matchups(
        card,
        _filters(type, since),
        top_only=top_only,
        min_games=min_games,
        mode="favorable" if favorable else "counters",
    )
    _json(summary)
    _show(df, limit)


@app.command()
def decks(
    with_: Annotated[
        list[str] | None, typer.Option("--with", help="Deck must contain this card")
    ] = None,
    without: Annotated[
        list[str] | None, typer.Option(help="Deck must not contain this card")
    ] = None,
    sort: Annotated[
        str, typer.Option(help="adj_win_rate | win_rate | games")
    ] = "adj_win_rate",
    min_games: int = 10,
    limit: int = 15,
    type: TypesOpt = None,
    since: SinceOpt = None,
    top_only: TopOpt = False,
) -> None:
    """Best exact decks."""
    rows = _analyzer().top_decks(
        _filters(type, since),
        include=list(with_ or []),
        exclude=list(without or []),
        top_only=top_only,
        min_games=min_games,
        sort_by=sort,  # type: ignore[arg-type]
        limit=limit,
    )
    for r in rows:
        print(
            f"{r['adj_win_rate']:.3f} adj | {r['win_rate']:.3f} wr | {r['games']:>5} games | "
            f"{r['avg_elixir']} elx | " + ", ".join(r["cards"])
        )


@app.command()
def complete(
    cards: list[str],
    min_games: int = 10,
    type: TypesOpt = None,
    since: SinceOpt = None,
    top_only: TopOpt = False,
) -> None:
    """Suggest cards to complete a partial deck."""
    _json(
        _analyzer().complete_deck(
            cards, _filters(type, since), top_only=top_only, min_games=min_games
        )
    )


@app.command()
def player(
    tag: Annotated[str | None, typer.Argument(help="Defaults to CR_MY_TAG")] = None,
) -> None:
    """Player profile, record and recent battles."""
    t = tag or get_settings().my_tag
    if not t:
        raise typer.BadParameter("Pass a tag or set CR_MY_TAG.")
    _json(_analyzer().player_profile(t))


@app.command("my-decks")
def my_decks(
    tag: Annotated[str | None, typer.Argument(help="Defaults to CR_MY_TAG")] = None,
    min_games: int = 15,
    min_level: Annotated[
        int | None, typer.Option(help="Only use your cards at or above this level")
    ] = None,
    type: TypesOpt = None,
    since: SinceOpt = None,
    top_only: TopOpt = False,
) -> None:
    """Proven decks you can build from your collection."""
    t = tag or get_settings().my_tag
    if not t:
        raise typer.BadParameter("Pass a tag or set CR_MY_TAG.")
    _json(
        _analyzer().decks_for_player(
            t,
            _filters(type, since),
            top_only=top_only,
            min_games=min_games,
            min_level=min_level,
        )
    )


@app.command()
def sql(query: str, limit: int = 100) -> None:
    """Run a read-only SQL query."""
    _show(_analyzer().query(query), limit)


@app.command()
def mcp(
    transport: Annotated[str, typer.Option(help="stdio | streamable-http")] = "stdio",
    port: int = 8765,
) -> None:
    """Run the MCP server (stdio by default)."""
    from .mcp_server import run

    run(transport, port)


def main() -> None:
    app()
