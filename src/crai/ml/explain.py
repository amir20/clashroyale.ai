"""The deck model on the server: predicted win chance, and why, from `model.json` (no torch).

The model (see crai.ml.model.Baseline) scores a battle as a sum of weights:

    logit(A beats B) = own(A) - own(B) + sum over a in A, b in B of counter(a, b)
    own(D)           = sum of card weights + sum of the 28 pair weights (synergy)
    counter(a, b)    = vs(a, b) - vs(b, a)

Against "the decks you face" (a sample of decks near the player's trophies, with equal card
levels and trophies), the average logit splits exactly into one share per card:

    share(a) = alone(a) + synergy(a) + matchups(a)
    alone(a)    = card(a) - the average card weight in those decks
    synergy(a)  = half of a's pair weights with the other 7 - the average deck's, per card
    matchups(a) = average over the decks of sum over b of counter(a, b)

The shares add up to the deck's edge, so each card's bar says how much it adds. Shares are in
log-odds; the page shows them as about how many wins per 100 games.
"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

lazy import polars as pl

from crai.analysis import insights
from crai.analysis.analytics import Analyzer, variants
from crai.ml.data import PAD

FIELD_DECKS = 1000  # decks sampled from the player's trophy range: the decks they face
MIN_FIELD_DECKS = 200
MIN_CARD_SHARE = 0.05  # an opponent card shows as a strong/weak matchup if this share run it
SHOWN = 3


@dataclass
class Model:
    first: dict[int, int]  # card id -> its first token
    card: list[float]
    pair: dict[tuple[int, int], float]
    vs: dict[tuple[int, int], float]
    about: dict[str, Any]
    games: list[int] = field(default_factory=list)  # training battles per token
    strength: tuple[float, float] = (0.0, 0.0)  # per level of card-level gap, per 1,000 trophies

    def token(self, v: int) -> int:
        """The token of a card as played (card id * 10 + Evolution/Hero level)."""
        first = self.first.get(v // 10)
        return PAD if first is None else first + v % 10

    def variant(self, t: int) -> int:
        return self._back[t]

    def __post_init__(self) -> None:
        self._back = {f + k: cid * 10 + k for cid, f in self.first.items() for k in range(4)}

    def counter(self, x: int, y: int) -> float:
        return self.vs.get((x, y), 0.0) - self.vs.get((y, x), 0.0)

    def alone(self, deck: list[int]) -> float:
        return sum(self.card[t] for t in deck if t != PAD)

    def pairs(self, deck: list[int]) -> float:
        d = sorted(deck)
        return sum(self.pair.get((x, y), 0.0) for i, x in enumerate(d) for y in d[i + 1 :])

    def own(self, deck: list[int]) -> float:
        return self.alone(deck) + self.pairs(deck)


_cache: dict[Path, tuple[float, Model]] = {}


def load(path: Path) -> Model | None:
    """The published model, reread when the file changes. None when there isn't one."""
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    raw = json.loads(path.read_text())
    model = Model(
        first={int(k): v for k, v in raw["vocab"].items()},
        card=raw["card"],
        pair={(int(x), int(y)): w for x, y, w in raw["pair"]},
        vs={(int(x), int(y)): w for x, y, w in raw["vs"]},
        about=raw["about"],
        games=raw.get("games", []),
        strength=(raw["strength"][0], raw["strength"][1]),
    )
    _cache[path] = (mtime, model)
    return model


@dataclass
class Field:
    """The decks a player faces, as the model sees them."""

    decks: list[list[int]]
    own: list[float]  # each deck's own score
    share: dict[int, float]  # token -> share of decks running it
    card_avg: float  # the average deck's card weights, per card
    pair_avg: float  # the average deck's pair weights, per card
    near: bool
    m: dict[int, float] = field(default_factory=dict)  # token -> average matchups vs the field


def field_for(a: Analyzer, model: Model, items: list[insights.Game]) -> Field | None:
    def build() -> Field | None:
        decks = insights.near_decks(a, items)
        near = decks is not None and decks.height >= MIN_FIELD_DECKS
        if not near:
            decks = a.decks()
        if decks is None or decks.height < MIN_FIELD_DECKS:
            return None
        sample = decks.sample(min(FIELD_DECKS, decks.height), seed=7).select(v=variants())
        found = [[model.token(v) for v in vs] for vs in sample["v"].to_list()]
        found = [d for d in found if len(d) == 8]
        if len(found) < MIN_FIELD_DECKS:
            return None
        share: dict[int, float] = {}
        for d in found:
            for t in set(d):
                share[t] = share.get(t, 0.0) + 1 / len(found)
        return Field(
            decks=found,
            own=[model.own(d) for d in found],
            share=share,
            card_avg=sum(model.alone(d) for d in found) / len(found) / 8,
            pair_avg=sum(model.pairs(d) for d in found) / len(found) / 8,
            near=near,
        )

    return insights._near_stat(a, ("model_field", id(model)), items, build)


def win_chance(model: Model, deck: list[int], f: Field) -> float:
    """Average predicted win chance against the field, with equal levels and trophies."""
    mine = model.own(deck)
    total = 0.0
    for d, own in zip(f.decks, f.own, strict=True):
        logit = mine - own + sum(model.counter(x, y) for x in deck for y in d)
        total += 1 / (1 + math.exp(-logit))
    return total / len(f.decks)


def _matchups(model: Model, t: int, f: Field) -> float:
    if t not in f.m:
        f.m[t] = sum(s * model.counter(t, y) for y, s in f.share.items())
    return f.m[t]


def edge(model: Model, deck: list[int], f: Field) -> float:
    """The deck's average log-odds against the field, up to a constant: cheap (36 lookups plus
    cached matchups), so thousands of candidate decks can be ranked. Exact for comparing decks
    in log-odds; win chance comes from `win_chance`."""
    return model.own(deck) + sum(_matchups(model, t, f) for t in deck)


def shares(model: Model, deck: list[int], f: Field) -> list[dict[str, float]]:
    """Each card's share of the deck's edge: alone, synergy and matchups (log-odds)."""
    out = []
    for t in deck:
        others = [u for u in deck if u != t]
        syn = sum(model.pair.get((min(t, u), max(t, u)), 0.0) for u in others) / 2
        alone = (model.card[t] if t != PAD else 0.0) - f.card_avg
        out.append(
            {"alone": alone, "synergy": syn - f.pair_avg, "matchups": _matchups(model, t, f)}
        )
    return out


def _wins(logit: float, p: float) -> float:
    """Log-odds as about how many wins per 100 games, near a win chance of p."""
    return round(logit * p * (1 - p) * 100, 1)


def _card(cards: dict[int, dict[str, Any]], model: Model, t: int) -> dict[str, Any] | None:
    v = model.variant(t) if t != PAD else None
    if v is None or v // 10 not in cards:
        return None
    return insights._played_as(cards[v // 10], v % 10)


def deck_view(
    model: Model, f: Field, mine: list[int], cards: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    deck = [model.token(v) for v in mine]
    p = win_chance(model, deck, f)
    parts = shares(model, deck, f)
    rows = []
    for t, s in zip(deck, parts, strict=True):
        c = _card(cards, model, t)
        if c:
            rows.append(
                c | {k: _wins(x, p) for k, x in s.items()} | {"total": _wins(sum(s.values()), p)}
            )
    rows.sort(key=lambda r: -r["total"])
    against = []
    for y, share in f.share.items():
        c = _card(cards, model, y)
        if c and share >= MIN_CARD_SHARE and y not in deck:
            effect = sum(model.counter(x, y) for x in deck)
            against.append(c | {"share": round(share, 3), "effect": _wins(effect, p)})
    against.sort(key=lambda r: -r["effect"])
    pairs: list[dict[str, Any]] = []
    for i, x in enumerate(sorted(deck)):
        for y in sorted(deck)[i + 1 :]:
            w = model.pair.get((x, y), 0.0)
            cx, cy = _card(cards, model, x), _card(cards, model, y)
            if cx and cy and w:
                pairs.append({"cards": [cx, cy], "effect": _wins(w, p)})
    pairs.sort(key=lambda r: -r["effect"])
    return {
        "win": round(p, 3),
        "cards": rows,
        "strong": [r for r in against[:SHOWN] if r["effect"] > 0],
        "weak": [r for r in against[::-1][:SHOWN] if r["effect"] < 0],
        "synergy": [r for r in pairs[:SHOWN] if r["effect"] > 0],
        "clash": [r for r in pairs[::-1][:1] if r["effect"] < 0],
        "field": {"decks": len(f.decks), "near": f.near},
    }


def package_view(
    model: Model,
    f: Field,
    mine: list[int],
    out: list[int],
    added: list[int],
    cards: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """What the model says about swapping `out` for `added`, and the biggest reasons."""
    old = [model.token(v) for v in mine]
    gone = {model.token(v) for v in out}
    new = [t for t in old if t not in gone] + [model.token(v) for v in added]
    p0, p1 = win_chance(model, old, f), win_chance(model, new, f)
    a0, a1 = shares(model, old, f), shares(model, new, f)
    part = {k: _wins(sum(s[k] for s in a1) - sum(s[k] for s in a0), p0) for k in a0[0]}
    reasons: list[dict[str, Any]] = []
    # The pair that changed most: a synergy gained (new card with another) or lost.
    kept = [t for t in old if t not in gone]
    fresh = [t for t in new if t not in kept]

    def w(t: int, u: int) -> float:
        return model.pair.get((min(t, u), max(t, u)), 0.0)

    changed = [
        (w(t, u), t, u, "gained") for i, t in enumerate(fresh) for u in kept + fresh[i + 1 :]
    ]
    changed += [(-w(t, u), t, u, "lost") for t in gone for u in kept]
    if changed:
        effect, t, u, how = max(changed, key=lambda r: abs(r[0]))
        ct, cu = _card(cards, model, t), _card(cards, model, u)
        if ct and cu and abs(_wins(effect, p0)) >= 0.5:
            reasons.append(
                {"kind": "synergy", "cards": [ct, cu], "effect": _wins(effect, p0), "how": how}
            )
    # The opponent card the swap handles most differently.
    best = None
    for y, share in f.share.items():
        if share < MIN_CARD_SHARE:
            continue
        d = share * (sum(model.counter(x, y) for x in new) - sum(model.counter(x, y) for x in old))
        if best is None or abs(d) > abs(best[0]):
            best = (d, y, share)
    if best:
        c = _card(cards, model, best[1])
        if c and abs(_wins(best[0], p0)) >= 0.5:
            reasons.append(
                {
                    "kind": "matchup",
                    "card": c,
                    "share": round(best[2], 3),
                    "effect": _wins(best[0], p0),
                }
            )
    return {
        "win": round(p1, 3),
        "gain": round((p1 - p0) * 100, 1),  # wins per 100 games
        "parts": part,
        "reasons": reasons,
    }


def about(model: Model) -> dict[str, Any]:
    return model.about | {"pairs": len(model.pair), "matchups": len(model.vs)}


MIN_TOKEN_GAMES = 5000  # the model has to have seen a card this often to recommend it
# ...and players with the player's core have to run it this often: the model adds up what it
# learned, so for a combination nobody plays it would happily extrapolate. Keeping to cards that
# real decks with the core use keeps its suggestions to decks it knows something about.
MIN_CORE_CARD_GAMES = 200
MIN_STEP = 0.5  # wins per 100: a swap smaller than this isn't worth suggesting
ALTERNATIVES = 3
MAX_SWAPS = 2  # more is a different deck: harder to try, and further from what the model knows


def _options(
    model: Model,
    cards: dict[int, dict[str, Any]],
    owned: dict[int, int] | None,
    taken: set[int],
    slot: int,
    allowed: set[int] | None = None,
) -> list[tuple[int, int]]:
    """(variant, token) of every card that can take a slot played as `slot` (0 plain, 1 Evo,
    2 Hero, 3 both): owned, not already in the deck, and seen often enough by the model. A card
    in an Evolution or Hero slot can also go in plain."""
    out = []
    for cid, card in cards.items():
        first = model.first.get(cid)
        if first is None or cid in taken or (owned is not None and cid not in owned):
            continue
        for form in {0, slot}:
            if form and not (
                insights.can_play(card, form) and (owned is None or owned[cid] & form == form)
            ):
                continue
            t = first + form
            if allowed is not None and cid * 10 + form not in allowed:
                continue
            if model.games and model.games[t] < MIN_TOKEN_GAMES:
                continue
            out.append((cid * 10 + form, t))
    return out


def _champion(cards: dict[int, dict[str, Any]], v: int) -> bool:
    return str(cards.get(v // 10, {}).get("rarity") or "").lower() == "champion"


def recommend(
    model: Model,
    f: Field,
    mine: list[int],
    flex: list[int],
    cards: dict[int, dict[str, Any]],
    owned: dict[int, int] | None,
    allowed: set[int] | None = None,
) -> dict[str, Any] | None:
    """The model's own suggestion: swap flex cards one at a time, each time for the owned card
    that raises the deck's edge against the field most, while a swap adds at least MIN_STEP wins
    per 100. Core cards stay. Returns the package, the steps, and the best single swaps."""
    deck = list(mine)
    p0 = win_chance(model, [model.token(v) for v in deck], f)
    slope = p0 * (1 - p0) * 100  # log-odds -> wins per 100, near the deck's win chance
    free = [v for v in deck if v in flex]
    steps: list[dict[str, Any]] = []
    singles: dict[int, tuple[float, int]] = {}  # first round: best replacement per flex card
    while free and len(steps) < MAX_SWAPS:
        cur = edge(model, [model.token(v) for v in deck], f)
        taken = {v // 10 for v in deck}
        best: tuple[float, int, int] | None = None
        for out_v in free:
            rest = [v for v in deck if v != out_v]
            rest_t = [model.token(v) for v in rest]
            has_champion = any(_champion(cards, v) for v in rest)
            for v, t in _options(model, cards, owned, taken, out_v % 10, allowed):
                if has_champion and _champion(cards, v):
                    continue
                e = edge(model, [*rest_t, t], f)
                if best is None or e > best[0]:
                    best = (e, out_v, v)
                if not steps and (out_v not in singles or e > singles[out_v][0]):
                    singles[out_v] = (e, v)
        if best is None or (best[0] - cur) * slope < MIN_STEP:
            break
        e, out_v, v = best
        steps.append(
            {
                "out": insights._as_card(cards, out_v),
                "in": insights._as_card(cards, v),
                "gain": round((e - cur) * slope, 1),
            }
        )
        deck = [x for x in deck if x != out_v] + [v]
        free.remove(out_v)
    if not steps:
        return None
    dropped = [v for v in mine if v not in deck]
    added = [v for v in deck if v not in mine]
    verdict = package_view(model, f, mine, dropped, added, cards)
    base = edge(model, [model.token(v) for v in mine], f)
    alternatives: list[dict[str, Any]] = sorted(
        (
            {
                "out": insights._as_card(cards, out_v),
                "in": insights._as_card(cards, v),
                "gain": round((e - base) * slope, 1),
            }
            for out_v, (e, v) in singles.items()
        ),
        key=lambda r: -r["gain"],
    )
    return {
        "win": round(p0, 3),
        "win_after": verdict["win"],
        "gain": verdict["gain"],
        "out": [insights._as_card(cards, v) for v in dropped],
        "in": [insights._as_card(cards, v) for v in added],
        "steps": steps,
        "reasons": verdict["reasons"],
        "parts": verdict["parts"],
        "singles": [r for r in alternatives if r["gain"] >= MIN_STEP][:ALTERNATIVES],
        "dropped": dropped,
        "added": added,
    }


def _check(a: Analyzer, tag: str, items: list[insights.Game], pick: dict[str, Any]) -> dict | None:
    """What battle data says about the model's pick: players with the same core who made the
    same swap, against those who kept the player's cards. None without a core."""
    arch = insights.archetype(a, tag, items)
    if arch is None:
        return None
    h2h = insights.package(arch, items, pick["dropped"], pick["added"])
    return {
        "verdict": h2h.verdict,  # better / worse / even / unknown (too few games)
        "gain": round(h2h.gap * 100, 1) if h2h.gap is not None else None,
        "margin": round(h2h.margin * 100, 1) if h2h.margin is not None else None,
        "games": h2h.in_games,
        "players": h2h.in_players,
        "near": h2h.near,
    }


REPLAY_GAMES = 30
MIN_REPLAY_GAMES = 10
SCORE = {"win": 1.0, "draw": 0.5, "loss": 0.0}


def chance(model: Model, a: list[int], b: list[int], gaps: tuple[float, float]) -> float:
    """P(deck a beats deck b) with a's card-level gap and trophy gap (thousands) over b."""
    logit = model.own(a) - model.own(b) + sum(model.counter(x, y) for x in a for y in b)
    logit += model.strength[0] * gaps[0] + model.strength[1] * gaps[1]
    return 1 / (1 + math.exp(-logit))


def _played(a: Analyzer, ids: list[int]) -> dict[tuple[int, str], tuple[list[int], float | None]]:
    """Each side's deck in these battles, as played (card id * 10 + form), and its average card
    level against the max (only the gap between the two sides matters)."""
    marks = ",".join("?" * len(ids))
    rows = a.query(
        f"""SELECT battle_id, player_tag, card_id, coalesce(evolution_level, 0) AS form,
                   level - max_level AS below
            FROM participant_cards WHERE battle_id IN ({marks}) AND kind = 'deck'""",
        tuple(ids),
    ).to_dicts()
    decks: dict[tuple[int, str], list[int]] = {}
    levels: dict[tuple[int, str], list[float]] = {}
    for r in rows:
        k = (r["battle_id"], r["player_tag"])
        decks.setdefault(k, []).append(r["card_id"] * 10 + r["form"])
        if r["below"] is not None:
            levels.setdefault(k, []).append(r["below"])
    return {
        k: (d, sum(levels[k]) / len(levels[k]) if levels.get(k) else None) for k, d in decks.items()
    }


def replay(
    a: Analyzer,
    model: Model,
    tag: str,
    items: list[insights.Game],
    mine: list[int],
    alt: list[int] | None,
    cards: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    """The player's last opponents, played again on paper: each battle's win chance with their
    deck and, if there's a suggestion, with the suggested one, against the same opponent with
    the same level and trophy gaps. Matchmaking goes by trophies, not decks, so they'd have met
    the same opponents. Only battles with their current deck, so the odds compare with what
    actually happened."""
    key = insights.current_deck_key(items)
    games = [g for g in items if g["deck_key"] == key][:REPLAY_GAMES]
    if len(games) < MIN_REPLAY_GAMES:
        return None
    decks = _played(a, [g["battle_id"] for g in games])
    now_t = [model.token(v) for v in mine]
    alt_t = [model.token(v) for v in alt] if alt else None
    rows: list[dict[str, Any]] = []
    for g in games:
        opp = decks.get((g["battle_id"], g["opponent_tag"]))
        me = decks.get((g["battle_id"], tag))
        if not opp or len(opp[0]) != 8:
            continue
        level_gap = me[1] - opp[1] if me and me[1] is not None and opp[1] is not None else 0.0
        mine_tr, theirs_tr = g["starting_trophies"], g["opponent_trophies"]
        trophy_gap = (mine_tr - theirs_tr) / 1000 if mine_tr and theirs_tr else 0.0
        theirs = [model.token(v) for v in opp[0]]
        p_now = chance(model, now_t, theirs, (level_gap, trophy_gap))
        p_alt = chance(model, alt_t, theirs, (level_gap, trophy_gap)) if alt_t else None
        rows.append(
            {
                "battle_id": g["battle_id"],
                "time": g["battle_time"],
                "result": g["result"],
                "opponent": g["opponent"],
                "deck": [insights._as_card(cards, v) for v in opp[0] if v // 10 in cards],
                "level_gap": round(level_gap, 2),
                "now": round(p_now, 3),
                "pick": round(p_alt, 3) if p_alt is not None else None,
            }
        )
    if len(rows) < MIN_REPLAY_GAMES:
        return None
    expected = sum(r["now"] for r in rows)
    return {
        "games": len(rows),
        "wins": sum(SCORE.get(r["result"], 0.0) for r in rows),
        "expected": round(expected, 1),
        "expected_pick": round(sum(r["pick"] for r in rows), 1) if alt_t else None,
        # The battles the suggestion changes most, either way.
        "battles": sorted(rows, key=lambda r: -abs((r["pick"] or r["now"]) - r["now"])),
    }


def _core_cards(a: Analyzer, tag: str, items: list[insights.Game]) -> set[int] | None:
    """Cards (as played) that decks with the player's core run often enough. None when the core
    isn't known: then any card the model has seen enough can be suggested."""
    arch = insights.archetype(a, tag, items)
    if arch is None:
        return None
    counts = arch.decks.select(pl.col("v").explode()).group_by("v").len()
    return set(counts.filter(pl.col("len") >= MIN_CORE_CARD_GAMES)["v"].to_list())


def report(
    a: Analyzer,
    path: Path,
    tag: str,
    items: list[insights.Game],
    replace: dict[str, Any],
) -> dict[str, Any] | None:
    """The model's part of a report: the deck's breakdown, the model's own swap suggestion
    (checked against battle data), and its verdict on the battle data's swaps. None without a
    model or a current deck."""
    model = load(path)
    key = insights.current_deck_key(items)
    if model is None or key is None:
        return None
    f = field_for(a, model, items)
    if f is None:
        return None
    played = insights.slots(a, tag, items)
    mine = [cid * 10 + played.get(cid, 0) for cid in insights._ids(key)]
    cards = insights._cards(a)
    for c in replace.get("candidates") or []:
        c["model"] = package_view(
            model,
            f,
            mine,
            [x["id"] * 10 + _level(x) for x in c["out"]],
            [x["id"] * 10 + _level(x) for x in c["in"]],
            cards,
        )
    # Only flex cards are swapped; without a known core, any card.
    core = {x["id"] * 10 + _level(x) for x in replace.get("core") or []}
    flex = [v for v in mine if v not in core]
    pick = recommend(
        model, f, mine, flex, cards, insights.unlocked(a, tag), _core_cards(a, tag, items)
    )
    alt = None
    if pick:
        pick["check"] = _check(a, tag, items, pick)
        alt = [v for v in mine if v not in pick["dropped"]] + pick["added"]
        del pick["dropped"], pick["added"]
    replayed = replay(a, model, tag, items, mine, alt, cards)
    view = deck_view(model, f, mine, cards)
    view["field"]["scope"] = insights.trophy_scope(items) if f.near else "all players"
    ranked = f.near and items[0]["battle_type"] == "pathOfLegend"
    view["field"]["where"] = "in ranked" if ranked else "at your trophies" if f.near else "overall"
    return view | {"pick": pick, "replay": replayed, "about": about(model)}


LEVEL = {None: 0, "evolution": 1, "hero": 2, "both": 3}


def _level(card: dict[str, Any]) -> int:
    return LEVEL.get(card.get("status"), 0)
