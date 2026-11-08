"""Terminal rendering for the CLI: tables, panels and crawl progress (rich).

Formatting rules, by column name:
  *_rate / win_rate / pair_rate  -> percentage ("52.4%")
  margin                         -> "±5.5" (points)
  synergy / delta / gain         -> signed points, green when positive, red when negative
  result                         -> win (green) / loss (red) / draw
  lists                          -> comma-joined
"""

import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
)
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
lazy import polars as pl

console = Console()

PERCENT_COLS = frozenset({"win_rate", "adj_win_rate", "use_rate", "pair_rate", "overall_rate"})
SIGNED_COLS = frozenset({"synergy", "delta", "gain"})
NUMERIC_JUSTIFY = "right"


def _fmt(col: str, v: Any) -> Text | str:
    match v:
        case None:
            return Text("—", style="dim")
        case list() | tuple():
            return ", ".join(map(str, v))
        case bool():
            return "yes" if v else "no"
        case float() if col in PERCENT_COLS or col.endswith("_rate"):
            return f"{v * 100:.1f}%"
        case float() if col == "margin":
            return f"±{v * 100:.1f}"
        case float() if col in SIGNED_COLS:
            pts = v * 100
            style = "green" if pts > 0.05 else "red" if pts < -0.05 else ""
            return Text(f"{pts:+.1f}", style=style)
        case float():
            return f"{v:,.2f}" if abs(v) < 1000 else f"{v:,.0f}"
        case int() if col not in {"card_id", "rank", "seed_rank"} and abs(v) >= 1000:
            return f"{v:,}"
        case str() if col == "result":
            return Text(v, style={"win": "green", "loss": "red"}.get(v, "yellow"))
        case _:
            return str(v)


def _header(col: str) -> str:
    return {"adj_win_rate": "adj win", "win_rate": "win", "pair_rate": "paired"}.get(
        col, col.replace("_", " ")
    )


def table(
    rows: Sequence[Mapping[str, Any]] | pl.DataFrame,
    title: str | None = None,
    *,
    limit: int | None = None,
    hide: Iterable[str] = ("card_id", "card_ids", "is_support"),
) -> Table:
    if isinstance(rows, pl.DataFrame):
        rows = rows.head(limit).to_dicts() if limit else rows.to_dicts()
    elif limit:
        rows = rows[:limit]
    hidden = set(hide)
    cols = [c for c in (rows[0] if rows else {}) if c not in hidden]
    t = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold cyan", title_justify="left")
    for c in cols:
        sample = next((r[c] for r in rows if r[c] is not None), None)
        numeric = isinstance(sample, int | float) and not isinstance(sample, bool)
        t.add_column(
            _header(c),
            justify="right" if numeric else "left",
            overflow="fold",
            ratio=3 if isinstance(sample, list) else None,
        )
    for r in rows:
        t.add_row(*(_fmt(c, r[c]) for c in cols))
    if not rows:
        t.add_row(Text("no rows", style="dim"))
    return t


def kv(data: Mapping[str, Any], title: str | None = None, *, columns: int = 2) -> Panel:
    """A compact key/value panel."""
    grid = Table.grid(padding=(0, 2))
    for _ in range(columns):
        grid.add_column(style="dim", justify="right")
        grid.add_column()
    items = [(k.replace("_", " "), _fmt(k, v)) for k, v in data.items()]
    for i in range(0, len(items), columns):
        chunk = items[i : i + columns]
        cells: list[Text | str] = [x for pair in chunk for x in pair]
        cells += [""] * (2 * columns - len(cells))
        grid.add_row(*cells)
    return Panel(grid, title=title, title_align="left", border_style="cyan", expand=False)


def show(*renderables: Any) -> None:
    for r in renderables:
        console.print(r)


def sql(text: str) -> Syntax:
    return Syntax(text.strip(), "sql", theme="ansi_dark", word_wrap=True)


# ---------------------------------------------------------------- crawl progress

DEPTH_LABEL = frozendict({0: "seeds", 1: "opponents", 2: "2nd hop"})


def _compact(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e4 else f"{n:,}"


class _Totals:
    """The running-totals line under the bars (re-rendered on every refresh)."""

    def __init__(self) -> None:
        self.text = Text("starting…", style="dim")

    def __rich__(self) -> Text:
        return self.text


class CrawlProgress:
    """Live progress for `crawl()`: one compact bar per depth, running totals underneath.

    Totals live on their own line so the bars keep their width in narrow terminals.
    """

    def __init__(self) -> None:
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description:<10}"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TextColumn("[cyan]{task.fields[rate]:>4}/s"),
            TextColumn("[dim]eta[/]"),
            TimeRemainingColumn(compact=True),
            console=console,
            expand=True,
        )
        self._totals = _Totals()
        self._live = Live(Group(self.progress, self._totals), console=console, refresh_per_second=8)
        self._task: TaskID | None = None
        self._started = 0.0
        self._skipped = 0

    def __enter__(self) -> CrawlProgress:
        self._live.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._live.__exit__(None, None, None)

    def depth_started(self, depth: int, to_fetch: int, fresh: int, budget_left: int) -> None:
        self._skipped += fresh
        label = f"depth {depth} {DEPTH_LABEL.get(depth, '')}".strip()
        self._task = self.progress.add_task(label, total=max(to_fetch, 1), rate="—")
        self._started = time.monotonic()
        if to_fetch == 0:
            self.progress.update(self._task, completed=1)

    def player_done(
        self, done: int, battles_new: int, requests: int, throttled: int, failed: int
    ) -> None:
        if self._task is None:
            return
        elapsed = max(time.monotonic() - self._started, 1e-6)
        self.progress.update(self._task, completed=done, rate=f"{done / elapsed:.0f}")
        parts = [
            f"[green]{_compact(battles_new)}[/] new battles",
            f"{_compact(requests)} requests",
            f"{_compact(self._skipped)} skipped (fresh)",
        ]
        if throttled:
            parts.append(f"[yellow]{_compact(throttled)} rate-limited[/]")
        if failed:
            parts.append(f"[red]{failed} failed[/]")
        self._totals.text = Text.from_markup("  " + " · ".join(parts))


def crawl_summary(summary: Mapping[str, Any], seconds: float) -> Group:
    fetched = summary.get("players_fetched", 0)
    head = {
        "players fetched": fetched,
        "skipped (fresh)": summary.get("players_skipped_fresh", 0),
        "failed": summary.get("players_failed", 0),
        "not found": summary.get("players_missing", 0),
        "new battles": summary.get("battles_new", 0),
        "requests": summary.get("requests", 0),
        "429s": summary.get("throttled_429", 0),
        "time": f"{seconds / 60:.1f} min ({fetched / max(seconds, 1e-6):.0f} players/s)",
    }
    parts: list[Any] = [kv(head, "crawl finished")]
    if keys := summary.get("keys"):
        parts.append(table(keys, "API keys"))
    return Group(*parts)
