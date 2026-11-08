"""Rook's prompts. The .txt files here are read on every call, so edits apply without a restart."""

from collections.abc import Iterable
from pathlib import Path

PROMPTS = Path(__file__).parent


def system_prompt(card_names: Iterable[str] = ()) -> str:
    """Rook's instructions. Read on every question, so edits apply without a restart."""
    return (PROMPTS / "system.txt").read_text().replace("{cards}", ", ".join(card_names))


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
    capabilities = "\n".join(
        f"- {desc}" for name, (_, desc) in tool_prompts().items() if name != "not_about_the_game"
    )
    return (PROMPTS / "suggestions.txt").read_text().replace("{capabilities}", capabilities)
