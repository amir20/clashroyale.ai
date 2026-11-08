"""FastAPI app: HTTP routes only. The work happens in `service`, `loading` and `crai.coach`."""

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from crai.coach.agent import Ask, SuggestRequest, ask_coach, suggest_questions
from crai.collect.client import normalize_tag
from crai.config import PROJECT_ROOT, Settings, get_settings
from crai.web.loading import load_player, sse
from crai.web.service import DAILY_QUESTIONS, QuestionId, Service

DAILY_SUGGESTIONS = 40


async def as_stream(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    async for event in events:
        yield sse(event)


def create_app(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    # No-op if the CLI already set up logging; needed in `crai dev`'s reloading worker process.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
            as_stream(ask_coach(svc, normalized, body.question, left, body.history)),
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
