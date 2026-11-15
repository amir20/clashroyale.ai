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
from crai.coach.chats import new_chat
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


def _only_swaps(visuals: list[dict[str, Any]]) -> bool:
    return bool(visuals) and all(v["type"] == "swap" for v in visuals)


def _rows(result: Any) -> int:
    """How many records a tool returned: list items, including lists one level down."""
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict):
        return sum(len(v) for v in result.values() if isinstance(v, list))
    return 0


def _ms(since: float) -> int:
    return round((time.monotonic() - since) * 1000)


async def _call_model(
    http: httpx.AsyncClient,
    headers: dict[str, str],
    payload: dict[str, Any],
    request: dict[str, Any],
) -> AsyncIterator[tuple[Literal["delta", "done"], Any]]:
    """One model call, streamed: ("delta", text) as the reply is written, then ("done", body)
    with the whole response in the usual non-streamed shape. A server that answers with
    plain JSON instead (the tests' fake) works too."""
    async with http.stream(
        "POST", OPENROUTER_URL, headers=headers, json=payload | {"stream": True}
    ) as resp:
        request["status"] = resp.status_code
        if resp.is_error:
            request["error"] = (await resp.aread()).decode(errors="replace")[:4000]
            resp.raise_for_status()
        if not resp.headers.get("content-type", "").startswith("text/event-stream"):
            yield "done", json.loads(await resp.aread())
            return
        msg: dict[str, Any] = {"role": "assistant", "content": ""}
        calls: dict[int, dict[str, Any]] = {}
        body: dict[str, Any] = {}
        finish = None
        async for line in resp.aiter_lines():
            if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                continue  # blank lines and ": OPENROUTER PROCESSING" keep-alives
            chunk = json.loads(line[5:])
            if "error" in chunk:
                raise httpx.HTTPError(f"model stream failed: {chunk['error']}")
            body |= {k: chunk[k] for k in ("id", "model", "provider", "usage") if chunk.get(k)}
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                finish = choice.get("finish_reason") or finish
                if text := delta.get("content"):
                    msg["content"] += text
                    yield "delta", text
                if reasoning := delta.get("reasoning"):
                    msg["reasoning"] = msg.get("reasoning", "") + reasoning
                for part in delta.get("tool_calls") or []:
                    call = calls.setdefault(
                        part.get("index", 0),
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    call["id"] = part.get("id") or call["id"]
                    fn = part.get("function") or {}
                    call["function"]["name"] += fn.get("name") or ""
                    call["function"]["arguments"] += fn.get("arguments") or ""
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        body["choices"] = [{"message": msg, "finish_reason": finish}]
        yield "done", body


async def ask_coach(
    svc: CoachContext,
    tag: str,
    question: str,
    left: int,
    history: list[Turn],
    client: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Answer a custom question. Every path ends in an `answer` or `error` event.

    The whole exchange is saved to `svc.chats`, however it ends, so a bad answer can be read
    back with `crai chats`. The answer carries the chat's `ref` so the visitor can rate it.
    """
    chat = new_chat("question", tag, question, client or {})
    chat["history"] = [t.model_dump() for t in history]
    chat["model"] = svc.settings.openrouter_model
    started = time.monotonic()
    try:
        async for event in _ask_coach(svc, tag, question, left, history, chat):
            if event["type"] == "step":
                chat["steps"].append(event)
            elif event["type"] in {"answer", "error"}:
                chat["final"] = event
            yield event
    except Exception as e:
        log.exception("custom question failed for %s", tag)
        error = {"type": "error", "message": "My brain glitched. Try asking that again."}
        chat["final"] = error | {"exception": repr(e)}
        yield error
    finally:
        chat["seconds"] = round(time.monotonic() - started, 2)
        chat["transcript"] = [m for m in chat["transcript"] if m.get("role") != "system"]
        svc.chats.save(chat)


async def _ask_coach(
    svc: CoachContext,
    tag: str,
    question: str,
    left: int,
    history: list[Turn],
    chat: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "status", "message": "Rook is reading your question…"}
    tools = player_tools(svc, tag)
    labels = prompts.tool_prompts()
    schema = tool_schema()
    started = time.monotonic()
    spent: dict[str, Any] = {"rounds": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    async with svc.lock:
        card_names = await asyncio.to_thread(lambda: sorted(svc.analyzer.card_names().values()))
    system = prompts.system_prompt(card_names)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        *[t.model_dump() for t in history],
        {"role": "user", "content": question},
    ]
    chat |= {
        "system_prompt": system,
        "tools": json.dumps(schema, sort_keys=True),
        "transcript": messages,  # filled as the loop runs; saved without the system prompt
        "spent": spent,
    }
    headers = {"Authorization": f"Bearer {svc.settings.openrouter_key}"}
    async with httpx.AsyncClient(timeout=60, transport=svc.transport) as http:
        evidence: list[str] = []
        called: list[str] = []
        visuals: list[dict[str, Any]] = []
        for attempt in range(MAX_TOOL_ROUNDS + 1):
            if attempt:
                yield {"type": "status", "message": "Rook is reading what it found…"}
            params = {
                "model": svc.settings.openrouter_model,
                # The first turn must look at data, so no answer is ever ungrounded.
                "tool_choice": "required" if attempt == 0 else "auto",
                # The limit includes the model's reasoning, so leave room for both.
                "max_tokens": MAX_OUTPUT_TOKENS,
                "reasoning": {"effort": "low"},
                "usage": {"include": True},
            }
            asked = time.monotonic()
            request: dict[str, Any] = {"params": params, "messages": len(messages)}
            chat["requests"].append(request)
            body: dict[str, Any] = {}
            payload = params | {"messages": messages, "tools": schema}
            async for kind, value in _call_model(http, headers, payload, request):
                if kind == "delta":
                    yield {"type": "delta", "round": attempt + 1, "text": value}
                else:
                    body = value
            request |= {"ms": _ms(asked), "response": body}
            choice = body["choices"][0]
            msg = choice["message"]
            used = body.get("usage") or {}
            spent["rounds"] += 1
            spent["input_tokens"] += used.get("prompt_tokens") or 0
            spent["output_tokens"] += used.get("completion_tokens") or 0
            spent["cost_usd"] += used.get("cost") or 0.0
            calls = msg.get("tool_calls") or []
            messages.append(msg)
            yield {
                "type": "step",
                "kind": "model",
                "round": attempt + 1,
                "ms": request["ms"],
                "at_ms": _ms(started),
                "looks_up": [
                    labels.get(c["function"]["name"], ("the numbers", ""))[0] for c in calls
                ],
                "input_tokens": used.get("prompt_tokens") or 0,
                "output_tokens": used.get("completion_tokens") or 0,
                "finish": choice.get("finish_reason"),
            }
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
                    "text": voice.tidy(text, _only_swaps(visuals)),
                    "left": left,
                    "evidence": evidence,
                    "visuals": visuals,
                    "ref": chat["ref"],
                }
                if svc.settings.dev:
                    answer["usage"] = spent | {
                        "model": svc.settings.openrouter_model,
                        "seconds": round(time.monotonic() - started, 2),
                    }
                yield answer
                return
            for call in calls:
                name = call["function"]["name"]
                called.append(name)
                label = labels.get(name, ("the numbers", ""))[0]
                yield {"type": "status", "message": f"Checking {label}…"}
                ran = time.monotonic()
                args: dict[str, Any] = {}
                if name not in tools:
                    result: Any = {"error": "unknown tool"}
                else:
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                    except ValueError:
                        result = {"error": "arguments were not valid JSON"}
                    else:
                        async with svc.lock:
                            try:
                                result = await asyncio.to_thread(tools[name], args)
                            except ValueError as e:
                                result = {"error": str(e)}
                sent = json.dumps(without_art(result), default=str)
                failed = isinstance(result, dict) and "error" in result
                if (fact := voice.evidence(name, result)) and fact not in evidence:
                    evidence.append(fact)
                is_visual = isinstance(result, dict) and "type" in result and not failed
                if (
                    name.startswith("show_")
                    and is_visual
                    and result not in visuals
                    and len(visuals) < MAX_VISUALS
                ):
                    visuals.append(result)
                yield {
                    "type": "step",
                    "kind": "tool",
                    "name": name,
                    "label": label,
                    "args": args,
                    "ms": _ms(ran),
                    "at_ms": _ms(started),
                    "bytes": len(sent.encode()),
                    "rows": _rows(result),
                    "error": result["error"] if failed else None,
                }
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": sent})
                # The full result, images included, for reading back later; never sent.
                chat["tool_results"].append({"id": call["id"], "name": name, "result": result})
    yield {"type": "error", "message": "I went down a rabbit hole. Ask me that a simpler way."}


async def suggest_questions(
    svc: CoachContext, tag: str, body: SuggestRequest, client: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Follow-up questions from the model. On any failure, the UI keeps the built-in ones."""
    a = svc.analyzer
    chat = new_chat("suggestions", tag, "(follow-up suggestions)", client or {})
    chat["history"] = [t.model_dump() for t in body.history]
    chat["model"] = svc.settings.openrouter_model
    started = time.monotonic()

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
        system = prompts.suggestions_prompt()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": context}]
        params = {
            "model": svc.settings.openrouter_model,
            "max_tokens": 700,
            "reasoning": {"effort": "low"},
            "usage": {"include": True},
        }
        chat |= {"system_prompt": system, "transcript": messages[1:]}
        async with httpx.AsyncClient(timeout=30, transport=svc.transport) as http:
            resp = await http.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {svc.settings.openrouter_key}"},
                json=params | {"messages": messages},
            )
        request: dict[str, Any] = {"params": params, "status": resp.status_code, "ms": _ms(started)}
        chat["requests"].append(request)
        resp.raise_for_status()
        payload = resp.json()
        request["response"] = payload
        message = payload["choices"][0]["message"]
        chat["transcript"].append(message)
        text = message.get("content")
    except httpx.HTTPError, KeyError, ValueError, LookupError, TypeError, AttributeError:
        log.warning("could not suggest questions for %s", tag, exc_info=True)
        chat["final"] = {"type": "error", "message": "no suggestions"}
        chat["seconds"] = round(time.monotonic() - started, 2)
        svc.chats.save(chat)
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
    text_shown = " / ".join(result["suggestions"])
    chat |= {"spent": usage, "seconds": seconds, "final": {"type": "answer", "text": text_shown}}
    chat["final"]["suggestions"] = result["suggestions"]
    svc.chats.save(chat)
    if svc.settings.dev:
        result["usage"] = usage
    return result
