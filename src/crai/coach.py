"""Rook's voice: turns computed insights into cheeky, honest coaching lines.

Rook is a rival who happens to be on your side. Rules the jokes never break: every number comes
from the insights, and a difference inside its margin of error is called noise, not a finding.
Lines are picked deterministically per player so a page reload doesn't change what Rook said.
"""

import random
from collections.abc import Iterable
from pathlib import Path
from typing import Any

PROMPTS = Path(__file__).parent / "prompts"


def system_prompt() -> str:
    """Rook's instructions. Read on every question, so edits apply without a restart."""
    return (PROMPTS / "system.txt").read_text()


def tool_prompts() -> dict[str, tuple[str, str]]:
    """Tool name -> (status label, description for the model), from prompts/tools.txt."""
    tools: dict[str, tuple[str, str]] = {}
    for line in (PROMPTS / "tools.txt").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, label, description = (part.strip() for part in line.split("|", 2))
            tools[name] = (label, description)
    return tools


def suggestions_prompt() -> str:
    """Instructions for proposing follow-up questions, limited to what the tools can answer."""
    capabilities = "\n".join(f"- {desc}" for _, desc in tool_prompts().values())
    return (PROMPTS / "suggestions.txt").read_text().replace("{capabilities}", capabilities)


def parse_suggestions(text: str | None, asked: Iterable[str], limit: int = 3) -> list[str]:
    """Clean the model's lines into usable questions, dropping anything malformed or repeated."""
    seen = {q.strip().casefold() for q in asked}
    found: list[str] = []
    for raw in (text or "").splitlines():
        q = raw.strip().lstrip("-*•0123456789.) ").strip('"')
        if q.endswith("?") and 8 <= len(q) <= 90 and q.casefold() not in seen:
            seen.add(q.casefold())
            found.append(q)
    return found[:limit]


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _pick(seed: str, *options: str) -> str:
    return random.Random(seed).choice(options)


def intro(o: dict[str, Any]) -> str:
    p, r, s = o["player"], o["record"], o["streak"]
    name = p["name"]
    if r["games"] == 0:
        return f"{name}, I found you, but you've got no recent battles I can roast. Go play a few."
    opener = _pick(
        p["tag"],
        f"Well, well. {name}.",
        f"So you're {name}. I've heard things.",
        f"{name}! Let's see how much damage you've done.",
    )
    body = f"I found {r['games']} recent games: {r['wins']} wins, {r['losses']} losses."
    if s["kind"] == "win" and s["length"] >= 3:
        tail = f"And you're on a {s['length']}-game win streak. Annoying. Keep going."
    elif s["kind"] == "loss" and s["length"] >= 3:
        tail = f"{s['length']} losses in a row, though. Put the phone down after this one."
    elif r["win_rate"] >= 0.55:
        tail = "That's winning more than you lose. Don't let it go to your head."
    elif r["win_rate"] < 0.45:
        tail = "We have some work to do. Lucky you found me."
    else:
        tail = "Right on the coin flip. Let's find you an edge."
    return f"{opener} {body} {tail}"


def wins_and_losses(w: dict[str, Any]) -> str:
    close = w["close_games"]
    if not close["games"]:
        return "Not enough games to see a pattern yet."
    crowns = (
        f"You take {w['crowns_for']} crowns a game and give up {w['crowns_against']}."
        if w["crowns_for"] is not None
        else ""
    )
    if close["win_rate"] >= 0.55:
        clutch = f"In one-crown games you're {close['wins']}-{close['losses']}. Clutch. Irritating."
    elif close["win_rate"] <= 0.45:
        clutch = f"One-crown games? {close['wins']}-{close['losses']}. You fold under pressure."
    else:
        clutch = f"One-crown games are {close['wins']}-{close['losses']}, dead even."
    three = w["three_crowned_losses"]
    ouch = (
        f" You've been three-crowned {three} times. I counted. Twice."
        if three >= 3
        else " And you rarely get three-crowned, which I respect."
    )
    return f"{crowns} {clutch}{ouch}".strip()


def _pct1(x: float) -> str:
    return f"{x * 100:.1f}%"


def replace_card(r: dict[str, Any]) -> str:
    if not r.get("enough"):
        return (
            "Your deck's too unusual: I can't find enough similar decks to compare it fairly. "
            "Weird flex, but okay."
        )
    weak, n = r["weakest"], f"{r['family_decks']:,}"
    if weak is None:
        return "I couldn't rate your cards this time. Try again after a few more games."
    stats = (
        f"decks with it win {_pct1(weak['win_rate_with'])} of games, "
        f"decks without it {_pct1(weak['win_rate_without'])}"
    )
    if weak["real"]:
        msg = f"{weak['name']} is costing you: {stats}, across {n} decks like yours."
    else:
        msg = (
            f"I checked {n} decks like yours and nothing in yours is actually bad. "
            f"{weak['name']} is your lowest: {stats}. That gap's too small to matter. "
            "Stop blaming your cards."
        )
    if r["candidates"]:
        c = r["candidates"][0]
        msg += (
            f" If you're itching to swap, test {c['name']}: decks with it win "
            f"{_pct1(c['win_rate_with'])}, from {c['players']:,} different players. "
            "I checked a lot of cards, so it could still be a fluke."
        )
    return msg


def best_cards(r: dict[str, Any]) -> str:
    best = r["best"]
    if best is None:
        return (
            "Play at least five games with this deck and I'll tell you who's pulling their weight."
        )
    head = (
        f"{best['name']}: you win {_pct(best['your_win_rate'])} with it, everyone else "
        f"{_pct(best['field_win_rate'])}, over {best['games']} of your games."
    )
    if best["real"] and best["diff"] > 0:
        return head + " You're genuinely better with it than most. Don't tell anyone I said that."
    return head + f" But {best['games']} games isn't enough for me to be impressed yet."


def nemesis(r: dict[str, Any]) -> str:
    if not r["worst"]:
        return "You haven't seen any card often enough for me to name a nemesis. Play more."
    w = r["worst"][0]
    line = f"{w['name']} has your number: you won {w['wins']} of {w['games']} games against it."
    if w["real"]:
        return line + " That's a real problem, not bad luck."
    return line + " That's a tiny sample, so it might be bad luck, but I'd keep an eye on it."


def best_deck(r: dict[str, Any]) -> str:
    decks = r["decks"]
    if not decks:
        return "You need at least three games on a deck before I'll judge it."
    d = decks[0]
    head = f"Your best so far went {d['wins']}-{d['losses']} ({_pct(d['win_rate'])})."
    if len(decks) == 1:
        return head + " It's also your only deck with enough games. Commitment. Or fear."
    gap = d["win_rate"] - decks[1]["win_rate"]
    if gap > max(d["margin"], decks[1]["margin"]):
        return head + " It's clearly ahead. Stick with it."
    return head + " The next one is close enough that it could just be luck."


def evidence(tool: str, r: Any) -> str | None:
    """A plain description of what a tool looked at, built from its result (never by the AI)."""
    if isinstance(r, dict) and "error" in r:
        return None
    match tool:
        case "get_overview":
            return f"Your last {r['record']['games']} games"
        case "get_patterns":
            return f"{r['wins'] + r['losses']} of your games, wins vs losses"
        case "which_card_to_replace" if r.get("enough"):
            return f"{r['family_decks']:,} decks like yours from the last 7 days"
        case "which_card_to_replace":
            return "Too few similar decks to compare"
        case "cards_vs_other_players":
            return "Your win rate with each card vs all players, last 7 days"
        case "opponent_cards_that_beat_me":
            return "Your record against every card you've faced at least 3 times"
        case "my_record_against_card" if r["games"]:
            wins = "1 win" if r["wins"] == 1 else f"{r['wins']} wins"
            return f"{r['card']}: {wins} in {r['games']} of your games"
        case "my_record_against_card":
            return f"You haven't faced {r['card']} in {r['total_games_checked']} recorded games"
        case "my_decks_compared":
            return f"{len(r['decks'])} of your decks with at least 3 games"
        case "best_partners_for_card":
            return "Card pairings across all players, last 7 days"
        case "counters_for_card":
            return "Matchups across all players, last 7 days"
        case "top_decks_with_cards":
            return "Top decks across all players, last 7 days"
    return None


# Status lines shown while work is happening, so the screen is never idle.
LOADING_LINES = (
    "Sharpening my beak…",
    "Going through your replays. Oh no.",
    "Counting crowns. Yours won't take long.",
    "Asking your opponents what happened…",
    "Still here. Your decks are a lot to process.",
    "Comparing you to people who are better at this…",
)
