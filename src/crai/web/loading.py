"""The player-loading stream: every step reports status, so the page is never idle."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from crai.coach import voice
from crai.collect.client import ClashAPIError, normalize_tag
from crai.timing import Laps
from crai.web.service import FRESH_SECONDS, HEARTBEAT_SECONDS, Service

log = logging.getLogger(__name__)

WARM_UP_SECONDS = 25  # about how long the NAS takes to load everyone's battles


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


async def with_heartbeat[T](
    work: Awaitable[T],
    emit: Callable[[dict[str, Any]], Awaitable[None]],
    step: str,
    seed: int,
    lines: tuple[str, ...] = voice.LOADING_LINES,
) -> T:
    """Await `work`, emitting a fresh status line whenever it takes a while."""
    task = asyncio.ensure_future(work)
    i = seed
    while True:
        done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
        if done:
            return task.result()
        i += 1
        if not lines:
            continue
        await emit(
            {
                "type": "status",
                "step": step,
                "message": lines[i % len(lines)],
            }
        )


async def load_player(svc: Service, raw_tag: str, them: bool = False) -> AsyncIterator[str]:
    """Stream a player's report. `them` means the viewer is looking someone else up."""
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def emit(event: dict[str, Any]) -> None:
        await queue.put(event)

    laps = Laps()
    outcome = "cancelled"  # until it finishes one way or another

    async def wake() -> None:
        """Wait out the startup warm-up, if it's still running, saying so plainly.

        The bar creeps forward over the usual warm-up time, and the line changes every few
        seconds (slow enough to read) instead of on every heartbeat.
        """
        warming = svc.warming
        if warming is None or warming.done():
            return
        started = time.monotonic()
        with laps.lap("wake"):
            message, beats = voice.WAKING_UP, 0
            while True:
                elapsed = time.monotonic() - started
                progress = 25 + round(
                    20 * min(1.0, elapsed / WARM_UP_SECONDS)
                )  # up to the next step's 48
                await emit(
                    {"type": "status", "step": "wake", "progress": progress, "message": message}
                )
                done, _ = await asyncio.wait({warming}, timeout=HEARTBEAT_SECONDS)
                if done:
                    return
                beats += 1
                if beats % 3 == 0:
                    message = voice.WAKING_LINES[(beats // 3 - 1) % len(voice.WAKING_LINES)]

    async def run() -> None:
        nonlocal outcome
        try:
            try:
                tag = normalize_tag(raw_tag)
            except ValueError:
                outcome = "bad tag"
                await emit(
                    {
                        "type": "error",
                        "code": "bad_tag",
                        "message": "That's not a real tag. Check it in your profile.",
                    }
                )
                return
            await emit(
                {
                    "type": "status",
                    "step": "lookup",
                    "progress": 8,
                    "message": "Looking them up…" if them else "Looking you up…",
                }
            )
            with laps.lap("lookup"):
                known = await asyncio.to_thread(svc.known, tag)
            fresh = time.monotonic() - svc.fetched_at.get(tag, -1e9) < FRESH_SECONDS
            if not fresh:
                if them:
                    intro = (
                        "Pulling their latest battles…"
                        if known
                        else "Never seen them before. Fetching their battles…"
                    )
                else:
                    intro = (
                        "Pulling your latest battles…"
                        if known
                        else "Never seen you before. Fetching your whole story…"
                    )
                lines = voice.SCOUT_LINES if them else voice.LOADING_LINES
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

                with laps.lap("fetch"):
                    profile, battles = await with_heartbeat(fetch(), emit, "fetch", 0, lines)
                if profile is None:
                    outcome = "not found"
                    await emit(
                        {
                            "type": "error",
                            "code": "not_found",
                            "message": "No player with that tag. Typo?",
                        }
                    )
                    return
                await wake()  # the warm-up holds the snapshot while it reads it
                await emit(
                    {
                        "type": "status",
                        "step": "store",
                        "progress": 48,
                        "message": f"Reading {len(battles)} battles…",
                    }
                )
                with laps.lap("wait"):
                    await svc.lock.acquire()
                try:
                    with laps.lap("store"):
                        await with_heartbeat(
                            asyncio.to_thread(svc.store, profile, battles, tag),
                            emit,
                            "store",
                            2,
                            lines,
                        )
                finally:
                    svc.lock.release()
                svc.fetched_at[tag] = time.monotonic()
            await wake()
            await emit(
                {
                    "type": "status",
                    "step": "analyze",
                    "progress": 74,
                    "message": "Comparing them with everyone else…"
                    if them
                    else "Comparing you with everyone else…",
                }
            )
            with laps.lap("wait"):
                await svc.lock.acquire()
            loop = asyncio.get_running_loop()

            def on_step(name: str) -> None:  # runs in the report's thread
                steps = voice.SCOUT_STEPS if them else voice.REPORT_STEPS
                if name in steps:
                    progress, message = steps[name]
                    event = {
                        "type": "status",
                        "step": name,
                        "progress": progress,
                        "message": message,
                    }
                    loop.call_soon_threadsafe(queue.put_nowait, event)

            try:
                with laps.lap("analyze"):
                    # Each section says what it's doing, so no filler lines in between.
                    report = await with_heartbeat(
                        asyncio.to_thread(svc.report, tag, on_step), emit, "analyze", 4, ()
                    )
            finally:
                svc.lock.release()
            outcome = "ok"
            await emit({"type": "status", "step": "done", "progress": 100, "message": "Got it."})
            await emit({"type": "ready", "report": report})
        except ClashAPIError as e:
            outcome = "api error"
            log.warning("API error loading %s: %s", raw_tag, e)
            await emit(
                {
                    "type": "error",
                    "code": "api",
                    "message": "The game's servers aren't talking to me. Try again in a minute.",
                }
            )
        except Exception:
            outcome = "failed"
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
        log.info("load %s %s in %.2fs (%s)", raw_tag, outcome, laps.total, laps)
