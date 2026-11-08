"""Rook's AI loop: custom questions (with tool calls) and follow-up suggestions.

`_ask_coach` is the loop itself: call the model, run any tools it asks for, feed the results back,
and stop when it answers. Events are plain dicts; the web layer turns them into a stream.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Iterable
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from crai.analysis import insights
from crai.coach import prompts, voice
from crai.coach.tools import CoachContext, player_tools, tool_schema, without_art

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_TOOL_ROUNDS = 4
MAX_OUTPUT_TOKENS = 1200
MAX_VISUALS = 2
MAX_HISTORY = 12  # messages of earlier conversation sent back to the model


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=2000)


class Ask(BaseModel):
    question: str = Field(min_length=3, max_length=300)
    history: list[Turn] = Field(default_factory=list, max_length=MAX_HISTORY)


class SuggestRequest(BaseModel):
    history: list[Turn] = Field(default_factory=list, max_length=MAX_HISTORY)
    asked: list[str] = Field(default_factory=list, max_length=40)


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


def _log_call(
    kind: str, tag: str, text: str, spent: dict[str, Any], model: str, seconds: float
) -> None:
    """One line per AI request: who asked what, and what it cost."""
    tools = ",".join(spent.get("tools", [])) or "none"
    log.info(
        "AI %s tag=%s model=%s calls=%d tools=%s in=%d out=%d cost=$%.5f time=%.1fs text=%r",
        kind,
        tag,
        model,
        spent["rounds"],
        tools,
        spent["input_tokens"],
        spent["output_tokens"],
        spent["cost_usd"],
        seconds,
        text[:200],
    )


def _finished_text(content: str | None, finish_reason: str | None) -> str:
    """The reply to show. One cut off by the token limit is trimmed to its last sentence."""
    text = (content or "").strip()
    if finish_reason != "length":
        return text
    end = max(text.rfind(mark) for mark in ".!?")
    return text[: end + 1] if end >= 0 else ""


async def ask_coach(
    svc: CoachContext, tag: str, question: str, left: int, history: list[Turn]
) -> AsyncIterator[dict[str, Any]]:
    """Answer a custom question. Every path ends in an `answer` or `error` event."""
    try:
        async for event in _ask_coach(svc, tag, question, left, history):
            yield event
    except Exception:
        log.exception("custom question failed for %s", tag)
        yield {"type": "error", "message": "My brain glitched. Try asking that again."}


async def _ask_coach(
    svc: CoachContext, tag: str, question: str, left: int, history: list[Turn]
) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "status", "message": "Rook is thinking…"}
    tools = player_tools(svc, tag)
    labels = prompts.tool_prompts()
    started = time.monotonic()
    spent: dict[str, Any] = {"rounds": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompts.system_prompt()},
        *[t.model_dump() for t in history],
        {"role": "user", "content": question},
    ]
    headers = {"Authorization": f"Bearer {svc.settings.openrouter_key}"}
    async with httpx.AsyncClient(timeout=60, transport=svc.transport) as http:
        evidence: list[str] = []
        called: list[str] = []
        visuals: list[dict[str, Any]] = []
        for attempt in range(MAX_TOOL_ROUNDS + 1):
            resp = await http.post(
                OPENROUTER_URL,
                headers=headers,
                json={
                    "model": svc.settings.openrouter_model,
                    "messages": messages,
                    "tools": tool_schema(),
                    # The first turn must look at data, so no answer is ever ungrounded.
                    "tool_choice": "required" if attempt == 0 else "auto",
                    # The limit includes the model's reasoning, so leave room for both.
                    "max_tokens": MAX_OUTPUT_TOKENS,
                    "reasoning": {"effort": "low"},
                    "usage": {"include": True},
                },
            )
            resp.raise_for_status()
            body = resp.json()
            choice = body["choices"][0]
            msg = choice["message"]
            used = body.get("usage") or {}
            spent["rounds"] += 1
            spent["input_tokens"] += used.get("prompt_tokens") or 0
            spent["output_tokens"] += used.get("completion_tokens") or 0
            spent["cost_usd"] += used.get("cost") or 0.0
            calls = msg.get("tool_calls") or []
            if not calls:
                text = _finished_text(msg.get("content"), choice.get("finish_reason"))
                _log_call(
                    "question",
                    tag,
                    question,
                    spent | {"tools": called},
                    svc.settings.openrouter_model,
                    time.monotonic() - started,
                )
                if not text:
                    log.warning("empty model reply (finish=%s)", choice.get("finish_reason"))
                    yield {"type": "error", "message": "I lost my train of thought. Ask me again."}
                    return
                answer = {
                    "type": "answer",
                    "text": text,
                    "left": left,
                    "evidence": evidence,
                    "visuals": visuals,
                }
                if svc.settings.dev:
                    elapsed = round(time.monotonic() - started, 2)
                    answer["usage"] = spent | {
                        "model": svc.settings.openrouter_model,
                        "seconds": elapsed,
                    }
                yield answer
                return
            messages.append(msg)
            for call in calls:
                name = call["function"]["name"]
                called.append(name)
                label = labels.get(name, ("the numbers", ""))[0]
                yield {"type": "status", "message": f"Checking {label}…"}
                if name not in tools:
                    result: Any = {"error": "unknown tool"}
                else:
                    args = json.loads(call["function"].get("arguments") or "{}")
                    async with svc.lock:
                        try:
                            result = await asyncio.to_thread(tools[name], args)
                        except ValueError as e:
                            result = {"error": str(e)}
                if (fact := voice.evidence(name, result)) and fact not in evidence:
                    evidence.append(fact)
                is_visual = isinstance(result, dict) and "type" in result and "error" not in result
                if name.startswith("show_") and is_visual and len(visuals) < MAX_VISUALS:
                    visuals.append(result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(without_art(result), default=str),
                    }
                )
    yield {"type": "error", "message": "I went down a rabbit hole. Ask me that a simpler way."}


async def suggest_questions(svc: CoachContext, tag: str, body: SuggestRequest) -> dict[str, Any]:
    """Follow-up questions from the model. On any failure, the UI keeps the built-in ones."""
    a = svc.analyzer

    def facts() -> dict[str, Any]:
        return insights.overview(a, tag, insights.games(a, tag))

    try:
        async with svc.lock:
            o = await asyncio.to_thread(facts)
        record, deck = o["record"], ", ".join(c["name"] for c in o["deck"])
        talk = "\n".join(f"{t.role}: {t.content}" for t in body.history)
        context = (
            f"Deck: {deck}\nRecent record: {record['wins']} wins, {record['losses']} losses\n"
            f"Already asked or on offer: {'; '.join(body.asked) or 'nothing'}\n"
            f"Conversation:\n{talk}"
        )
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=30, transport=svc.transport) as http:
            resp = await http.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {svc.settings.openrouter_key}"},
                json={
                    "model": svc.settings.openrouter_model,
                    "messages": [
                        {"role": "system", "content": prompts.suggestions_prompt()},
                        {"role": "user", "content": context},
                    ],
                    "max_tokens": 700,
                    "reasoning": {"effort": "low"},
                    "usage": {"include": True},
                },
            )
        resp.raise_for_status()
        payload = resp.json()
        text = payload["choices"][0]["message"].get("content")
    except httpx.HTTPError, KeyError, ValueError, LookupError:
        log.warning("could not suggest questions for %s", tag, exc_info=True)
        return {"suggestions": []}
    result: dict[str, Any] = {"suggestions": parse_suggestions(text, body.asked)}
    used = payload.get("usage") or {}
    seconds = round(time.monotonic() - started, 2)
    usage = {
        "model": svc.settings.openrouter_model,
        "rounds": 1,
        "input_tokens": used.get("prompt_tokens") or 0,
        "output_tokens": used.get("completion_tokens") or 0,
        "cost_usd": used.get("cost") or 0.0,
        "seconds": seconds,
    }
    _log_call("suggestions", tag, " / ".join(result["suggestions"]), usage, usage["model"], seconds)
    if svc.settings.dev:
        result["usage"] = usage
    return result
