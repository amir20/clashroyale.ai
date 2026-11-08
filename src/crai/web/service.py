"""Shared state behind the routes: building reports, answering questions, and usage limits."""

import asyncio
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from crai.analysis import insights
from crai.analysis.analytics import Analyzer
from crai.coach import voice
from crai.collect.client import ClashClient
from crai.config import Settings
from crai.storage import database as db
from crai.storage import snapshot

FRESH_SECONDS = 600
DAILY_QUESTIONS = 5
HEARTBEAT_SECONDS = 1.6
LOADS_PER_MINUTE = 20
QUESTIONS = {
    "replace_card": "Which card should I replace?",
    "best_cards": "Which of my cards do I play better than other people?",
    "nemesis": "Which cards beat me the most?",
    "best_deck": "What's my best deck?",
}

type QuestionId = Literal["replace_card", "best_cards", "nemesis", "best_deck"]


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
            "intro": voice.intro(overview),
            "similar": similar,
            "patterns": patterns,
            "patterns_line": voice.wins_and_losses(patterns),
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
                voice.replace_card,
                "which_card_to_replace",
            )
        elif question == "best_cards":
            data, line, tool = (
                insights.best_cards(a, items),
                voice.best_cards,
                "cards_vs_other_players",
            )
        elif question == "nemesis":
            data, line, tool = (
                insights.nemesis(a, items),
                voice.nemesis,
                "opponent_cards_that_beat_me",
            )
        else:
            data, line, tool = insights.best_deck(a, items), voice.best_deck, "my_decks_compared"
        return {"data": data, "line": line(data), "evidence": [voice.evidence(tool, data)]}
