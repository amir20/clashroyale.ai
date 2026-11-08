"""FastAPI backend for the Rook web app.

`GET /api/players/{tag}/load` is a Server-Sent Events stream: a never-seen player is fetched live,
and every step reports status, with a heartbeat during slow steps, so the UI is never idle.
Pre-filled questions are computed answers (no AI cost). Custom questions go to OpenRouter with a
daily limit.
"""

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import coach, db, insights, snapshot
from .analytics import Analyzer, records
from .api import ClashAPIError, ClashClient, normalize_tag
from .config import PROJECT_ROOT, Settings, get_settings

log = logging.getLogger(__name__)

FRESH_SECONDS = 600
DAILY_QUESTIONS = 5
HEARTBEAT_SECONDS = 1.6
LOADS_PER_MINUTE = 20
MAX_TOOL_ROUNDS = 4
MAX_OUTPUT_TOKENS = 1200
MAX_HISTORY = 12  # messages of earlier conversation sent back to the model
DAILY_SUGGESTIONS = 40
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

QUESTIONS = {
    "replace_card": "Which card should I replace?",
    "best_cards": "Which of my cards do I play better than other people?",
    "nemesis": "Which cards beat me the most?",
    "best_deck": "What's my best deck?",
}

type QuestionId = Literal["replace_card", "best_cards", "nemesis", "best_deck"]


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=2000)


class Ask(BaseModel):
    question: str = Field(min_length=3, max_length=300)
    history: list[Turn] = Field(default_factory=list, max_length=MAX_HISTORY)


class SuggestRequest(BaseModel):
    history: list[Turn] = Field(default_factory=list, max_length=MAX_HISTORY)
    asked: list[str] = Field(default_factory=list, max_length=40)


class Usage:
    """Per-day counters for custom AI questions, in their own small database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS usage
                   (day TEXT, who TEXT, n INTEGER, PRIMARY KEY (day, who))"""
            )
            c.commit()

    def take(self, who: str, limit: int) -> int | None:
        """Consume one question. Returns how many are left today, or None if none remain."""
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        with closing(sqlite3.connect(self.path, timeout=30)) as c:
            row = c.execute("SELECT n FROM usage WHERE day = ? AND who = ?", (day, who)).fetchone()
            used = row[0] if row else 0
            if used >= limit:
                return None
            c.execute(
                """INSERT INTO usage VALUES (?, ?, 1)
                   ON CONFLICT(day, who) DO UPDATE SET n = n + 1""",
                (day, who),
            )
            c.commit()
        return limit - used - 1


class RateLimiter:
    """Sliding-window limit per client, so one visitor can't burn the API keys."""

    def __init__(self, limit: int, seconds: float) -> None:
        self.limit, self.seconds = limit, seconds
        self.hits: defaultdict[str, deque[float]] = defaultdict(deque)

    def allow(self, who: str) -> bool:
        now, hits = time.monotonic(), self.hits[who]
        while hits and now - hits[0] > self.seconds:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


class Service:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None) -> None:
        self.settings = settings
        self.transport = transport
        self.analyzer = Analyzer(settings.db_path, settings.analytics_days)
        self.lock = asyncio.Lock()  # one writer and one analytics load at a time
        self.usage = Usage(settings.db_path.with_name("app.db"))
        self.fetched_at: dict[str, float] = {}
        self.loads = RateLimiter(LOADS_PER_MINUTE, 60)

    def client(self) -> ClashClient:
        return ClashClient(
            self.settings.api_tokens, self.settings.api_base, transport=self.transport
        )

    def known(self, tag: str) -> bool:
        if not self.settings.db_path.exists():
            return False
        with db.session(self.settings.db_path, read_only=True) as conn:
            row = conn.execute(
                "SELECT profile_fetched_at FROM players WHERE tag = ?", (tag,)
            ).fetchone()
        return bool(row and row[0])

    def store(self, profile: Any, battles: list[Any], tag: str) -> int:
        with db.session(self.settings.db_path) as conn:
            db.upsert_player(conn, profile)
            new = db.ingest_battlelog(conn, battles)
            db.mark_battlelog_fetched(conn, tag)
        snapshot.sync(self.settings.db_path)
        return new

    def report(self, tag: str) -> dict[str, Any]:
        items = insights.games(self.analyzer, tag)
        overview = insights.overview(self.analyzer, tag, items)
        patterns = insights.wins_and_losses(items)
        similar = insights.similar_battles(self.analyzer, tag, items) if items else None
        return {
            "overview": overview,
            "intro": coach.intro(overview),
            "similar": similar,
            "patterns": patterns,
            "patterns_line": coach.wins_and_losses(patterns),
            "questions": [{"id": k, "label": v} for k, v in QUESTIONS.items()],
            "custom_questions_per_day": DAILY_QUESTIONS,
            "ai_enabled": bool(self.settings.openrouter_key),
            "dev": self.settings.dev,
        }

    def answer(self, tag: str, question: QuestionId) -> dict[str, Any]:
        a, items = self.analyzer, insights.games(self.analyzer, tag)
        if question == "replace_card":
            data, line, tool = (
                insights.replace_card(a, tag, items),
                coach.replace_card,
                "which_card_to_replace",
            )
        elif question == "best_cards":
            data, line, tool = (
                insights.best_cards(a, items),
                coach.best_cards,
                "cards_vs_other_players",
            )
        elif question == "nemesis":
            data, line, tool = (
                insights.nemesis(a, items),
                coach.nemesis,
                "opponent_cards_that_beat_me",
            )
        else:
            data, line, tool = insights.best_deck(a, items), coach.best_deck, "my_decks_compared"
        return {"data": data, "line": line(data), "evidence": [coach.evidence(tool, data)]}


async def with_heartbeat[T](
    work: Awaitable[T], emit: Callable[[dict[str, Any]], Awaitable[None]], step: str, seed: int
) -> T:
    """Await `work`, emitting a fresh status line whenever it takes a while."""
    task = asyncio.ensure_future(work)
    i = seed
    while True:
        done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
        if done:
            return task.result()
        i += 1
        await emit(
            {
                "type": "status",
                "step": step,
                "message": coach.LOADING_LINES[i % len(coach.LOADING_LINES)],
            }
        )


async def load_player(svc: Service, raw_tag: str) -> AsyncIterator[str]:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def emit(event: dict[str, Any]) -> None:
        await queue.put(event)

    async def run() -> None:
        try:
            try:
                tag = normalize_tag(raw_tag)
            except ValueError:
                await emit(
                    {
                        "type": "error",
                        "code": "bad_tag",
                        "message": "That's not a real tag. Check it in your profile.",
                    }
                )
                return
            await emit(
                {"type": "status", "step": "lookup", "progress": 8, "message": "Looking you up…"}
            )
            known = await asyncio.to_thread(svc.known, tag)
            fresh = time.monotonic() - svc.fetched_at.get(tag, -1e9) < FRESH_SECONDS
            if not fresh:
                intro = (
                    "Pulling your latest battles…"
                    if known
                    else "Never seen you before. Fetching your whole story…"
                )
                await emit(
                    {
                        "type": "status",
                        "step": "fetch",
                        "progress": 22,
                        "known": known,
                        "message": intro,
                    }
                )

                async def fetch() -> tuple[Any, list[Any]]:
                    async with svc.client() as c:
                        return await asyncio.gather(c.player(tag), c.battlelog(tag))

                profile, battles = await with_heartbeat(fetch(), emit, "fetch", 0)
                if profile is None:
                    await emit(
                        {
                            "type": "error",
                            "code": "not_found",
                            "message": "No player with that tag. Typo?",
                        }
                    )
                    return
                await emit(
                    {
                        "type": "status",
                        "step": "store",
                        "progress": 48,
                        "message": f"Reading {len(battles)} battles…",
                    }
                )
                async with svc.lock:
                    await with_heartbeat(
                        asyncio.to_thread(svc.store, profile, battles, tag), emit, "store", 2
                    )
                svc.fetched_at[tag] = time.monotonic()
            await emit(
                {
                    "type": "status",
                    "step": "analyze",
                    "progress": 74,
                    "message": "Comparing you with everyone else…",
                }
            )
            async with svc.lock:
                report = await with_heartbeat(
                    asyncio.to_thread(svc.report, tag), emit, "analyze", 4
                )
            await emit({"type": "status", "step": "done", "progress": 100, "message": "Got it."})
            await emit({"type": "ready", "report": report})
        except ClashAPIError as e:
            log.warning("API error loading %s: %s", raw_tag, e)
            await emit(
                {
                    "type": "error",
                    "code": "api",
                    "message": "The game's servers aren't talking to me. Try again in a minute.",
                }
            )
        except Exception:
            log.exception("failed loading %s", raw_tag)
            await emit(
                {
                    "type": "error",
                    "code": "internal",
                    "message": "I tripped over something on my end. Try again.",
                }
            )
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())
    try:
        while (event := await queue.get()) is not None:
            yield sse(event)
    finally:
        task.cancel()


def _tools(svc: Service, tag: str) -> dict[str, Callable[[dict[str, Any]], Any]]:
    a = svc.analyzer

    def items() -> list[insights.Game]:
        return insights.games(a, tag)

    return {
        "get_overview": lambda _: insights.overview(a, tag, items()),
        "get_patterns": lambda _: insights.wins_and_losses(items()),
        "which_card_to_replace": lambda _: insights.replace_card(a, tag, items()),
        "cards_vs_other_players": lambda _: insights.best_cards(a, items()),
        "opponent_cards_that_beat_me": lambda _: insights.nemesis(a, items()),
        "my_decks_compared": lambda _: insights.best_deck(a, items()),
        "my_record_against_card": lambda args: insights.record_against(a, items(), args["card"]),
        "best_partners_for_card": lambda args: records(
            a.card_pairs(args["card"], min_games=50, sort_by="adj_win_rate")[1], 10
        ),
        "counters_for_card": lambda args: records(
            a.card_matchups(args["card"], min_games=50)[1], 10
        ),
        "top_decks_with_cards": lambda args: a.top_decks(
            include=args.get("cards", []), min_games=30, limit=5
        ),
    }


def _finished_text(content: str | None, finish_reason: str | None) -> str:
    """The reply to show. One cut off by the token limit is trimmed to its last sentence."""
    text = (content or "").strip()
    if finish_reason != "length":
        return text
    end = max(text.rfind(mark) for mark in ".!?")
    return text[: end + 1] if end >= 0 else ""


TOOL_PARAMS: dict[str, dict[str, Any]] = {
    "my_record_against_card": {"card": {"type": "string"}},
    "best_partners_for_card": {"card": {"type": "string"}},
    "counters_for_card": {"card": {"type": "string"}},
    "top_decks_with_cards": {"cards": {"type": "array", "items": {"type": "string"}}},
}


def _tool_schema() -> list[dict[str, Any]]:
    schema = []
    for name, (_, description) in coach.tool_prompts().items():
        props = TOOL_PARAMS.get(name, {})
        parameters = {"type": "object", "properties": props, "required": list(props)}
        function = {"name": name, "description": description, "parameters": parameters}
        schema.append({"type": "function", "function": function})
    return schema


async def ask_coach(
    svc: Service, tag: str, question: str, left: int, history: list[Turn]
) -> AsyncIterator[str]:
    """Answer a custom question. Every path ends in an `answer` or `error` event."""
    try:
        async for event in _ask_coach(svc, tag, question, left, history):
            yield event
    except Exception:
        log.exception("custom question failed for %s", tag)
        yield sse({"type": "error", "message": "My brain glitched. Try asking that again."})


async def _ask_coach(
    svc: Service, tag: str, question: str, left: int, history: list[Turn]
) -> AsyncIterator[str]:
    yield sse({"type": "status", "message": "Rook is thinking…"})
    tools = _tools(svc, tag)
    labels = coach.tool_prompts()
    started = time.monotonic()
    spent: dict[str, Any] = {"rounds": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": coach.system_prompt()},
        *[t.model_dump() for t in history],
        {"role": "user", "content": question},
    ]
    headers = {"Authorization": f"Bearer {svc.settings.openrouter_key}"}
    async with httpx.AsyncClient(timeout=60, transport=svc.transport) as http:
        evidence: list[str] = []
        for attempt in range(MAX_TOOL_ROUNDS + 1):
            resp = await http.post(
                OPENROUTER_URL,
                headers=headers,
                json={
                    "model": svc.settings.openrouter_model,
                    "messages": messages,
                    "tools": _tool_schema(),
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
                if not text:
                    log.warning("empty model reply (finish=%s)", choice.get("finish_reason"))
                    yield sse(
                        {"type": "error", "message": "I lost my train of thought. Ask me again."}
                    )
                    return
                answer = {
                    "type": "answer",
                    "text": text,
                    "left": left,
                    "evidence": evidence,
                }
                if svc.settings.dev:
                    elapsed = round(time.monotonic() - started, 2)
                    answer["usage"] = spent | {
                        "model": svc.settings.openrouter_model,
                        "seconds": elapsed,
                    }
                yield sse(answer)
                return
            messages.append(msg)
            for call in calls:
                name = call["function"]["name"]
                label = labels.get(name, ("the numbers", ""))[0]
                yield sse({"type": "status", "message": f"Checking {label}…"})
                if name not in tools:
                    result: Any = {"error": "unknown tool"}
                else:
                    args = json.loads(call["function"].get("arguments") or "{}")
                    async with svc.lock:
                        try:
                            result = await asyncio.to_thread(tools[name], args)
                        except ValueError as e:
                            result = {"error": str(e)}
                if (fact := coach.evidence(name, result)) and fact not in evidence:
                    evidence.append(fact)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result, default=str),
                    }
                )
    yield sse({"type": "error", "message": "I went down a rabbit hole. Ask me that a simpler way."})


async def suggest_questions(svc: Service, tag: str, body: SuggestRequest) -> dict[str, Any]:
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
                        {"role": "system", "content": coach.suggestions_prompt()},
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
    result: dict[str, Any] = {"suggestions": coach.parse_suggestions(text, body.asked)}
    if svc.settings.dev:
        used = payload.get("usage") or {}
        result["usage"] = {
            "model": svc.settings.openrouter_model,
            "rounds": 1,
            "input_tokens": used.get("prompt_tokens") or 0,
            "output_tokens": used.get("completion_tokens") or 0,
            "cost_usd": used.get("cost") or 0.0,
            "seconds": round(time.monotonic() - started, 2),
        }
    return result


def create_app(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    svc = Service(settings or get_settings(), transport)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        # Warm the analytics cache so the first visitor isn't the one who waits for it.
        if svc.settings.db_path.exists():
            app.state.warm = asyncio.create_task(asyncio.to_thread(svc.analyzer.decks))
        yield

    app = FastAPI(title="Rook", lifespan=lifespan)
    app.state.service = svc
    stream_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    @app.get("/api/players/{tag}/load")
    async def load(tag: str, request: Request) -> StreamingResponse:
        ip = request.client.host if request.client else "unknown"
        if not svc.settings.dev and not svc.loads.allow(ip):
            busy = sse(
                {
                    "type": "error",
                    "code": "rate_limited",
                    "message": "Easy, slow down. Try again in a minute.",
                }
            )

            async def refuse() -> AsyncIterator[str]:
                yield busy

            return StreamingResponse(
                refuse(), media_type="text/event-stream", headers=stream_headers
            )
        return StreamingResponse(
            load_player(svc, tag), media_type="text/event-stream", headers=stream_headers
        )

    @app.get("/api/players/{tag}/questions/{question}")
    async def question(tag: str, question: QuestionId) -> dict[str, Any]:
        try:
            normalized = normalize_tag(tag)
        except ValueError as e:
            raise HTTPException(400, "bad tag") from e
        started = time.monotonic()
        async with svc.lock:
            result = await asyncio.to_thread(svc.answer, normalized, question)
        if svc.settings.dev:  # pre-filled answers are computed, so they never spend AI money
            elapsed = round(time.monotonic() - started, 2)
            result["usage"] = {"model": None, "cost_usd": 0.0, "seconds": elapsed}
        return result

    @app.post("/api/players/{tag}/ask")
    async def ask(tag: str, body: Ask, request: Request) -> StreamingResponse:
        try:
            normalized = normalize_tag(tag)
        except ValueError as e:
            raise HTTPException(400, "bad tag") from e
        if not svc.settings.openrouter_key:
            raise HTTPException(503, "ai_disabled")
        ip = request.client.host if request.client else "unknown"
        who = hashlib.sha256(ip.encode()).hexdigest()
        left = DAILY_QUESTIONS if svc.settings.dev else svc.usage.take(who, DAILY_QUESTIONS)
        if left is None:
            raise HTTPException(429, "daily_limit")
        return StreamingResponse(
            ask_coach(svc, normalized, body.question, left, body.history),
            media_type="text/event-stream",
            headers=stream_headers,
        )

    @app.post("/api/players/{tag}/suggestions")
    async def suggestions(tag: str, body: SuggestRequest, request: Request) -> dict[str, Any]:
        try:
            normalized = normalize_tag(tag)
        except ValueError as e:
            raise HTTPException(400, "bad tag") from e
        if not svc.settings.openrouter_key:
            return {"suggestions": []}
        ip = request.client.host if request.client else "unknown"
        who = hashlib.sha256(f"suggest:{ip}".encode()).hexdigest()
        if not svc.settings.dev and svc.usage.take(who, DAILY_SUGGESTIONS) is None:
            return {"suggestions": []}
        return await suggest_questions(svc, normalized, body)

    dist = PROJECT_ROOT / "web" / "dist"
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            target = (dist / path).resolve()
            if path and target.is_file() and target.is_relative_to(dist.resolve()):
                return FileResponse(target)
            return FileResponse(dist / "index.html")

    return app
