"""Commands that answer questions from the data."""

from typing import Annotated, Any

import typer
lazy import polars as pl

from crai.cli.app import (
    SinceOpt,
    TagArg,
    TopOpt,
    TypesOpt,
    _analyzer,
    _emit,
    _my_tag,
    app,
)
lazy from crai.analysis.analytics import CardSort, DeckSort, Filters, PairSort
lazy from crai.cli import ui


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
