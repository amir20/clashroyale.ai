"""Saved conversations with Rook, so bad answers can be read back and Rook improved later.

Everything that shaped an answer is kept:

- the question, the earlier turns the page sent, and who asked (a hashed IP, the browser)
- every message the model and its tools exchanged, and each tool result in full (images too)
- every request sent to OpenRouter (minus the messages, which are in the transcript) and the
  raw response, with its id, provider, finish reason, reasoning and usage
- each step with its timing and how much data it returned, as the page saw it
- the system prompt and tool list, stored once per version in `prompts` and linked by hash
- the reply the visitor saw, and their thumbs up or down with an optional note

Pre-filled questions and follow-up suggestions are saved too, under their own `kind`. Rows live
in `app.db` next to the usage counters, never in the battle database, and go after KEEP_DAYS.
"""

import hashlib
import json
import logging
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

KEEP_DAYS = 180

type ChatKind = Literal["question", "preset", "suggestions"]

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY,
        ref TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        at TEXT NOT NULL,
        tag TEXT NOT NULL,
        who TEXT,
        user_agent TEXT,
        question TEXT NOT NULL,
        outcome TEXT NOT NULL,
        reply TEXT,
        model TEXT,
        system_prompt TEXT,
        tools TEXT,
        rounds INTEGER,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cost_usd REAL,
        seconds REAL,
        history TEXT NOT NULL,
        transcript TEXT NOT NULL,
        requests TEXT NOT NULL,
        steps TEXT NOT NULL,
        tool_results TEXT NOT NULL,
        final TEXT,
        rating INTEGER,
        note TEXT,
        rated_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS chats_tag ON chats (tag, at)",
    "CREATE INDEX IF NOT EXISTS chats_rating ON chats (rating) WHERE rating IS NOT NULL",
    """CREATE TABLE IF NOT EXISTS prompts (
        hash TEXT PRIMARY KEY,
        text TEXT NOT NULL,
        first_seen TEXT NOT NULL
    )""",
)
JSON_COLUMNS = ("history", "transcript", "requests", "steps", "tool_results", "final")
SUMMARY = "id, ref, kind, at, tag, question, outcome, rating, reply, cost_usd, seconds"


def new_chat(kind: ChatKind, tag: str, question: str, client: dict[str, Any]) -> dict[str, Any]:
    """An empty record that the code answering the question fills in as it goes."""
    return {
        "ref": uuid.uuid4().hex,
        "kind": kind,
        "tag": tag,
        "question": question,
        "who": client.get("who"),
        "user_agent": client.get("user_agent"),
        "history": [],
        "transcript": [],
        "requests": [],
        "steps": [],
        "tool_results": [],
    }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class ChatLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as c:
            for statement in SCHEMA:
                c.execute(statement)
            c.commit()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _prompt(self, c: sqlite3.Connection, text: str | None) -> str | None:
        """Store a prompt once and return its hash, so a chat can name the exact version."""
        if text is None:
            return None
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        c.execute("INSERT OR IGNORE INTO prompts VALUES (?, ?, ?)", (digest, text, _now()))
        return digest

    def save(self, chat: dict[str, Any]) -> None:
        """Store one finished exchange. A failure is logged and never reaches the visitor."""
        final = chat.get("final") or {}
        spent = chat.get("spent") or {}
        try:
            with closing(self._connect()) as c:
                row = {
                    "ref": chat["ref"],
                    "kind": chat["kind"],
                    "at": chat.get("at") or _now(),
                    "tag": chat["tag"],
                    "who": chat.get("who"),
                    "user_agent": chat.get("user_agent"),
                    "question": chat["question"],
                    "outcome": final.get("type") or "unfinished",
                    "reply": final.get("text") or final.get("line") or final.get("message"),
                    "model": chat.get("model"),
                    "system_prompt": self._prompt(c, chat.get("system_prompt")),
                    "tools": self._prompt(c, chat.get("tools")),
                    "rounds": spent.get("rounds"),
                    "input_tokens": spent.get("input_tokens"),
                    "output_tokens": spent.get("output_tokens"),
                    "cost_usd": spent.get("cost_usd"),
                    "seconds": chat.get("seconds"),
                } | {k: json.dumps(chat.get(k) or [], default=str) for k in JSON_COLUMNS[:-1]}
                row["final"] = json.dumps(final, default=str) if final else None
                c.execute(
                    f"INSERT INTO chats ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                    tuple(row.values()),
                )
                cutoff = datetime.now(UTC) - timedelta(days=KEEP_DAYS)
                c.execute("DELETE FROM chats WHERE at < ?", (cutoff.isoformat(),))
                c.commit()
        except sqlite3.Error, TypeError, ValueError:
            log.exception("could not save chat for %s", chat.get("tag"))

    def rate(self, ref: str, rating: int, note: str | None) -> bool:
        """A visitor's thumbs up (1) or down (-1) on an answer. False if there's no such chat."""
        with closing(self._connect()) as c:
            cur = c.execute(
                "UPDATE chats SET rating = ?, note = ?, rated_at = ? WHERE ref = ?",
                (rating, note, _now(), ref),
            )
            c.commit()
        return cur.rowcount > 0

    def recent(
        self,
        tag: str | None = None,
        *,
        failed: bool = False,
        disliked: bool = False,
        kind: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Newest first: one line per chat, without the transcript."""
        where: list[str] = []
        args: list[Any] = []
        if tag:
            where.append("tag = ?")
            args.append(tag)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if failed:
            where.append("outcome != 'answer'")
        if disliked:
            where.append("rating < 0")
        sql = f"SELECT {SUMMARY} FROM chats"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with closing(self._connect()) as c:
            rows = c.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
            return [dict(r) for r in rows]

    def get(self, chat_id: int | str) -> dict[str, Any] | None:
        """One chat in full, by number or ref, with its prompts and JSON columns decoded."""
        column = "id" if isinstance(chat_id, int) else "ref"
        with closing(self._connect()) as c:
            row = c.execute(f"SELECT * FROM chats WHERE {column} = ?", (chat_id,)).fetchone()
            if row is None:
                return None
            chat = dict(row)
            for key in ("system_prompt", "tools"):
                if chat[key]:
                    found = c.execute("SELECT text FROM prompts WHERE hash = ?", (chat[key],))
                    chat[f"{key}_text"] = (found.fetchone() or [None])[0]
        for key in JSON_COLUMNS:
            chat[key] = json.loads(chat[key]) if chat[key] else None
        return chat
