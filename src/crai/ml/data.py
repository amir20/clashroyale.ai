"""Training data for the deck models: one row per battle, both decks as tokens.

A token is a card as it was played: every card has four (plain, Evolution, Hero, both), so
`Evo Knight` and `Knight` are different words. Order inside a deck doesn't matter (apart from
the Evolution/Hero slots, which the token already says), so a deck is a set of 8 tokens.

This module needs only polars, so the export runs where the data lives (the NAS image has no
torch); training reads the Parquet it writes.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

lazy import polars as pl

from crai.analysis.analytics import EXCLUDED_TYPES, variants
from crai.storage import snapshot

FORMS = 4  # plain, Evolution, Hero, both: evolution_level 0-3
PAD = 0  # token 0: a card the vocabulary doesn't know (a new card)
MODES = {"trail": 1, "pathOfLegend": 2}  # everything else is 0
BUCKET_TROPHIES = 1000
BUCKETS = 16  # per mode; the last one holds everything above


def vocabulary(db_path: Path) -> dict[str, int]:
    """Card id -> its first token. A card's forms follow it: plain, Evo, Hero, both."""
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM cards WHERE is_support = 0 ORDER BY id")]
    return {str(cid): 1 + i * FORMS for i, cid in enumerate(ids)}


def tokens(vocab: dict[str, int]) -> pl.Expr:
    """The deck's 8 tokens, sorted, from its cards and their Evolution/Hero forms."""
    first = {int(k): v for k, v in vocab.items()}
    v = variants()
    unknown = -100  # stays negative whatever the form adds, then becomes PAD
    token = (pl.element() // 10).replace_strict(
        first, default=unknown, return_dtype=pl.Int32
    ) + pl.element() % 10
    return (
        v.list.eval(pl.when(token < 0).then(PAD).otherwise(token))
        .list.sort()
        .cast(pl.List(pl.Int16))
    )


def context() -> pl.Expr:
    """Mode and trophy bucket of a battle as one token: 0 = unknown."""
    mode = pl.col("battle_type").cast(pl.String).replace_strict(MODES, default=0)
    bucket = (pl.col("starting_trophies") // BUCKET_TROPHIES).clip(0, BUCKETS - 2) + 1
    return (mode * BUCKETS + bucket.fill_null(0)).cast(pl.Int16)


def export(db_path: Path, out: Path, days: int = 8) -> int:
    """Write battles from the last `days` days to `out` (Parquet) and the vocabulary next to it.

    Columns: day, ctx (mode + trophy bucket of side A), a and b (8 tokens each), y (1.0 if A
    won, 0.0 if B won; draws are dropped). Returns the number of battles written.
    """
    vocab = vocabulary(db_path)

    def shape(scan: pl.LazyFrame) -> pl.LazyFrame:  # while reading: tens of millions of rows
        return scan.filter(
            ~pl.col("battle_type").cast(pl.String).fill_null("").is_in(EXCLUDED_TYPES),
            pl.col("deck_selection").cast(pl.String).fill_null("collection") == "collection",
        ).select(
            "battle_id",
            "side",
            "score",
            pl.col("battle_time").str.slice(0, 10).alias("day"),
            ctx=context(),
            t=tokens(vocab),
        )

    _, decks = snapshot.read(db_path, days=days, transform=shape, sync=False)
    a = decks.filter(pl.col("side") == 0)
    b = decks.filter(pl.col("side") == 1).select("battle_id", pl.col("t").alias("b"))
    battles = (
        a.join(b, on="battle_id")
        .filter(pl.col("score") != 0.5)
        .select("day", "ctx", pl.col("t").alias("a"), "b", y=pl.col("score").cast(pl.Float32))
        .sort("day")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    battles.write_parquet(out, compression="zstd")
    out.with_suffix(".vocab.json").write_text(json.dumps(vocab))
    return battles.height
