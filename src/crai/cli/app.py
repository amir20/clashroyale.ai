"""The Typer app and helpers shared by every command."""

import json
import logging
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from typing import Annotated, Any

import typer

# Lazy (PEP 810): `crai crawl` never loads polars, `crai stats` never loads httpx.
lazy import polars as pl
lazy from rich.logging import RichHandler

from crai.config import Settings, get_settings
from crai.storage.schema import is_legacy
lazy from crai.analysis.analytics import Analyzer
lazy from crai.cli import ui

app = typer.Typer(no_args_is_help=True, help="Clash Royale crawler, analytics and MCP server.")
log = logging.getLogger("crai")
_state = {"json": False}
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
