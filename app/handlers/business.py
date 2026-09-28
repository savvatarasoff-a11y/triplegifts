"""Апдейты Telegram Business: подключение к аккаунту и сообщения в личных чатах."""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.types import BusinessConnection, Message

from ..config import Config
from ..db import Database
from ..responder import Responder

log = logging.getLogger(__name__)


def _can_reply(conn: BusinessConnection) -> bool:
    if conn.rights is not None:
        return bool(conn.rights.can_reply)
    return bool(conn.can_reply)


def build_business_router(cfg: Config, db: Database, responder: Responder) -> Router:
    router = Router(name="business")

    async def save(conn: BusinessConnection) -> None:
        await db.save_connection(conn.id, conn.user.id, _can_reply(conn), conn.is_enabled)

    @router.business_connection()
    async def on_connection(conn: BusinessConnection) -> None:
        if conn.user.id != cfg.owner_id:
            log.warning("Чужой аккаунт пытается подключить бота, игнорирую")
            return
        await save(conn)
        if not conn.is_enabled:
            await responder.notify("🔌 Бот отключён от твоего аккаунта.")
        elif not _can_reply(conn):
            await responder.notify(
                "⚠️ Бот подключён, но без права отвечать. Включи «Отвечать на сообщения» "
                "в Настройках → Telegram для бизнеса → Чат-боты."
            )
        else:
            await responder.notify(
                "🔌 Бот подключён к аккаунту. По умолчанию отвечаю только в разрешённых чатах "
                "(/allow) и в режиме черновиков (/draft)."
            )

    @router.business_message()
    async def on_business_message(message: Message, bot: Bot) -> None:
        conn_id = message.business_connection_id
        if not conn_id:
            return
        conn = await db.get_connection(conn_id)
        if conn is None:
            # Подключение могло появиться, пока бот был выключен
            try:
                await save(await bot.get_business_connection(conn_id))
            except Exception:
                log.exception("Не удалось получить бизнес-подключение")
                return
            conn = await db.get_connection(conn_id)
        if not conn or conn["user_id"] != cfg.owner_id:
            return
        if message.sender_business_bot:
            return  # это отправил сам бот
        if message.from_user and message.from_user.id == cfg.owner_id:
            await responder.on_owner_message(message)
        else:
            await responder.on_incoming(message)

    return router
