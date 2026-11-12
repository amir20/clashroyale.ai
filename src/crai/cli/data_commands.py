"""Commands that collect and maintain data."""

import asyncio
import fcntl
import time
from typing import Annotated, Any

import typer

from crai.cli.app import (
    _emit,
    _require_tokens,
    _state,
    app,
    log,
)
from crai.config import get_settings
lazy from crai.cli import ui
lazy from crai.collect.client import ClashClient
lazy from crai.collect.crawl import CrawlParams, crawl, sync_cards
lazy from crai.storage import database as db
lazy from crai.storage import snapshot
lazy from crai.storage.upgrade import upgrade

SNAPSHOT_EVERY_SECONDS = 180  # how often a running crawl writes its battles to the snapshot


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
    tracked_refresh_hours: Annotated[
        float, typer.Option(help="Refresh people who use Rook if older than this")
    ] = 2.0,
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
        tracked_refresh_hours=tracked_refresh_hours,
        location=location,
    )
    live = not _state["json"] and ui.console.is_terminal

    async def keep_snapshot_fresh() -> None:
        """Write new battles to the analytics snapshot as the crawl goes, not all at the end,
        so the web server reads them as Parquet instead of converting SQLite rows itself."""
        while True:
            await asyncio.sleep(SNAPSHOT_EVERY_SECONDS)
            try:
                n = await asyncio.to_thread(snapshot.sync, s.db_path)
                log.info("snapshot: appended %d deck rows", n)
            except Exception:
                log.warning("snapshot sync during crawl failed", exc_info=True)

    async def run() -> dict[str, Any]:
        syncing = asyncio.create_task(keep_snapshot_fresh())
        try:
            return await crawl_and_count()
        finally:
            syncing.cancel()

    async def crawl_and_count() -> dict[str, Any]:
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


@app.command()
def prune(days: Annotated[int, typer.Option(help="Delete battles older than this")] = 14) -> None:
    """Delete old battles to keep the database bounded (top players' and your games are kept)."""
    with (
        ui.console.status(f"pruning battles older than {days} days…"),
        db.session(get_settings().db_path) as conn,
    ):
        removed = db.prune_battles(conn, days)
        db.checkpoint(conn)
    _emit(
        {"removed": removed},
        lambda: ui.console.print(f"[green]✓[/] removed {removed:,} old battles"),
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
