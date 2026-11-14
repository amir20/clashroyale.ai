"""Train a win-probability model on exported battles and score it on battles it never saw.

- by day (default): the newest full day is the test, the day before chooses when to stop, so
  the scores say how well the model predicts tomorrow, as it will be used.
- by player: some players are held out entirely, and only battles between two of them are
  tested. A model that scores well only by recognizing strong players (from their exact decks)
  does worse here; one that learned how decks match up does about as well.
"""

import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl
import torch
from torch import Tensor, nn

from crai.ml.data import FORMS, strength
from crai.ml.model import Baseline, DeckNet, Strength

log = logging.getLogger(__name__)


@dataclass
class Split:
    a: Tensor
    b: Tensor
    ctx: Tensor
    x: Tensor  # (n, 2): A's average card level minus B's, trophy gap in thousands (0 if unknown)
    y: Tensor

    def __len__(self) -> int:
        return len(self.y)


@dataclass
class Scores:
    log_loss: float  # lower is better; always guessing 50% scores 0.693
    accuracy: float
    auc: float  # chance the model ranks a random win above a random loss; 0.5 = no skill
    calibration: float  # mean |predicted - actual| over 10 bins; 0 = "60%" means 60%


def device() -> torch.device:
    return torch.device("mps" if torch.backends.mps.is_available() else "cpu")


HELD_OUT = 0.15  # by player: share of players held out for the test, and again for validation


def _by_day(df: pl.DataFrame) -> dict[str, pl.Expr]:
    per_day = df.group_by("day").len().sort("day")
    # A day still being crawled has a fraction of the battles: test on full days only.
    full = per_day.filter(pl.col("len") >= pl.col("len").max() / 4)["day"].to_list()
    test_day, val_day = full[-1], full[-2]
    day = pl.col("day")
    return {"train": day < val_day, "val": day == val_day, "test": day == test_day}


def _by_player(df: pl.DataFrame) -> dict[str, pl.Expr]:
    def group(tag: str) -> pl.Expr:  # 0 = train, 1 = validation, 2 = test; stable per player
        h = pl.col(tag).hash(seed=7) % 1000 / 1000
        return pl.when(h < HELD_OUT).then(2).when(h < 2 * HELD_OUT).then(1).otherwise(0)

    ga, gb = group("pa"), group("pb")
    # A battle trains only if neither player is held out, and tests only if both are.
    return {
        "train": (ga == 0) & (gb == 0),
        "val": (ga == 1) & (gb == 1),
        "test": (ga == 2) & (gb == 2),
    }


def load(path: Path, by: str = "day") -> tuple[dict[str, Split], int]:
    df = pl.read_parquet(path)
    vocab = json.loads(path.with_suffix(".vocab.json").read_text())
    vocab_size = max(vocab.values()) + FORMS
    parts = {"day": _by_day, "player": _by_player}[by](df)

    def split(part: pl.DataFrame) -> Split:
        def side(col: str) -> Tensor:
            return torch.from_numpy(part[col].list.to_array(8).to_numpy().astype(np.int16))

        return Split(
            side("a"),
            side("b"),
            torch.from_numpy(part["ctx"].to_numpy().astype(np.int16)),
            torch.from_numpy(part.select(strength()).to_numpy().astype(np.float32)),
            torch.from_numpy(part["y"].to_numpy().astype(np.float32)),
        )

    splits = {k: split(df.filter(f)) for k, f in parts.items()}
    log.info("battles: %s", {k: len(v) for k, v in splits.items()})
    return splits, vocab_size


@torch.no_grad()
def predict(model: nn.Module, s: Split, dev: torch.device, batch: int = 65536) -> Tensor:
    model.eval()
    out = []
    for i in range(0, len(s), batch):
        sl = slice(i, i + batch)
        logit = model(
            s.a[sl].to(dev).long(),
            s.b[sl].to(dev).long(),
            s.ctx[sl].to(dev).long(),
            s.x[sl].to(dev),
        )
        out.append(torch.sigmoid(logit).float().cpu())
    return torch.cat(out)


def score(p: Tensor, y: Tensor) -> Scores:
    p = p.clamp(1e-6, 1 - 1e-6)
    log_loss = -(y * p.log() + (1 - y) * (1 - p).log()).mean().item()
    accuracy = ((p > 0.5).float() == y).float().mean().item()
    order = p.argsort()
    ranks = torch.empty_like(p)
    ranks[order] = torch.arange(1, len(p) + 1, dtype=p.dtype)
    pos = y.sum().item()
    both = pos * (len(y) - pos)  # 0 when every game went one way: no ranking to judge
    auc = (ranks[y == 1].sum().item() - pos * (pos + 1) / 2) / both if both else 0.5
    bins = (p * 10).long().clamp(max=9)
    gaps = [
        (p[bins == k].mean() - y[bins == k].mean()).abs().item() * (bins == k).sum().item()
        for k in range(10)
        if (bins == k).any()
    ]
    return Scores(log_loss, accuracy, auc, sum(gaps) / len(p))


def train(
    data: Path,
    out: Path,
    kind: str = "decknet",
    epochs: int = 4,
    batch: int = 4096,
    lr: float = 2e-3,
    dim: int = 64,
    by: str = "day",
) -> dict[str, Scores]:
    dev = device()
    splits, vocab_size = load(data, by)
    model: nn.Module = {
        "decknet": lambda: DeckNet(vocab_size, dim=dim),
        "baseline": lambda: Baseline(vocab_size),
        "strength": Strength,
    }[kind]()
    model.to(dev)
    params = sum(p.numel() for p in model.parameters())
    log.info("%s: %s parameters on %s", kind, f"{params:,}", dev)
    opt = torch.optim.AdamW(
        model.parameters(), lr=lr, weight_decay=1e-4 if kind == "decknet" else 0
    )
    tr = splits["train"]
    steps = math.ceil(len(tr) / batch) * epochs
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=lr, total_steps=steps)
    loss_fn = nn.BCEWithLogitsLoss()
    best, best_state = math.inf, None
    for epoch in range(epochs):
        model.train()
        started, perm = time.perf_counter(), torch.randperm(len(tr))
        for i in range(0, len(tr), batch):
            idx = perm[i : i + batch]
            a, b = tr.a[idx].to(dev).long(), tr.b[idx].to(dev).long()
            x, y = tr.x[idx], tr.y[idx]
            if torch.rand(()) < 0.5:  # either side first: the model must not care
                a, b, x, y = b, a, -x, 1 - y
            loss = loss_fn(model(a, b, tr.ctx[idx].to(dev).long(), x.to(dev)), y.to(dev))
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            sched.step()
        val = score(predict(model, splits["val"], dev), splits["val"].y)
        log.info(
            "epoch %d: val log loss %.4f, auc %.4f (%.0fs)",
            epoch + 1,
            val.log_loss,
            val.auc,
            time.perf_counter() - started,
        )
        if val.log_loss < best:
            best = val.log_loss
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
    assert best_state is not None
    model.load_state_dict(best_state)
    result = {k: score(predict(model, splits[k], dev), splits[k].y) for k in ("val", "test")}
    out.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {"kind": kind, "vocab_size": vocab_size, "dim": dim, "state": model.state_dict()}, out
    )
    return result
