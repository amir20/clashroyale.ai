"""`crai ml ...`: export battles for the deck models, and train them (needs the `ml` group)."""

from pathlib import Path
from typing import Annotated

import typer

from crai.cli.app import app
from crai.config import get_settings
lazy from crai.ml.data import export  # polars only
lazy from crai.ml.train import train  # needs the `ml` group (torch)

ml = typer.Typer(no_args_is_help=True, help="Deck models: export training data and train.")
app.add_typer(ml, name="ml")

DataOpt = Annotated[Path, typer.Option(help="Exported battles (Parquet)")]


@ml.command("export")
def export_cmd(
    out: DataOpt = Path("data/ml/battles.parquet"),
    days: Annotated[int, typer.Option(help="Battles from the last N days")] = 8,
) -> None:
    """One row per battle: both decks as tokens, mode and trophy bucket, who won."""
    n = export(get_settings().db_path, out, days)
    typer.echo(f"wrote {n:,} battles to {out}")


@ml.command("train")
def train_cmd(
    data: DataOpt = Path("data/ml/battles.parquet"),
    kind: Annotated[
        str, typer.Option(help="decknet, baseline, or strength (levels and trophies only)")
    ] = "decknet",
    epochs: int = 4,
    batch: int = 4096,
    lr: float = 2e-3,
    dim: int = 64,
    by: Annotated[
        str, typer.Option(help="Hold out the newest day, or players never seen in training")
    ] = "day",
) -> None:
    """Train, then report scores on held-out battles (the newest day, or unseen players)."""
    out = data.with_name(f"{kind}.pt" if by == "day" else f"{kind}-{by}.pt")
    for split, s in train(data, out, kind, epochs, batch, lr, dim, by).items():
        typer.echo(
            f"{split}: log loss {s.log_loss:.4f} (coin flip 0.6931), accuracy {s.accuracy:.3f}, "
            f"AUC {s.auc:.4f}, calibration error {s.calibration:.4f}"
        )
    typer.echo(f"saved {out}")
