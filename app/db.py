"""SQLite: история, настройки чатов, примеры и профиль стиля."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connections (
    id         TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    can_reply  INTEGER NOT NULL,
    is_enabled INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    chat_id      INTEGER PRIMARY KEY,
    title        TEXT,
    username     TEXT,
    status       TEXT NOT NULL DEFAULT 'new',   -- new | allow | block
    paused_until REAL NOT NULL DEFAULT 0,
    connection_id TEXT,
    notified     INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    from_owner INTEGER NOT NULL,
    text       TEXT NOT NULL,
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
CREATE TABLE IF NOT EXISTS examples (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    incoming TEXT,               -- что написали мне (может быть пусто)
    reply    TEXT NOT NULL,      -- что ответил я
    source   TEXT NOT NULL,      -- manual | forward | screenshot | live
    ts       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS style (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    description TEXT NOT NULL DEFAULT '',
    profile     TEXT NOT NULL DEFAULT '{}',
    summary     TEXT NOT NULL DEFAULT '',
    updated_at  REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS drafts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    connection_id TEXT NOT NULL,
    parts         TEXT NOT NULL,          -- JSON-список сообщений
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | skipped | editing
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS replies_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    mode    TEXT NOT NULL,   -- auto | draft | evasive
    ts      REAL NOT NULL
);
"""

DEFAULT_SETTINGS = {"enabled": "1", "draft_mode": "1", "collecting": "0"}


@dataclass
class Chat:
    chat_id: int
    title: str | None
    username: str | None
    status: str
    paused_until: float
    connection_id: str | None
    notified: bool


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            await self._conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
            )
        await self._conn.execute("INSERT OR IGNORE INTO style(id) VALUES (1)")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "База не подключена"
        return self._conn

    # --- настройки ---
    async def get_setting(self, key: str) -> str:
        async with self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else DEFAULT_SETTINGS.get(key, "")

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self.conn.commit()

    async def get_flag(self, key: str) -> bool:
        return await self.get_setting(key) == "1"

    async def set_flag(self, key: str, value: bool) -> None:
        await self.set_setting(key, "1" if value else "0")

    # --- бизнес-подключения ---
    async def save_connection(self, conn_id: str, user_id: int, can_reply: bool, enabled: bool) -> None:
        await self.conn.execute(
            "INSERT INTO connections(id, user_id, can_reply, is_enabled, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id, "
            "can_reply=excluded.can_reply, is_enabled=excluded.is_enabled, updated_at=excluded.updated_at",
            (conn_id, user_id, int(can_reply), int(enabled), time.time()),
        )
        await self.conn.commit()

    async def get_connection(self, conn_id: str) -> dict[str, Any] | None:
        async with self.conn.execute("SELECT * FROM connections WHERE id=?", (conn_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    # --- чаты ---
    async def upsert_chat(
        self, chat_id: int, title: str | None, username: str | None, connection_id: str | None
    ) -> Chat:
        await self.conn.execute(
            "INSERT INTO chats(chat_id, title, username, connection_id, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username, "
            "connection_id=COALESCE(excluded.connection_id, chats.connection_id), updated_at=excluded.updated_at",
            (chat_id, title, username, connection_id, time.time()),
        )
        await self.conn.commit()
        chat = await self.get_chat(chat_id)
        assert chat is not None
        return chat

    async def get_chat(self, chat_id: int) -> Chat | None:
        async with self.conn.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
        return _chat(row) if row else None

    async def find_chat_by_username(self, username: str) -> Chat | None:
        async with self.conn.execute(
            "SELECT * FROM chats WHERE lower(username)=lower(?)", (username.lstrip("@"),)
        ) as cur:
            row = await cur.fetchone()
        return _chat(row) if row else None

    async def set_chat_status(self, chat_id: int, status: str) -> None:
        await self.conn.execute(
            "INSERT INTO chats(chat_id, status, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET status=excluded.status",
            (chat_id, status, time.time()),
        )
        await self.conn.commit()

    async def mark_notified(self, chat_id: int) -> None:
        await self.conn.execute("UPDATE chats SET notified=1 WHERE chat_id=?", (chat_id,))
        await self.conn.commit()

    async def pause_chat(self, chat_id: int, until: float) -> None:
        await self.conn.execute("UPDATE chats SET paused_until=? WHERE chat_id=?", (until, chat_id))
        await self.conn.commit()

    async def recent_chats(self, limit: int = 10) -> list[Chat]:
        async with self.conn.execute(
            "SELECT * FROM chats ORDER BY updated_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [_chat(r) for r in rows]

    async def chats_by_status(self, status: str) -> list[Chat]:
        async with self.conn.execute(
            "SELECT * FROM chats WHERE status=? ORDER BY updated_at DESC", (status,)
        ) as cur:
            rows = await cur.fetchall()
        return [_chat(r) for r in rows]

    # --- история ---
    async def add_message(self, chat_id: int, from_owner: bool, text: str) -> None:
        await self.conn.execute(
            "INSERT INTO messages(chat_id, from_owner, text, ts) VALUES (?, ?, ?, ?)",
            (chat_id, int(from_owner), text, time.time()),
        )
        await self.conn.commit()

    async def history(self, chat_id: int, limit: int = 20) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT from_owner, text, ts FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in reversed(rows)]

    async def last_incoming(self, chat_id: int) -> str | None:
        """Последние подряд идущие сообщения собеседника (после моего последнего ответа)."""
        rows = await self.history(chat_id, limit=20)
        tail: list[str] = []
        for row in reversed(rows):
            if row["from_owner"]:
                break
            tail.append(row["text"])
        return "\n".join(reversed(tail)) or None

    # --- примеры ---
    async def add_example(self, reply: str, incoming: str | None, source: str) -> None:
        await self.add_example_id(reply, incoming, source)

    async def add_example_id(self, reply: str, incoming: str | None, source: str) -> int:
        cur = await self.conn.execute(
            "INSERT INTO examples(incoming, reply, source, ts) VALUES (?, ?, ?, ?)",
            (incoming, reply, source, time.time()),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def append_to_example(self, example_id: int, text: str) -> None:
        """Дописывает следующее сообщение той же «пачки» с новой строки."""
        await self.conn.execute(
            "UPDATE examples SET reply = reply || char(10) || ?, ts=? WHERE id=?",
            (text, time.time(), example_id),
        )
        await self.conn.commit()

    async def examples(self) -> list[dict[str, Any]]:
        async with self.conn.execute("SELECT id, incoming, reply, source FROM examples ORDER BY id") as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def examples_count(self) -> int:
        async with self.conn.execute("SELECT COUNT(*) AS n FROM examples") as cur:
            row = await cur.fetchone()
        return int(row["n"])

    # --- стиль ---
    async def get_style(self) -> dict[str, Any]:
        async with self.conn.execute("SELECT * FROM style WHERE id=1") as cur:
            row = await cur.fetchone()
        data = dict(row)
        data["profile"] = json.loads(data["profile"] or "{}")
        return data

    async def set_style_description(self, text: str) -> None:
        await self.conn.execute("UPDATE style SET description=?, updated_at=? WHERE id=1", (text, time.time()))
        await self.conn.commit()

    async def set_style_profile(self, profile: dict[str, Any], summary: str) -> None:
        await self.conn.execute(
            "UPDATE style SET profile=?, summary=?, updated_at=? WHERE id=1",
            (json.dumps(profile, ensure_ascii=False), summary, time.time()),
        )
        await self.conn.commit()

    async def reset_style(self) -> None:
        await self.conn.execute("DELETE FROM examples")
        await self.conn.execute(
            "UPDATE style SET description='', profile='{}', summary='', updated_at=? WHERE id=1",
            (time.time(),),
        )
        await self.conn.commit()

    # --- черновики ---
    async def create_draft(self, chat_id: int, connection_id: str, parts: list[str]) -> int:
        cur = await self.conn.execute(
            "INSERT INTO drafts(chat_id, connection_id, parts, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, connection_id, json.dumps(parts, ensure_ascii=False), time.time()),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def get_draft(self, draft_id: int) -> dict[str, Any] | None:
        async with self.conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        data = dict(row)
        data["parts"] = json.loads(data["parts"])
        return data

    async def set_draft_status(self, draft_id: int, status: str) -> None:
        await self.conn.execute("UPDATE drafts SET status=? WHERE id=?", (status, draft_id))
        await self.conn.commit()

    async def claim_draft(self, draft_id: int, from_status: str, to_status: str) -> bool:
        """Атомарно меняет статус. False, если черновик уже обработан."""
        cur = await self.conn.execute(
            "UPDATE drafts SET status=? WHERE id=? AND status=?", (to_status, draft_id, from_status)
        )
        await self.conn.commit()
        return cur.rowcount == 1

    async def editing_draft(self) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT id FROM drafts WHERE status='editing' ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return await self.get_draft(row["id"]) if row else None

    async def expire_pending_drafts(self, chat_id: int) -> list[int]:
        """Старые черновики чата больше не актуальны, когда пришло новое сообщение или ответил я сам."""
        async with self.conn.execute(
            "SELECT id FROM drafts WHERE chat_id=? AND status='pending'", (chat_id,)
        ) as cur:
            ids = [r["id"] for r in await cur.fetchall()]
        if ids:
            await self.conn.execute(
                "UPDATE drafts SET status='expired' WHERE chat_id=? AND status='pending'", (chat_id,)
            )
            await self.conn.commit()
        return ids

    # --- статистика ---
    async def log_reply(self, chat_id: int, mode: str) -> None:
        await self.conn.execute(
            "INSERT INTO replies_log(chat_id, mode, ts) VALUES (?, ?, ?)", (chat_id, mode, time.time())
        )
        await self.conn.commit()

    async def stats(self) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT r.chat_id, c.title, c.username, COUNT(*) AS n, MAX(r.ts) AS last_ts "
            "FROM replies_log r LEFT JOIN chats c ON c.chat_id = r.chat_id "
            "GROUP BY r.chat_id ORDER BY n DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]


def _chat(row: aiosqlite.Row) -> Chat:
    return Chat(
        chat_id=row["chat_id"],
        title=row["title"],
        username=row["username"],
        status=row["status"],
        paused_until=row["paused_until"],
        connection_id=row["connection_id"],
        notified=bool(row["notified"]),
    )
