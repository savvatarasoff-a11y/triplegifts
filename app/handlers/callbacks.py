"""Кнопки: черновики, разрешение/блок чатов, сброс стиля."""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from ..config import Config
from ..db import Database
from ..responder import Responder, chat_label

log = logging.getLogger(__name__)


def build_callbacks_router(cfg: Config, db: Database, responder: Responder) -> Router:
    router = Router(name="callbacks")
    router.callback_query.filter(F.from_user.id == cfg.owner_id)

    async def mark(cb: CallbackQuery, suffix: str) -> None:
        if isinstance(cb.message, Message):
            text = cb.message.html_text or ""
            try:
                await cb.message.edit_text(f"{text}\n\n{suffix}", reply_markup=None)
            except Exception:
                pass

    @router.callback_query(F.data.startswith("c:"))
    async def on_chat(cb: CallbackQuery) -> None:
        _, action, raw_id = cb.data.split(":", 2)
        chat_id = int(raw_id)
        status = "allow" if action == "allow" else "block"
        await db.set_chat_status(chat_id, status)
        chat = await db.get_chat(chat_id)
        label = chat_label(chat, chat_id)
        await cb.answer("Разрешён ✅" if status == "allow" else "Заблокирован 🚫")
        if isinstance(cb.message, Message) and cb.message.reply_markup and len(cb.message.reply_markup.inline_keyboard) > 1:
            return  # это список чатов из /allow — оставляем кнопки
        await mark(cb, f"<b>{html.escape(label)}: {'разрешён ✅' if status == 'allow' else 'заблокирован 🚫'}</b>")
        if status == "allow":
            await responder.kick(chat_id)

    @router.callback_query(F.data.startswith("d:"))
    async def on_draft(cb: CallbackQuery) -> None:
        _, action, raw_id = cb.data.split(":", 2)
        draft_id = int(raw_id)
        draft = await db.get_draft(draft_id)
        if not draft:
            await cb.answer("Черновик не найден")
            return

        if action == "send":
            if not await db.claim_draft(draft_id, "pending", "sent"):
                await cb.answer("Черновик уже неактуален", show_alert=True)
                await mark(cb, "<i>неактуален</i>")
                return
            await cb.answer("Отправляю")
            await mark(cb, "✅ <b>Отправлено</b>")
            try:
                await responder.send_parts(draft["chat_id"], draft["connection_id"], draft["parts"], wait=False)
                await db.log_reply(draft["chat_id"], "draft")
            except Exception:
                log.exception("Не удалось отправить черновик")
                await responder.notify("⚠️ Не удалось отправить сообщение. Проверь, что бот подключён к аккаунту.")

        elif action == "skip":
            await db.claim_draft(draft_id, "pending", "skipped")
            await cb.answer("Пропущено")
            await mark(cb, "⏭ <b>Пропущено</b>")

        elif action == "edit":
            previous = await db.editing_draft()
            if previous and previous["id"] != draft_id:
                await db.set_draft_status(previous["id"], "pending")
            if not await db.claim_draft(draft_id, "pending", "editing"):
                await cb.answer("Черновик уже неактуален", show_alert=True)
                return
            await cb.answer()
            await responder.notify(
                "✏️ Пришли свой вариант ответа. Каждая строка уйдёт отдельным сообщением. /cancel — отмена."
            )

    @router.callback_query(F.data.startswith("s:"))
    async def on_style(cb: CallbackQuery) -> None:
        if cb.data == "s:reset":
            await db.reset_style()
            await cb.answer("Стёрто")
            await mark(cb, "🗑 <b>Стиль и примеры стёрты.</b>")
        else:
            await cb.answer("Отменено")
            await mark(cb, "Оставил как есть.")

    return router
