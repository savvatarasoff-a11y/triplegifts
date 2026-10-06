"""Рассылка от админа: выбор получателей и отправка копии сообщения каждому, с учётом лимитов Telegram."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from .db import Database

log = logging.getLogger(__name__)

WEEK = 7 * 86400
SEND_PAUSE = 0.05          # ~20 сообщений в секунду — ниже лимита Telegram на рассылки ботом

# ключ → (название кнопки, условие SQL по таблице users)
AUDIENCES: dict[str, tuple[str, str]] = {
    "all": ("Всем", "1=1"),
    "paid": ("Пополнявшим", "(deposited > 0 OR ton_deposited > 0)"),
    "unpaid": ("Не пополнявшим", "(deposited = 0 AND ton_deposited = 0)"),
    "active": ("Активным за неделю", "last_seen > :week"),
    "sleeping": ("Не заходили неделю+", "last_seen <= :week"),
    "balance": ("С балансом", "(balance > 0 OR ton > 0)"),
}


async def audience(db: Database, kind: str) -> list[int]:
    _, cond = AUDIENCES[kind]
    sql = cond.replace(":week", "?")
    args = [time.time() - WEEK] if ":week" in cond else []
    return [r["id"] for r in await db.all(f"SELECT id FROM users WHERE {sql} ORDER BY id", *args)]


async def counts(db: Database) -> dict[str, int]:
    return {k: len(await audience(db, k)) for k in AUDIENCES}


async def parse_list(db: Database, text: str) -> tuple[list[int], list[str]]:
    """Свой список: id и @ники через пробел, запятую или с новой строки. Возвращает (id найденных, не найденные)."""
    ids: list[int] = []
    missing: list[str] = []
    for token in re.split(r"[\s,;]+", text or ""):
        token = token.strip()
        if not token:
            continue
        user = None
        if token.lstrip("@").isdigit():
            user = await db.get_user(int(token.lstrip("@")))
        else:
            user = await db.one("SELECT id FROM users WHERE lower(username)=lower(?)", token.lstrip("@"))
        if user and user["id"] not in ids:
            ids.append(user["id"])
        elif not user:
            missing.append(token)
    return ids, missing


async def send(bot: Bot, ids: list[int], from_chat: int, message_id: int, reply_markup: Any = None,
               pause: float = SEND_PAUSE) -> dict[str, int]:
    """Копирует сообщение каждому. Заблокировавшие бота и не начинавшие с ним чат — в «не доставлено»."""
    sent = blocked = failed = 0
    for uid in ids:
        for attempt in range(3):
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=from_chat, message_id=message_id,
                                       reply_markup=reply_markup)
                sent += 1
                break
            except TelegramRetryAfter as e:                 # Telegram просит притормозить
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                blocked += 1
                break
            except Exception as e:
                if attempt == 2:
                    failed += 1
                    log.info("Рассылка: %s не доставлено (%s)", uid, type(e).__name__)
                else:
                    await asyncio.sleep(1)
        await asyncio.sleep(pause)
    return {"sent": sent, "blocked": blocked, "failed": failed}
