"""Rook's voice: turns computed insights into cheeky, honest coaching lines.

Rook is a rival who happens to be on your side. Rules the jokes never break: every number comes
from the insights, and a difference inside its margin of error is called noise, not a finding.
Lines are picked deterministically per player so a page reload doesn't change what Rook said.
"""

import random
import re
from typing import Any

MIN_GAMES_TO_JUDGE = 15  # below this, a big difference is usually just a few lucky games


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


FORM = {"evolution": "Evo", "hero": "Hero", "both": "Evo + Hero"}


def _as_played(card: dict[str, Any]) -> str:
    """'Hero Giant' for a card in a Hero slot, plain 'Giant' otherwise."""
    form = FORM.get(card.get("status") or "")
    return f"{form} {card['name']}" if form else card["name"]


def _per_100(gain: float) -> str:
    return f"{abs(gain) * 100:.1f} {'more' if gain > 0 else 'fewer'} games per 100"


def _names(cards: list[dict[str, Any]]) -> str:
    names = [c["name"] for c in cards]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def swap_caveats(c: dict[str, Any]) -> str:
    """How literal the swap is, and where it loses: the swap may not work on its own."""
    out = []
    m = c.get("match") or {}
    if m.get("exact"):
        out.append("That's players running your exact deck with just that one change.")
    elif m.get("also"):
        also = m["also"]
        out.append(
            f"Heads up: {round(also['share'] * 100)}% of those decks also swapped "
            f"{_names(also['dropped'])} for {_names(also['added'])}, so you may need both."
        )
    if watch := c.get("watch_out"):
        worst = watch[0]
        out.append(
            f"It's weaker against {_names(watch)}: {_per_100(worst['gap'])} vs {worst['name']}."
        )
    return " ".join(out)


def replace_card(r: dict[str, Any]) -> str:
    if r.get("no_games"):
        return "No recent games, so no deck to judge. Go play a few and I'll tear it apart."
    if not r.get("enough"):
        return (
            "Your deck's too unusual: I can't find enough similar decks to compare it fairly. "
            "Weird flex, but okay."
        )
    weak, n = r["weakest"], f"{r['family_decks']:,}"
    if weak is None:
        return "I couldn't rate your cards this time. Try again after a few more games."
    stats = f"decks with it win {_per_100(weak['gain'])} than decks without it"
    if weak["real"]:
        msg = f"{_as_played(weak)} is costing you: {stats}, across {n} decks like yours."
    else:
        msg = (
            f"I checked {n} decks like yours and no card in yours is actually hurting you. "
            f"{_as_played(weak)} is your lowest: {stats}, and that gap's too small to matter. "
            "Your losses are coming from somewhere else, so ask me why you keep losing."
        )
    if r["candidates"]:
        c = r["candidates"][0]
        msg += (
            f" If you're itching to swap, test {_as_played(c)}: decks with it win "
            f"{_per_100(c['gain'])} than decks keeping {_as_played(weak)}, from "
            f"{c['players']:,} different players. {swap_caveats(c)} "
            "I checked a lot of cards, so it could still be a fluke."
        ).replace("  ", " ")
    return msg


def try_line(r: dict[str, Any]) -> str:
    """The short version of replace_card, for the story card under the swap art."""
    if not r.get("enough") or r.get("weakest") is None:
        return replace_card(r)
    weak = r["weakest"]
    if r["candidates"]:
        c = r["candidates"][0]
        return (
            f"Test {_as_played(c)} for {_as_played(weak)}. Decks like yours with it win "
            f"{_per_100(c['gain'])}. {swap_caveats(c)} Could still be a fluke, so give it 20 games."
        ).replace("  ", " ")
    if weak["real"]:
        gain = _per_100(weak["gain"])
        return f"{_as_played(weak)} is costing you: decks like yours win {gain} with it."
    return "None of your cards is hurting you. Your losses come from somewhere else."


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


def _about(games: int) -> int:
    return max(10, round(games, -1))


def trend(t: dict[str, Any]) -> str:
    lobbies = t["lobbies"]
    excuse = ""
    if lobbies:
        excuse = (
            " Your opponents got tougher, which explains some of it."
            if lobbies["harder"]
            else " Your opponents got no tougher, so that's no excuse."
        )
    if t["verdict"] == "changed_decks":
        more = _about(t["more_games"])
        return (
            f"You keep changing decks, so older and newer games aren't a fair fight.{excuse} "
            f"Stick with this one for about {more} more games and I can call it."
        )
    older, newer = _pct(t["older"]["win_rate"]), _pct(t["newer"]["win_rate"])
    head = f"Same deck: you won {older} of your older games and {newer} of your newer ones."
    match t["verdict"]:
        case "improving":
            return f"{head} You're actually getting better. Disgusting."
        case "slipping":
            return f"{head} That's a real slide.{excuse}"
    if t["more_games"] is None:
        return f"{head} Basically no change."
    return f"{head} Too close to call. About {_about(t['more_games'])} more games would settle it."


def close_games(w: dict[str, Any]) -> str:
    c = w["close_games"]
    head = f"You've won {c['wins']} of {c['games']} games decided by one crown."
    if c["games"] < MIN_GAMES_TO_JUDGE:
        return f"{head} Too few to judge you. Yet."
    if c["win_rate"] >= 0.55:
        return f"{head} Clutch. Irritating."
    if c["win_rate"] <= 0.45:
        return f"{head} You fold under pressure."
    return f"{head} Basically a coin flip."


def worst_matchups(v: dict[str, Any]) -> str:
    w = next((r for r in v["rows"] if r["real"]), v["rows"][0])
    line = f"{w['name']} has your number: you won {w['wins']} of {w['games']} against it."
    return line + (" That's not bad luck." if w["real"] else " Tiny sample, but I'm watching it.")


def elixir(e: dict[str, Any]) -> str:
    share = e["higher_than"]
    cost = f"Your deck costs {e['avg_elixir']}, "
    cost += (
        f"more than {_pct(share)} of decks."
        if share >= 0.5
        else f"less than {_pct(1 - share)} of decks."
    )
    if e["costs_wins"]:
        return f"{cost} Decks at that cost win less often. Your wallet's fine, your elixir isn't."
    return f"{cost} Decks at that cost win as often as any, so stop worrying."


def best_deck(r: dict[str, Any]) -> str:
    decks = r["decks"]
    if not decks:
        return "You need at least three games on a deck before I'll judge it."
    d = decks[0]
    head = f"Your best so far went {d['wins']}-{d['losses']} ({_pct(d['win_rate'])})."
    if len(decks) == 1:
        return (
            head + " It's also your only deck with enough games. Commitment. Or fear."
        ) + _proven_line(r)
    gap = d["win_rate"] - decks[1]["win_rate"]
    if min(d["games"], decks[1]["games"]) >= MIN_GAMES_TO_JUDGE and gap > max(
        d["margin"], decks[1]["margin"]
    ):
        return head + " It's clearly ahead. Stick with it." + _proven_line(r)
    return head + " The next one is close enough that it could just be luck." + _proven_line(r)


def _proven_line(r: dict[str, Any]) -> str:
    """Point at a deck other players have proven, keeping the cards this player likes."""
    p = r.get("proven")
    if not p:
        return ""
    who = "players at your trophies" if p["scope"].startswith("Other players") else "everyone"
    kept = " and ".join(p["kept"])
    return (
        f" Want a sure thing? This deck keeps your {kept} and wins {_pct(p['win_rate'])} for "
        f"{who}, over {p['games']:,} games from {p['players']:,} players."
    )


# The model occasionally breaks these rules however the prompt is worded, so enforce them here.
_NARRATES_THE_CARD = re.compile(
    r"\b(?:on|up on|on your) (?:the )?screen\b|\bunder my reply\b"
    r"|\bI(?:'ve| have)? drawn\b|\bI drew\b",
    re.IGNORECASE,
)
_WORDS = (
    (re.compile(r"\bthe tools? (?:says?|shows?)\b", re.IGNORECASE), "I see"),
    (re.compile(r"\bthe tools?\b", re.IGNORECASE), "I"),
    (re.compile(r"\bsmaller than (?:the|its|their) margins?\b", re.IGNORECASE), "small"),
    (re.compile(r"\bmargins\b", re.IGNORECASE), "gaps"),
    (re.compile(r"\bmargin\b", re.IGNORECASE), "gap"),
    (re.compile(r"\b(my|the|this|that|no|your) data\b", re.IGNORECASE), r"\1 games"),
    (re.compile(r"\bdata\b", re.IGNORECASE), "games"),
    (re.compile(r"\bstats\b", re.IGNORECASE), "numbers"),
    (re.compile(r"\bstat\b", re.IGNORECASE), "number"),
    (re.compile(r"\bverdict\b", re.IGNORECASE), "call"),
    (re.compile(r"\bbaseline\b", re.IGNORECASE), "usual rate"),
)
_ALL_PLAYERS = re.compile(r"\b(?:across|among|for) all players\b", re.IGNORECASE)


def tidy(text: str, only_similar_decks: bool = False) -> str:
    """Drop sentences announcing the drawing, and replace words Rook must never use.

    `only_similar_decks` is set when a swap is the only evidence, so "all players" is always wrong.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [s for s in sentences if not _NARRATES_THE_CARD.search(s)]
    out = " ".join(kept or sentences)
    for pattern, plain in _WORDS:
        out = pattern.sub(plain, out)
    return _ALL_PLAYERS.sub("in decks like yours", out) if only_similar_decks else out


def _where(r: Any, what: str) -> str:
    """Evidence for a ranking: whose games it came from, read off the result's scope."""
    near = isinstance(r, dict) and str(r.get("scope", "")).startswith("Other players")
    return (
        f"{what} among players at your trophies, last 7 days"
        if near
        else (f"{what} across all players, last 7 days")
    )


def _decks_like_yours(r: dict[str, Any]) -> str:
    near = "near your trophies " if r.get("near") else ""
    return f"{r['family_decks']:,} decks like yours {near}from the last 7 days"


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
            return _decks_like_yours(r)
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
        case "show_swap":
            return _decks_like_yours(r)
        case "my_record_with_card" if r["games"]:
            wins = "1 win" if r["wins"] == 1 else f"{r['wins']} wins"
            return f"{r['card']}: {wins} in {r['games']} of your games with it"
        case "my_record_with_card":
            return f"You haven't played {r['card']} in your recorded games"
        case "show_top_cards":
            return _where(r, "Every card")
        case "show_worst_matchups":
            return "Your record against cards you've faced at least 5 times"
        case "show_trend" if r["same_deck"]:
            return f"{r['older']['games'] + r['newer']['games']} of your games on the same deck"
        case "show_trend":
            return "Your games, split by the deck you played"
        case "show_close_games":
            return f"{r['close_games']['games']} of your one-crown games"
        case "show_elixir":
            return "Deck costs across all players, last 7 days"
        case "show_matchup" if r["games"]:
            wins = "1 win" if r["wins"] == 1 else f"{r['wins']} wins"
            return f"{r['card']['name']}: {wins} in {r['games']} of your games"
        case "show_matchup":
            return f"You haven't faced {r['card']['name']} in your recorded games"
        case "show_card_ranking":
            return _where(r, "Results")
        case "show_deck":
            return _where(r, f"{r['games']:,} games of this deck from {r['players']:,} players")
        case "best_partners_for_card":
            return _where(r, "Card pairings")
        case "counters_for_card":
            return _where(r, "Matchups")
        case "top_decks_with_cards":
            return _where(r, "Top decks")
        case "how_to_win_more" if r.get("enough"):
            return f"Your last {r['games']} games, checked for what's costing you wins"
        case "how_to_win_more":
            return "Too few of your games to find a pattern"
        case "how_many_games_rook_tracks":
            return "Every battle I've tracked, last 7 days"
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


WAKING_UP = "I just restarted, so I'm loading everyone's battles first. About 20 seconds…"
WAKING_LINES = (
    "Still loading everyone's battles…",
    "Millions of battles to read. Hang on…",
    "It's only this slow right after a restart…",
)

# The same loading screen when looking someone else up, such as an opponent.
SCOUT_LINES = (
    "Sharpening my beak…",
    "Going through their replays…",
    "Counting their crowns…",
    "Asking around about them…",
    "Comparing them to everyone else…",
)

# What the loading screen says as each part of the report starts, and how far along it is.
REPORT_STEPS = {
    "data": (76, "Catching up on the latest battles…"),
    "games": (78, "Reading your games…"),
    "overview": (80, "Adding up your record…"),
    "similar": (82, "Finding players with decks like yours…"),
    "trend": (86, "Checking how you're trending…"),
    "worst": (88, "Finding the cards that beat you…"),
    "elixir": (90, "Comparing your elixir cost…"),
    "replace": (92, "Picking a card worth swapping…"),
    "surprises": (94, "Looking for things you didn't know…"),
}
SCOUT_STEPS = REPORT_STEPS | {
    "games": (78, "Reading their games…"),
    "overview": (80, "Adding up their record…"),
    "similar": (82, "Finding players with decks like theirs…"),
    "trend": (86, "Checking how they're trending…"),
    "worst": (88, "Finding the cards that beat them…"),
    "elixir": (90, "Comparing their elixir cost…"),
    "replace": (92, "Crunching the last numbers…"),
    "surprises": (94, "Almost there…"),
}


def _tip_against(card: dict[str, Any]) -> str:
    """Rook's opinion on playing into a card, by what kind of card it is (from its id)."""
    kind = str(card["id"])[:2]
    if kind == "28":
        return (
            f"Stop stacking your support troops where {card['name']} can hit them all. "
            "Spread them out, and push once they've used it."
        )
    if kind == "27":
        return f"Pull your push to the other lane, or chip it down before {card['name']} lands."
    return f"Save your answer to {card['name']} for when it shows up, not before."


def _problem_line(kind: str, f: dict[str, Any]) -> str:
    """One finding from game_plan, as a diagnosis plus the thing to do about it."""
    match kind:
        case "worst_matchups":
            c = f["cards"][0]
            return (
                f"{c['name']} is your real problem: you've won {c['wins']} of {c['games']} "
                f"against it. {_tip_against(c)}"
            )
        case "card_levels":
            yes = f["in_those_games"]
            return (
                f"Card levels are hurting: against higher-level decks you win "
                f"{_pct(yes['win_rate'])} of {yes['games']} games. "
                "Put your gold into this deck only."
            )
        case "after_two_losses":
            yes, no = f["in_those_games"], f["other_games"]
            return (
                f"You tilt: right after two losses you win {_pct(yes['win_rate'])}, "
                f"otherwise {_pct(no['win_rate'])}. Lose two, put the phone down."
            )
        case "deck_switching":
            return (
                f"You used {f['decks_used']} decks in your last {f['recent_games']} games. "
                "Pick one and play it for a week. Hopping resets everything you've learned."
            )
        case "close_games":
            return (
                f"You've won only {f['wins']} of {f['games']} one-crown games. Stop spending "
                "elixir on the last 30 seconds of chip and defend your lead."
            )
        case "three_crowned":
            return (
                f"{f['three_crowned_losses']} of your {f['losses']} losses were three-crown "
                "blowouts. When a push breaks through, defend instead of racing."
            )
    return ""


def game_plan(r: dict[str, Any]) -> str:
    if not r.get("enough"):
        return f"Only {r['games']} games so far. Play at least 20 and I'll tell you what's wrong."
    real = r["real_problems"]
    if not real:
        rec = r["record"]
        return (
            f"You're {rec['wins']}-{rec['losses']}, and nothing in your games stands out as "
            "costing you wins: not levels, not tilt, not one card. Stick with this deck and grind."
        )
    lines = [_problem_line(k, r["findings"][k]) for k in real[:2]]
    return " ".join(line for line in lines if line)


def surprise(x: dict[str, Any]) -> dict[str, str]:
    """A headline and one line of proof for something the player couldn't see in the game."""
    match x["kind"]:
        case "signature_card":
            r, name = x["record"], x["card"]["name"]
            return {
                "headline": f"You're a {name} specialist",
                "detail": (
                    f"You win {_pct(r['win_rate'])} with it over {r['games']} games. "
                    f"Players at your trophies: {_pct(x['field_win_rate'])}."
                ),
            }
        case "unique_deck":
            alike = "Nobody" if x["players_alike"] == 0 else f"Only {x['players_alike']}"
            return {
                "headline": "Nobody plays your deck",
                "detail": (
                    f"{alike} of {x['players_total']:,} players at your trophies this week "
                    f"ran {x['shared_cards']} of your 8 cards."
                ),
            }
        case "free_wins":
            c = x["cards"][0]
            losses = c["games"] - c["wins"]
            return {
                "headline": f"{c['name']} is a free win",
                "detail": f"You're {c['wins']}-{losses} when they bring it.",
            }
        case "evolution_gap":
            m, f = x["matched"], x["fewer"]
            return {
                "headline": "Evolutions are your wall",
                "detail": (
                    f"When they match your {x['your_evolutions']} evolved cards you win "
                    f"{_pct(m['win_rate'])}. When they have fewer: {_pct(f['win_rate'])}."
                ),
            }
    return {"headline": "", "detail": ""}
