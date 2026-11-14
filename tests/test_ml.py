"""Training data export and the win-probability models."""

import json
from datetime import timedelta
from pathlib import Path

import polars as pl
import pytest
from conftest import ME, START, battle, card_list

from crai.ml.data import FORMS, PAD, export
from crai.storage import database as db
from crai.storage import snapshot


def test_export_writes_one_row_per_battle_with_8_tokens(db_path: Path, tmp_path: Path) -> None:
    snapshot.sync(db_path)
    out = tmp_path / "ml" / "battles.parquet"
    n = export(db_path, out, days=0)
    df = pl.read_parquet(out)
    assert n == df.height == 200
    assert df["a"].list.len().unique().to_list() == [8]
    assert set(df["y"].unique().to_list()) <= {0.0, 1.0}
    vocab = json.loads(out.with_suffix(".vocab.json").read_text())
    known = {v + f for v in vocab.values() for f in range(FORMS)}
    assert set(df["a"].explode().to_list()) <= known  # every card is in the vocabulary
    assert PAD not in df["a"].explode().to_list()
    assert df["ctx"].unique().to_list() == [2 * 16 + 10]  # ranked, 9,000-9,999 trophies


@pytest.fixture
def four_days(tmp_path: Path) -> Path:
    path = tmp_path / "d.db"
    with db.session(path) as conn:
        db.upsert_cards(conn, card_list())
        games = [
            battle(
                START - timedelta(days=d) + timedelta(minutes=i),
                ME,
                f"#{'2' * (i % 5 + 2)}",
                a_wins=i % 3 > 0,
            )
            for d in range(4)
            for i in range(60)
        ]
        db.ingest_battlelog(conn, games)
    snapshot.sync(path)
    return path


@pytest.mark.parametrize("kind", ["baseline", "decknet"])
def test_models_train_and_learn_the_better_deck(four_days: Path, tmp_path: Path, kind: str) -> None:
    pytest.importorskip("torch")
    from crai.ml.train import train  # noqa: PLC0415 - torch is optional

    data = tmp_path / "ml" / "battles.parquet"
    export(four_days, data, days=0)
    scores = train(data, tmp_path / "ml" / f"{kind}.pt", kind, epochs=30, batch=64, lr=0.05)
    # The player's deck wins two in three: the model learns it from the decks alone.
    assert scores["test"].log_loss < 0.66
    assert (tmp_path / "ml" / f"{kind}.pt").exists()
