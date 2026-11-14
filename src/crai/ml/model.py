"""Win-probability models: P(deck A beats deck B | mode, trophy bucket, level and trophy gaps).

The level and trophy gaps are inputs so the deck models don't learn account strength from the
decks (Evolutions come with progress; strong players run particular decks).

Both are antisymmetric by construction: logit(A, B) = s(A, B) - s(B, A), so P(A beats B) is
always 1 - P(B beats A), whichever side a battle lists first.

- Baseline: logistic regression. Every card, every pair of cards in a deck, and every (my card,
  their card) matchup has one learned weight; the score adds them up. No learned features.
- DeckNet: each card is a learned vector (embedding); two attention layers let the 8 cards adjust
  to each other (synergy) with no position information, since order doesn't matter; the mean is
  the deck's vector. A small network compares the two deck vectors in context.
"""

import torch
from torch import Tensor, nn

from crai.ml.data import BUCKETS, PAD

CONTEXTS = 3 * BUCKETS  # mode (0-2) x bucket
STRENGTH = 2  # level gap, trophy gap: see crai.ml.data.strength


class Baseline(nn.Module):
    HASH = 1 << 20  # card pairs and matchups are hashed into this many weights
    i: Tensor  # the 28 card pairs of a deck, as index pairs (buffers)
    j: Tensor

    def __init__(self, vocab_size: int) -> None:
        super().__init__()
        self.vocab_size = vocab_size
        self.card = nn.Embedding(vocab_size, 1)
        self.pair = nn.Embedding(self.HASH, 1)
        self.vs = nn.Embedding(self.HASH, 1)
        for e in (self.card, self.pair, self.vs):
            nn.init.zeros_(e.weight)
        self.strength = nn.Linear(STRENGTH, 1, bias=False)  # odd in x: antisymmetric
        i, j = torch.triu_indices(8, 8, offset=1)
        self.register_buffer("i", i, persistent=False)
        self.register_buffer("j", j, persistent=False)

    def _score(self, a: Tensor, b: Tensor) -> Tensor:
        real = (a != PAD).float()
        cards = (self.card(a).squeeze(-1) * real).sum(1)
        pa, pb = a[:, self.i], a[:, self.j]  # the 28 pairs in deck a
        pairs = self.pair((pa * self.vocab_size + pb) % self.HASH).squeeze(-1).sum(1)
        cross = a.unsqueeze(2) * self.vocab_size + b.unsqueeze(1)  # 64 matchups
        vs = self.vs(cross.flatten(1) % self.HASH).squeeze(-1).sum(1)
        return cards + pairs + vs

    def forward(self, a: Tensor, b: Tensor, ctx: Tensor, x: Tensor) -> Tensor:
        return self._score(a, b) - self._score(b, a) + self.strength(x).squeeze(-1)


class DeckNet(nn.Module):
    def __init__(
        self, vocab_size: int, dim: int = 64, layers: int = 2, heads: int = 4, dropout: float = 0.1
    ) -> None:
        super().__init__()
        self.card = nn.Embedding(vocab_size, dim, padding_idx=PAD)
        block = nn.TransformerEncoderLayer(
            dim, heads, dim_feedforward=dim * 2, dropout=dropout, batch_first=True
        )
        self.mix = nn.TransformerEncoder(block, layers, enable_nested_tensor=False)
        self.ctx = nn.Embedding(CONTEXTS, dim)
        self.head = nn.Sequential(
            nn.Linear(dim * 4 + STRENGTH, dim * 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(dim * 2, 1),
        )

    def deck(self, t: Tensor) -> Tensor:
        """A deck's vector: its cards after attention, averaged (unknown cards left out)."""
        real = t != PAD
        x = self.mix(self.card(t), src_key_padding_mask=~real)
        w = real.unsqueeze(-1).float()
        return (x * w).sum(1) / w.sum(1).clamp(min=1)

    def _score(self, da: Tensor, db: Tensor, c: Tensor, x: Tensor) -> Tensor:
        return self.head(torch.cat([da, db, da * db, c, x], -1)).squeeze(-1)

    def forward(self, a: Tensor, b: Tensor, ctx: Tensor, x: Tensor) -> Tensor:
        da, db, c = self.deck(a), self.deck(b), self.ctx(ctx)
        return self._score(da, db, c, x) - self._score(db, da, c, -x)


class Strength(nn.Module):
    """No decks at all: only card levels and trophies. What the deck models must beat to show
    the deck itself matters."""

    def __init__(self) -> None:
        super().__init__()
        self.f = nn.Sequential(nn.Linear(STRENGTH, 16), nn.GELU(), nn.Linear(16, 1))

    def forward(self, a: Tensor, b: Tensor, ctx: Tensor, x: Tensor) -> Tensor:
        return (self.f(x) - self.f(-x)).squeeze(-1)
