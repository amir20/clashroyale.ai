"""`crai` command line: crawl data, then ask questions of it.

Output is rendered for humans (tables, panels, progress bars). Pass `--json` before the command
(`crai --json pairs hog`) for machine-readable output instead.
"""

import asyncio
import fcntl
import json
import logging
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import closing
from typing import Annotated, Any

import typer

# Lazy (PEP 810): `crai crawl` never loads polars, `crai stats` never loads httpx.
lazy import polars as pl
lazy from rich.logging import RichHandler

from .config import Settings, get_settings
from .schema import is_legacy
lazy from . import db, snapshot, ui
lazy from .analytics import Analyzer, CardSort, DeckSort, Filters, PairSort
lazy from .api import ClashClient
lazy from .crawl import CrawlParams, crawl, sync_cards
lazy from .mcp_server import run as run_mcp
lazy from .upgrade import upgrade

app = typer.Typer(no_args_is_help=True, help="Clash Royale crawler, analytics and MCP server.")
log = logging.getLogger("crai")
_state = {"json": False}

# typer inspects these at runtime, so they are plain aliases rather than PEP 695 `type`.

TypesOpt = Annotated[
    list[str] | None, typer.Option("--type", help="Battle type(s), e.g. pathOfLegend, trail")
]
SinceOpt = Annotated[
    int | None, typer.Option(help="Only battles from the last N days (default 7; 0 = all time)")
]
TopOpt = Annotated[bool, typer.Option("--top-only", help="Only decks played by seed (top) players")]
TagArg = Annotated[str | None, typer.Argument(help="Player tag; defaults to CR_MY_TAG")]


def _analyzer() -> Analyzer:
    return Analyzer(get_settings().db_path)


def _emit(data: Any, render: Callable[[], None]) -> None:
    """JSON when --json was given, otherwise the pretty rendering."""
    if _state["json"]:
        if isinstance(data, pl.DataFrame):
            data = data.to_dicts()
        print(json.dumps(data, indent=2, default=str))
    else:
        render()


def _require_tokens(s: Settings) -> tuple[str, ...]:
    if not s.api_tokens:
        raise typer.BadParameter("CR_API_TOKEN is not set (comma-separated keys, in .env).")
    return s.api_tokens


def _my_tag(tag: str | None) -> str:
    if t := tag or get_settings().my_tag:
        return t
    raise typer.BadParameter("Pass a tag or set CR_MY_TAG.")


@app.callback()
def _main(
    ctx: typer.Context,
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Debug logging")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON output")] = False,
) -> None:
    _state["json"] = as_json
    handler: logging.Handler = (
        logging.StreamHandler(sys.stderr)
        if as_json
        else RichHandler(console=ui.console, show_path=False, log_time_format="%H:%M:%S")
    )
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[handler],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if ctx.invoked_subcommand not in {"upgrade-db", "mcp"}:
        _refuse_legacy()


def _refuse_legacy() -> None:
    path = get_settings().db_path
    if not path.exists():
        return
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        legacy = is_legacy(conn)
    if legacy:
        ui.console.print(
            f"[yellow]{path} uses the old (v2) layout, which makes crawls slow.[/]\n"
            "Convert it once with [bold]uv run crai upgrade-db[/] (your data is kept, and the "
            "original is saved as a backup)."
        )
        raise typer.Exit(1)


# ---------------------------------------------------------------- data collection


@app.command("sync-cards")
def sync_cards_cmd() -> None:
    """Fetch the full card list into the database."""
    s = get_settings()

    async def run() -> int:
        async with ClashClient(_require_tokens(s), s.api_base) as c:
            with db.session(s.db_path) as conn:
                return await sync_cards(c, conn)

    n = asyncio.run(run())
    _emit({"cards": n}, lambda: ui.console.print(f"[green]✓[/] synced {n} cards"))


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
    concurrency: Annotated[int, typer.Option(help="Parallel requests per API key")] = 8,
) -> None:
    """Crawl seeds -> opponents -> opponents' opponents. Safe to re-run any time."""
    s = get_settings()
    tokens = _require_tokens(s)
    tags = [*(tag or []), *([s.my_tag] if s.my_tag else [])]
    params = CrawlParams(
        top=top,
        tags=tags,
        depth=depth,
        max_players=max_players,
        refresh_hours=refresh_hours,
        location=location,
    )
    live = not _state["json"] and ui.console.is_terminal

    async def run() -> dict[str, Any]:
        async with ClashClient(tokens, s.api_base, concurrency=concurrency) as c:
            with db.session(s.db_path) as conn:
                n_cards = await sync_cards(c, conn)
                log.info(
                    "synced %d cards · %d API key(s) · %d parallel each",
                    n_cards,
                    len(tokens),
                    concurrency,
                )
                if live:
                    with ui.CrawlProgress() as progress:
                        stats = await crawl(c, conn, params, progress)
                else:
                    stats = await crawl(c, conn, params)
                db.checkpoint(conn)  # fold the WAL back in so it doesn't grow across crawls
            summary = {k: getattr(stats, k) for k in stats.__slots__}
            return summary | {
                "requests": c.requests,
                "throttled_429": c.throttled,
                "keys": c.key_stats(),
            }

    # Only one crawl at a time: they'd contend for the DB write lock and the API rate limit.
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    with (s.db_path.parent / ".crawl.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError, PermissionError:  # PEP 758: no parentheses needed
            typer.echo("Another `crai crawl` is already running; wait for it to finish.", err=True)
            raise typer.Exit(1) from None
        started = time.monotonic()
        summary = asyncio.run(run())
        elapsed = time.monotonic() - started
    # Pre-pay the analytics snapshot so the next question doesn't have to.
    with ui.console.status("updating analytics snapshot…") if live else _nullcontext():
        appended = snapshot.sync(s.db_path)
    summary["snapshot_rows_appended"] = appended
    _emit(summary, lambda: ui.show(ui.crawl_summary(summary, elapsed)))


class _nullcontext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


@app.command("upgrade-db")
def upgrade_db() -> None:
    """Convert an old (v2) database to the fast v3 layout. Keeps the original as a backup."""
    s = get_settings()
    with (s.db_path.parent / ".crawl.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError, PermissionError:
            typer.echo("A crawl is running; stop it before upgrading.", err=True)
            raise typer.Exit(1) from None
        started = time.monotonic()
        with ui.console.status("starting…") as status:
            report = upgrade(s.db_path, step=lambda msg: status.update(f"{msg}…"))
        with ui.console.status("rebuilding analytics snapshot…"):
            snap_rows = snapshot.sync(s.db_path)
    total = time.monotonic() - started
    gb = 1e9
    _emit(
        {
            "counts": report.counts,
            "seconds": report.seconds,
            "old_bytes": report.old_bytes,
            "new_bytes": report.new_bytes,
            "backup": str(report.backup_path),
        },
        lambda: ui.show(
            ui.kv(
                {
                    "before": f"{report.old_bytes / gb:.2f} GB",
                    "after": f"{report.new_bytes / gb:.2f} GB",
                    "total time": f"{total / 60:.1f} min",
                    "snapshot rows": snap_rows,
                    "backup": str(report.backup_path),
                },
                "database upgraded to v3",
                columns=1,
            ),
            ui.table(
                [{"table": k, "rows": v} for k, v in report.counts.items()], "rows (verified)"
            ),
            ui.table(
                [{"step": k, "seconds": round(v, 1)} for k, v in report.seconds.items()], "timing"
            ),
        ),
    )


@app.command("snapshot")
def snapshot_cmd(
    rebuild: Annotated[bool, typer.Option(help="Delete and rebuild from scratch")] = False,
) -> None:
    """Update the Parquet snapshot that analytics reads (normally automatic)."""
    path = get_settings().db_path
    if rebuild:
        snapshot.invalidate(path)
    with ui.console.status("syncing snapshot…"):
        added = snapshot.sync(path)
    covered = snapshot.covered_rowid(path)
    _emit(
        {"appended": added, "covered_rowid": covered},
        lambda: ui.console.print(
            f"[green]✓[/] appended {added:,} rows · covers battles up to rowid {covered:,}"
        ),
    )


# ---------------------------------------------------------------- questions


@app.command()
def overview() -> None:
    """What's in the database."""
    o = _analyzer().overview()

    def render() -> None:
        counts: dict[str, Any] = dict(o["counts"])
        counts["analyzable 1v1 decks"] = o["analyzable_1v1_decks"]
        span = o["battle_time_span"] or {}
        counts["battles from"] = span.get("first")
        counts["battles to"] = span.get("last")
        ui.show(
            ui.kv(counts, f"database · {o['db_path']}"),
            ui.table(o["battle_types"], "battle types"),
            ui.table(
                [
                    {
                        k: r[k]
                        for k in (
                            "id",
                            "started_at",
                            "players_fetched",
                            "battles_new",
                            "requests",
                            "status",
                        )
                    }
                    for r in o["recent_crawls"]
                ],
                "recent crawls",
            ),
        )

    _emit(o, render)


@app.command()
def card(query: str) -> None:
    """Look up a card by (fuzzy) name."""
    a = _analyzer()
    row = a.cards().filter(pl.col("card_id") == a.resolve_card(query))
    _emit(row, lambda: ui.show(ui.kv(row.to_dicts()[0], "card")))


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
    df = _analyzer().card_stats(f, top_only=top_only, min_games=min_games, sort_by=sort)
    _emit(df.head(limit), lambda: ui.show(ui.table(df, f"cards by {sort}", limit=limit)))


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
    _emit(
        {"summary": summary, "partners": df.head(limit).to_dicts()},
        lambda: ui.show(
            ui.kv(summary, f"{summary['card']}"),
            ui.table(df, f"cards paired with {summary['card']} · by {sort}", limit=limit),
        ),
    )


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
    what = "best matchups" if favorable else "counters"
    _emit(
        {"summary": summary, "opponent_cards": df.head(limit).to_dicts()},
        lambda: ui.show(
            ui.kv({k: v for k, v in summary.items() if k != "note"}, summary["card"]),
            ui.table(
                df,
                f"{what} for {summary['card']} · win rate of the {summary['card']} deck",
                limit=limit,
            ),
        ),
    )


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
    _emit(rows, lambda: ui.show(ui.table(rows, f"top decks · by {sort}")))


@app.command()
def complete(
    cards: list[str],
    min_games: int = 10,
    type: TypesOpt = None,
    since: SinceOpt = None,
    top_only: TopOpt = False,
) -> None:
    """Suggest cards to complete a partial deck."""
    r = _analyzer().complete_deck(
        cards, Filters.of(type, since), top_only=top_only, min_games=min_games
    )
    _emit(
        r,
        lambda: ui.show(
            ui.kv(
                {
                    "given": r["given"],
                    "matching decks": r["matching_decks"],
                    "win rate": r["win_rate_with_given"],
                },
                "partial deck",
                columns=1,
            ),
            ui.table(r["suggested_cards"], "cards winning decks add"),
            ui.table(r["full_decks"], "proven full decks"),
        ),
    )


@app.command()
def player(tag: TagArg = None) -> None:
    """Player profile, record and recent battles."""
    p = _analyzer().player_profile(_my_tag(tag))

    def render() -> None:
        prof = p["profile"]
        keep = (
            "name", "tag", "trophies", "best_trophies", "wins", "losses", "clan_name",
            "arena", "pol_league", "best_pol_league", "battlelog_fetched_at",
        )  # fmt: skip
        ui.show(
            ui.kv({k: prof.get(k) for k in keep}, "player"),
            ui.table(p["current_deck"], "current deck"),
            ui.table(p["record_by_battle_type"], "record by mode"),
            ui.table(p["most_used_decks"], "most used decks"),
            ui.table(
                [
                    {
                        "time": b["battle_time"],
                        "mode": b["battle_type"],
                        "result": b["result"],
                        "score": f"{b['crowns']}-{b['opponent_crowns']}",
                        "trophies": b["trophy_change"],
                        "opponent": b["opponent"],
                        "opponent deck": b["opponent_deck"],
                    }
                    for b in p["recent_battles"]
                ],
                "recent battles",
            ),
        )

    _emit(p, render)


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
    rows = _analyzer().decks_for_player(
        _my_tag(tag),
        Filters.of(type, since),
        top_only=top_only,
        min_games=min_games,
        min_level=min_level,
    )
    _emit(rows, lambda: ui.show(ui.table(rows, "proven decks you can build")))


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
    df = a.snapshot_query(query) if snapshot else a.query(query)
    note = f"{df.height:,} rows" + (f" (showing {limit})" if df.height > limit else "")
    _emit(df.head(limit), lambda: ui.show(ui.table(df, note, limit=limit, hide=())))


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
        samples = {n: a.snapshot_query(f"SELECT * FROM {n} LIMIT {rows}") for n in names}
        _emit(
            {
                "schema": a.snapshot_schema(),
                "samples": {n: d.to_dicts() for n, d in samples.items()},
            },
            lambda: ui.show(
                *(ui.table(d, f"{n} · {a.SNAPSHOT_TABLES[n]}", hide=()) for n, d in samples.items())
            ),
        )
        return
    if table is None:
        _emit({"schema": a.schema()}, lambda: ui.show(ui.sql(a.schema())))
        return
    if (ddl := a.table_sql(table)) is None:
        raise typer.BadParameter(f"No table named {table!r}. Run `crai schema` to list them.")
    sample = a.query(f"SELECT * FROM {table} LIMIT ?", (rows,))
    _emit(
        {"ddl": ddl, "sample": sample.to_dicts()},
        lambda: ui.show(ui.sql(ddl), ui.table(sample, f"sample rows from {table}", hide=())),
    )


@app.command()
def mcp(
    transport: Annotated[str, typer.Option(help="stdio | streamable-http")] = "stdio",
    port: int = 8765,
) -> None:
    """Run the MCP server (stdio by default)."""
    run_mcp(transport, port)


def main() -> None:
    sys.exit(app())
