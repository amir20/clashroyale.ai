"""The player-loading stream: every step reports status, so the page is never idle."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from crai.coach import voice
from crai.collect.client import ClashAPIError, normalize_tag
from crai.web.service import FRESH_SECONDS, HEARTBEAT_SECONDS, Service

log = logging.getLogger(__name__)


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


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
                "message": voice.LOADING_LINES[i % len(voice.LOADING_LINES)],
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
