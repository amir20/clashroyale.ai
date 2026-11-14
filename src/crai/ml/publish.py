"""Turn a trained baseline into `model.json`: plain weights the server reads without torch.

The baseline is a sum of weights, so the file is the whole model: one weight per card as played,
per pair of cards in a deck (synergy), and per (my card, their card) matchup (counters), plus how
much card levels and trophies count. It also records how well the model did on battles it never
saw, for the "how it works" page.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import torch

from crai.ml.model import Baseline
from crai.ml.train import _by_day, load, predict, score


def _table(w: torch.Tensor, vocab_size: int) -> list[list[float]]:
    """Nonzero weights as [token x, token y, weight]. Pairs and matchups never collide in the
    hashed table: vocab_size squared is far below its size."""
    w = w.squeeze(1)
    assert vocab_size * vocab_size <= Baseline.HASH
    idx = w.nonzero().squeeze(1).tolist()
    return [[i // vocab_size, i % vocab_size, round(float(w[i]), 5)] for i in idx]


def publish(checkpoint: Path, data: Path, out: Path) -> dict[str, Any]:
    ckpt = torch.load(checkpoint, map_location="cpu")
    if ckpt["kind"] != "baseline":
        raise ValueError("only the baseline can be explained card by card: train --kind baseline")
    vocab_size, st = ckpt["vocab_size"], ckpt["state"]
    model = Baseline(vocab_size)
    model.load_state_dict(st)
    splits, _ = load(data)
    test, dev = splits["test"], torch.device("cpu")
    strength = st["strength.weight"].squeeze(0)
    full = score(predict(model, test, dev), test.y)
    alone = score(torch.sigmoid(test.x @ strength), test.y)  # the same model, decks ignored
    days = pl.read_parquet(data, columns=["day"])
    test_day = days.filter(_by_day(days)["test"])["day"][0]
    about = {
        "published": datetime.now(UTC).strftime("%Y-%m-%d"),
        "battles": len(splits["train"]),
        "first_day": days["day"].min(),
        "test_day": test_day,
        "test_battles": len(test),
        "accuracy": round(full.accuracy, 4),
        "auc": round(full.auc, 4),
        "calibration": round(full.calibration, 4),
        "strength_accuracy": round(alone.accuracy, 4),
    }
    model_json = {
        "about": about,
        "vocab": json.loads(data.with_suffix(".vocab.json").read_text()),
        "strength": [round(float(w), 5) for w in strength],
        "card": [round(float(w), 5) for w in st["card.weight"].squeeze(1)],
        "pair": _table(st["pair.weight"], vocab_size),
        "vs": _table(st["vs.weight"], vocab_size),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(model_json, separators=(",", ":")))
    return about
