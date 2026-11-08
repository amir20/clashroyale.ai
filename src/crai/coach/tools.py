"""The tools Rook can call: what each one runs, and the schema sent to the model.

Descriptions live in prompts/tools.txt. `show_*` tools return a visual that the page draws.
"""

import asyncio
from collections import Counter
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from crai.analysis import insights, visuals
from crai.analysis.analytics import Analyzer, records
from crai.coach import prompts
from crai.config import Settings


class CoachContext(Protocol):
    """What the AI loop needs from the app."""

    settings: Settings
    transport: httpx.AsyncBaseTransport | None
    analyzer: Analyzer
    lock: asyncio.Lock


ART_KEYS = frozenset({"icon", "evo_icon", "hero_icon", "copy_url"})


def without_art(value: Any) -> Any:
    """A tool result for the model: same numbers, minus image links it has no use for."""
    if isinstance(value, dict):
        return {k: without_art(v) for k, v in value.items() if k not in ART_KEYS}
    if isinstance(value, list):
        return [without_art(v) for v in value]
    return value


def player_tools(svc: CoachContext, tag: str) -> dict[str, Callable[[dict[str, Any]], Any]]:
    a = svc.analyzer

    def items() -> list[insights.Game]:
        return insights.games(a, tag)

    def overview() -> dict[str, Any]:
        """The overview, with a current streak the model can't misread from a list of results."""
        o = insights.overview(a, tag, items())
        recent = Counter(o.pop("recent"))
        return o | {
            "current_streak": o.pop("streak"),
            "last_20_games": {"wins": recent["win"], "losses": recent["loss"]},
        }

    return {
        "get_overview": lambda _: overview(),
        "get_patterns": lambda _: insights.wins_and_losses(items()),
        "which_card_to_replace": lambda _: insights.replace_card(a, tag, items()),
        "cards_vs_other_players": lambda _: insights.best_cards(a, items()),
        "opponent_cards_that_beat_me": lambda _: insights.nemesis(a, items()),
        "my_decks_compared": lambda _: insights.best_deck(a, items()),
        "show_swap": lambda args: visuals.swap_view(
            a, tag, items(), args["out_card"], args["in_card"]
        ),
        "show_matchup": lambda args: visuals.matchup_view(a, items(), args["card"]),
        "show_card_ranking": lambda args: visuals.ranking_view(a, args["card"], args["kind"]),
        "show_deck": lambda args: visuals.deck_view(a, args["cards"]),
        "show_top_cards": lambda _: visuals.top_cards_view(a),
        "show_worst_matchups": lambda _: visuals.worst_matchups_view(a, items()),
        "show_trend": lambda _: visuals.trend_view(a, items()),
        "show_close_games": lambda _: visuals.close_games_view(items()),
        "show_elixir": lambda _: visuals.elixir_view(a, items()),
        "my_record_with_card": lambda args: insights.record_with(a, items(), args["card"]),
        "not_about_the_game": lambda _: {"note": "Nothing to look up. Don't quote any numbers."},
        "my_record_against_card": lambda args: insights.record_against(a, items(), args["card"]),
        "best_partners_for_card": lambda args: records(
            a.card_pairs(args["card"], min_games=50, sort_by="adj_win_rate")[1], 10
        ),
        "counters_for_card": lambda args: records(
            a.card_matchups(args["card"], min_games=50)[1], 10
        ),
        "top_decks_with_cards": lambda args: visuals.proven_decks(
            a, args.get("cards", []), limit=5
        ),
    }


TOOL_PARAMS: dict[str, dict[str, Any]] = {
    "show_swap": {"out_card": {"type": "string"}, "in_card": {"type": "string"}},
    "show_matchup": {"card": {"type": "string"}},
    "show_card_ranking": {
        "card": {"type": "string"},
        "kind": {"type": "string", "enum": ["pairs", "counters"]},
    },
    "show_deck": {"cards": {"type": "array", "items": {"type": "string"}}},
    "my_record_against_card": {"card": {"type": "string"}},
    "my_record_with_card": {"card": {"type": "string"}},
    "best_partners_for_card": {"card": {"type": "string"}},
    "counters_for_card": {"card": {"type": "string"}},
    "top_decks_with_cards": {"cards": {"type": "array", "items": {"type": "string"}}},
}


def tool_schema() -> list[dict[str, Any]]:
    schema = []
    for name, (_, description) in prompts.tool_prompts().items():
        props = TOOL_PARAMS.get(name, {})
        parameters = {"type": "object", "properties": props, "required": list(props)}
        function = {"name": name, "description": description, "parameters": parameters}
        schema.append({"type": "function", "function": function})
    return schema
