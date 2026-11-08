"""`crai` command line: crawl data, then ask questions of it."""

import asyncio
import fcntl
import json
import logging
import sys
from typing import Annotated

import typer

# Lazy (PEP 810): `crai crawl` never loads polars, `crai stats` never loads httpx.
lazy import polars as pl

from .config import Settings, get_settings
lazy from . import db, snapshot
lazy from .analytics import Analyzer, CardSort, DeckSort, Filters, PairSort
lazy from .api import ClashClient
lazy from .crawl import CrawlParams, crawl, sync_cards
lazy from .mcp_server import run as run_mcp

app = typer.Typer(no_args_is_help=True, help="Clash Royale crawler, analytics and MCP server.")
log = logging.getLogger("crai")

# typer inspects these at runtime, so they are plain aliases rather than PEP 695 `type`.

TypesOpt = Annotated[
    list[str] | None, typer.Option("--type", help="Battle type(s), e.g. pathOfLegend, trail")
]
SinceOpt = Annotated[int | None, typer.Option(help="Only battles from the last N days")]
TopOpt = Annotated[bool, typer.Option("--top-only", help="Only decks played by seed (top) players")]
TagArg = Annotated[str | None, typer.Argument(help="Player tag; defaults to CR_MY_TAG")]


def _analyzer() -> Analyzer:
    return Analyzer(get_settings().db_path)


def _show(df: pl.DataFrame, limit: int) -> None:
    with pl.Config(
        tbl_rows=limit, tbl_cols=20, fmt_str_lengths=60, tbl_width_chars=200, float_precision=3
    ):
        print(df.head(limit))


def _json(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _require_token(s: Settings) -> str:
    if not s.api_token:
        raise typer.BadParameter("CR_API_TOKEN is not set (put it in .env).")
    return s.api_token


def _my_tag(tag: str | None) -> str:
    if t := tag or get_settings().my_tag:
        return t
    raise typer.BadParameter("Pass a tag or set CR_MY_TAG.")


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
    s = get_settings()

    async def run() -> int:
        async with ClashClient(_require_token(s), s.api_base) as c:
            with db.session(s.db_path) as conn:
                return await sync_cards(c, conn)

    print(f"synced {asyncio.run(run())} cards")


@app.command("crawl")
def crawl_cmd(
    top: Annotated[int, typer.Option(help="Top-ranked players to seed with (0 = none)")] = 200,
    tag: Annotated[
        list[str] | None, typer.Option(help="Extra seed tag(s); CR_MY_TAG is always added")
    ] = None,
    depth: Annotated[
        int, typer.Option(help="0 = seeds, 1 = + opponents, 2 = + opponents' opponents")
    ] = 2,
    max_players: Annotated[int, typer.Option(help="Cap on players fetched this run")] = 3000,
    refresh_hours: Annotated[
        float, typer.Option(help="Skip players fetched more recently than this")
    ] = 12.0,
    location: Annotated[str, typer.Option(help="'global' or a location id")] = "global",
    concurrency: Annotated[int, typer.Option(help="Parallel API requests")] = 8,
) -> None:
    """Crawl seeds -> opponents -> opponents' opponents. Safe to re-run any time."""
    s = get_settings()
    token = _require_token(s)
    tags = [*(tag or []), *([s.my_tag] if s.my_tag else [])]
    params = CrawlParams(
        top=top,
        tags=tags,
        depth=depth,
        max_players=max_players,
        refresh_hours=refresh_hours,
        location=location,
    )

    async def run() -> dict[str, object]:
        async with ClashClient(token, s.api_base, concurrency=concurrency) as c:
            with db.session(s.db_path) as conn:
                log.info("synced %d cards", await sync_cards(c, conn))
                stats = await crawl(c, conn, params)
            return {k: getattr(stats, k) for k in stats.__slots__}

    # Only one crawl at a time: they'd contend for the DB write lock and the API rate limit.
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    with (s.db_path.parent / ".crawl.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError, PermissionError:  # PEP 758: no parentheses needed
            typer.echo("Another `crai crawl` is already running; wait for it to finish.", err=True)
            raise typer.Exit(1) from None
        _json(asyncio.run(run()))
    # Pre-pay the analytics snapshot so the next question doesn't have to.
    log.info("snapshot: %d rows appended", snapshot.sync(s.db_path))


@app.command("snapshot")
def snapshot_cmd(
    rebuild: Annotated[bool, typer.Option(help="Delete and rebuild from scratch")] = False,
) -> None:
    """Update the Parquet snapshot that analytics reads (normally automatic)."""
    path = get_settings().db_path
    if rebuild:
        snapshot.invalidate(path)
    added = snapshot.sync(path)
    print(f"appended {added} rows; covers battles up to rowid {snapshot.covered_rowid(path)}")


@app.command()
def overview() -> None:
    """What's in the database."""
    _json(_analyzer().overview())


@app.command()
def card(query: str) -> None:
    """Look up a card by (fuzzy) name."""
    a = _analyzer()
    _show(a.cards().filter(pl.col("card_id") == a.resolve_card(query)), 1)


@app.command()
def stats(
    sort: CardSort = "use_rate",
    min_games: int = 30,
    limit: int = 40,
    type: TypesOpt = None,
    since: SinceOpt = None,
    top_only: TopOpt = False,
) -> None:
    """Card usage and win rates."""
    f = Filters.of(type, since)
    _show(_analyzer().card_stats(f, top_only=top_only, min_games=min_games, sort_by=sort), limit)


@app.command()
def pairs(
    card: str,
    sort: PairSort = "games",
    min_games: int = 20,
    limit: int = 25,
    type: TypesOpt = None,
    since: SinceOpt = None,
    top_only: TopOpt = False,
) -> None:
    """Which cards pair with CARD."""
    summary, df = _analyzer().card_pairs(
        card, Filters.of(type, since), top_only=top_only, min_games=min_games, sort_by=sort
    )
    _json(summary)
    _show(df, limit)


@app.command()
def counters(
    card: str,
    favorable: Annotated[bool, typer.Option(help="Show best matchups instead")] = False,
    min_games: int = 20,
    limit: int = 25,
    type: TypesOpt = None,
    since: SinceOpt = None,
    top_only: TopOpt = False,
) -> None:
    """Opponent cards that beat (or lose to) decks with CARD."""
    summary, df = _analyzer().card_matchups(
        card,
        Filters.of(type, since),
        top_only=top_only,
        min_games=min_games,
        mode="favorable" if favorable else "counters",
    )
    _json(summary)
    _show(df, limit)


@app.command()
def decks(
    with_: Annotated[list[str] | None, typer.Option("--with", help="Must contain card")] = None,
    without: Annotated[list[str] | None, typer.Option(help="Must not contain card")] = None,
    sort: DeckSort = "adj_win_rate",
    min_games: int = 10,
    limit: int = 15,
    type: TypesOpt = None,
    since: SinceOpt = None,
    top_only: TopOpt = False,
) -> None:
    """Best exact decks."""
    rows = _analyzer().top_decks(
        Filters.of(type, since),
        include=with_ or [],
        exclude=without or [],
        top_only=top_only,
        min_games=min_games,
        sort_by=sort,
        limit=limit,
    )
    for r in rows:
        print(
            f"{r['adj_win_rate']:.3f} adj | {r['win_rate']:.3f} wr | {r['games']:>5} games | "
            f"{r['avg_elixir']} elx | {', '.join(r['cards'])}"
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
            cards, Filters.of(type, since), top_only=top_only, min_games=min_games
        )
    )


@app.command()
def player(tag: TagArg = None) -> None:
    """Player profile, record and recent battles."""
    _json(_analyzer().player_profile(_my_tag(tag)))


@app.command("my-decks")
def my_decks(
    tag: TagArg = None,
    min_games: int = 15,
    min_level: Annotated[int | None, typer.Option(help="Only cards at/above this level")] = None,
    type: TypesOpt = None,
    since: SinceOpt = None,
    top_only: TopOpt = False,
) -> None:
    """Proven decks you can build from your collection."""
    _json(
        _analyzer().decks_for_player(
            _my_tag(tag),
            Filters.of(type, since),
            top_only=top_only,
            min_games=min_games,
            min_level=min_level,
        )
    )


@app.command()
def sql(
    query: str,
    limit: int = 100,
    snapshot: Annotated[
        bool,
        typer.Option(
            "--snapshot",
            help="Query the Parquet snapshot with polars SQL (tables: decks, cards): fast",
        ),
    ] = False,
) -> None:
    """Run a read-only SQL query against SQLite, or the Parquet snapshot with --snapshot."""
    a = _analyzer()
    _show(a.snapshot_query(query) if snapshot else a.query(query), limit)


@app.command()
def schema(
    table: Annotated[str | None, typer.Argument(help="Show one table plus sample rows")] = None,
    snapshot: Annotated[
        bool, typer.Option("--snapshot", help="Describe the Parquet snapshot instead")
    ] = False,
    rows: Annotated[int, typer.Option(help="Sample rows to show with a table")] = 5,
) -> None:
    """Show the database schema, or one table with sample rows."""
    a = _analyzer()
    if snapshot:
        if table and table not in a.SNAPSHOT_TABLES:
            raise typer.BadParameter(f"Snapshot tables: {', '.join(a.SNAPSHOT_TABLES)}")
        names = [table] if table else list(a.SNAPSHOT_TABLES)
        for name in names:
            print(f"-- {name}: {a.SNAPSHOT_TABLES.get(name, '?')}")
            _show(a.snapshot_query(f"SELECT * FROM {name} LIMIT {rows}"), rows)
        return
    if table is None:
        print(a.schema())
        return
    if (ddl := a.table_sql(table)) is None:
        raise typer.BadParameter(f"No table named {table!r}. Run `crai schema` to list them.")
    print(ddl)
    _show(a.query(f"SELECT * FROM {table} LIMIT ?", (rows,)), rows)


@app.command()
def mcp(
    transport: Annotated[str, typer.Option(help="stdio | streamable-http")] = "stdio",
    port: int = 8765,
) -> None:
    """Run the MCP server (stdio by default)."""
    run_mcp(transport, port)


def main() -> None:
    sys.exit(app())
