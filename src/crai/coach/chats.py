"""Saved conversations with Rook, so a bad answer can be read back and fixed later.

Each custom question is one row: the question, the earlier turns the page sent, every message
the model and its tools exchanged (minus the system prompt), and how it ended. Rows live in
`app.db` next to the usage counters, never in the battle database.
"""

import json
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KEEP_DAYS = 90

SCHEMA = """CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    tag TEXT NOT NULL,
    question TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reply TEXT,
    model TEXT,
    cost_usd REAL,
    seconds REAL,
    history TEXT NOT NULL,
    transcript TEXT NOT NULL,
    final TEXT
)"""


class ChatLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as c:
            c.execute(SCHEMA)
            c.execute("CREATE INDEX IF NOT EXISTS chats_tag ON chats (tag, at)")
            c.commit()

    def save(self, chat: dict[str, Any]) -> int | None:
        """Store one finished question. A failure here is logged and never reaches the visitor."""
        final = chat.get("final") or {}
        now = datetime.now(UTC)
        row = (
            now.isoformat(timespec="seconds"),
            chat["tag"],
            chat["question"],
            final.get("type") or "unfinished",
            final.get("text") or final.get("message"),
            chat.get("model"),
            chat.get("cost_usd"),
            chat.get("seconds"),
            json.dumps(chat.get("history", []), default=str),
            json.dumps(chat.get("transcript", []), default=str),
            json.dumps(final, default=str) if final else None,
        )
        try:
            with closing(sqlite3.connect(self.path, timeout=30)) as c:
                cur = c.execute(
                    """INSERT INTO chats (at, tag, question, outcome, reply, model, cost_usd,
                       seconds, history, transcript, final) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    row,
                )
                cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
                c.execute("DELETE FROM chats WHERE at < ?", (cutoff,))
                c.commit()
                return cur.lastrowid
        except sqlite3.Error:
            log.exception("could not save chat for %s", chat["tag"])
            return None

    def recent(
        self, tag: str | None = None, *, failed: bool = False, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Newest first: one line per chat, without the transcript."""
        where, args = ["1 = 1"], []
        if tag:
            where.append("tag = ?")
            args.append(tag)
        if failed:
            where.append("outcome != 'answer'")
        sql = (
            "SELECT id, at, tag, question, outcome, reply, cost_usd, seconds FROM chats WHERE "
            + " AND ".join(where)
            + " ORDER BY id DESC LIMIT ?"
        )
        with closing(sqlite3.connect(self.path, timeout=30)) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql, (*args, limit))]

    def get(self, chat_id: int) -> dict[str, Any] | None:
        """One chat in full, with its history, transcript and final event decoded."""
        with closing(sqlite3.connect(self.path, timeout=30)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if row is None:
            return None
        chat = dict(row)
        for key in ("history", "transcript", "final"):
            chat[key] = json.loads(chat[key]) if chat[key] else None
        return chat
