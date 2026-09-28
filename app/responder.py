"""Логика ответов в бизнес-чатах: когда отвечать, как, черновики и обучение на ходу."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Config
from .db import Chat, Database
from .humanize import send_humanlike
from .llm import LLM, LLMError
from .style import compute_profile, profile_to_text, select_examples

log = logging.getLogger(__name__)

STALE_SECONDS = 600       # на сообщения старше 10 минут (бот был выключен) не отвечаем
DEBOUNCE_SECONDS = 4       # ждём, не допишет ли собеседник ещё сообщение
BURST_SECONDS = 120        # мои сообщения подряд в пределах 2 минут считаем одним ответом
PROFILE_REFRESH_EVERY = 10  # пересчёт статистики стиля после каждых N новых живых примеров


def message_text(message: Message) -> str:
    """Текст сообщения или пометка о типе вложения."""
    if message.text:
        return message.text
    placeholder = ""
    if message.sticker:
        placeholder = f"[стикер {message.sticker.emoji or ''}]".replace(" ]", "]")
    elif message.voice:
        placeholder = "[голосовое сообщение]"
    elif message.video_note:
        placeholder = "[видеосообщение-кружок]"
    elif message.photo:
        placeholder = "[фото]"
    elif message.video:
        placeholder = "[видео]"
    elif message.animation:
        placeholder = "[гифка]"
    elif message.document:
        placeholder = "[файл]"
    elif message.audio:
        placeholder = "[аудио]"
    elif message.location:
        placeholder = "[геолокация]"
    elif message.contact:
        placeholder = "[контакт]"
    if message.caption:
        return f"{placeholder} {message.caption}".strip()
    return placeholder or "[сообщение]"


def chat_label(chat: Chat | None, chat_id: int) -> str:
    if chat is None:
        return str(chat_id)
    name = chat.title or str(chat_id)
    if chat.username:
        name += f" (@{chat.username})"
    return name


def chat_card_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Разрешить", callback_data=f"c:allow:{chat_id}"),
        InlineKeyboardButton(text="🚫 Блок", callback_data=f"c:block:{chat_id}"),
    ]])


def draft_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data=f"d:send:{draft_id}"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data=f"d:edit:{draft_id}"),
        InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"d:skip:{draft_id}"),
    ]])


@dataclass
class _Burst:
    example_id: int
    ts: float


class Responder:
    def __init__(self, bot: Bot, cfg: Config, db: Database, llm: LLM):
        self.bot = bot
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self._pending: dict[int, asyncio.Task] = {}
        self._bursts: dict[str, _Burst] = {}
        self._live_added = 0

    # ---------- входящие от собеседника ----------

    async def on_incoming(self, message: Message) -> None:
        chat_id = message.chat.id
        text = message_text(message)
        chat = await self.db.upsert_chat(
            chat_id, message.chat.full_name, message.chat.username, message.business_connection_id
        )
        await self.db.add_message(chat_id, from_owner=False, text=text)
        self._bursts.pop(f"live:{chat_id}", None)

        if chat.status == "block":
            return
        if time.time() - message.date.timestamp() > STALE_SECONDS:
            return
        if chat.status == "new":
            if not chat.notified:
                await self.db.mark_notified(chat_id)
                await self.notify(
                    f"💬 Новый чат: <b>{html.escape(chat_label(chat, chat_id))}</b>\n"
                    f"«{html.escape(text[:300])}»\n\nОтвечать в этом чате?",
                    reply_markup=chat_card_keyboard(chat_id),
                )
            return
        if not await self.db.get_flag("enabled"):
            return
        if chat.paused_until > time.time():
            log.info("Чат %s на паузе после моего сообщения", chat_id)
            return
        conn = await self.db.get_connection(message.business_connection_id or "")
        if not conn or not conn["can_reply"] or not conn["is_enabled"]:
            log.warning("Нет права отвечать через бизнес-подключение")
            return

        await self._expire_drafts(chat_id)
        self._cancel(chat_id)
        self._pending[chat_id] = asyncio.create_task(
            self._respond(chat_id, message.business_connection_id or "")
        )

    def _cancel(self, chat_id: int) -> None:
        task = self._pending.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _respond(self, chat_id: int, connection_id: str) -> None:
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            incoming = await self.db.last_incoming(chat_id)
            if not incoming:
                return
            reply = await self._generate(chat_id, incoming)
            chat = await self.db.get_chat(chat_id)
            label = chat_label(chat, chat_id)

            if not reply.messages:
                await self.notify(
                    f"⚠️ Не смог ответить в чате <b>{html.escape(label)}</b>: "
                    f"{html.escape(reply.owner_reason or 'пустой ответ')}.\n«{html.escape(incoming[:500])}»"
                )
                return

            if reply.needs_owner:
                await self.notify(
                    f"🙋 Нужно твоё решение в чате <b>{html.escape(label)}</b>\n"
                    f"Вопрос: «{html.escape(incoming[:500])}»\n"
                    f"Что решить: {html.escape(reply.owner_reason or 'не указано')}"
                )

            if await self.db.get_flag("draft_mode"):
                draft_id = await self.db.create_draft(chat_id, connection_id, reply.messages)
                proposed = "\n".join(f"— {html.escape(m)}" for m in reply.messages)
                note = "\n\n<i>Ответ уклончивый: нужно твоё решение.</i>" if reply.needs_owner else ""
                await self.notify(
                    f"📝 Черновик для <b>{html.escape(label)}</b>\n"
                    f"Собеседник: «{html.escape(incoming[:700])}»\n\n"
                    f"Предлагаю ответить:\n{proposed}{note}",
                    reply_markup=draft_keyboard(draft_id),
                )
                return

            await self.send_parts(chat_id, connection_id, reply.messages, wait=True)
            await self.db.log_reply(chat_id, "evasive" if reply.needs_owner else "auto")
        except asyncio.CancelledError:
            raise
        except LLMError as e:
            log.error("Ошибка генерации ответа: %s", e)
            await self.notify(f"⚠️ {html.escape(str(e))}")
        except Exception:
            log.exception("Ошибка при ответе в чате %s", chat_id)
        finally:
            if self._pending.get(chat_id) is asyncio.current_task():
                self._pending.pop(chat_id, None)

    async def _generate(self, chat_id: int, incoming: str):
        style = await self.db.get_style()
        examples = select_examples(incoming, await self.db.examples(), k=15)
        history = await self.db.history(chat_id, limit=20)
        return await self.llm.generate_reply(
            owner_name=self.cfg.owner_name,
            style_description=style["description"],
            style_summary=style["summary"],
            profile_text=profile_to_text(style["profile"]),
            examples=examples,
            history=history,
            incoming=incoming,
        )

    async def send_parts(self, chat_id: int, connection_id: str, parts: list[str], wait: bool) -> None:
        # Эхо-апдейты с sender_business_bot в историю не пишутся, поэтому сохраняем здесь
        await send_humanlike(
            self.bot, chat_id, connection_id, parts, self.cfg.delay_min, self.cfg.delay_max, wait=wait
        )
        for part in parts:
            await self.db.add_message(chat_id, from_owner=True, text=part)

    async def kick(self, chat_id: int) -> None:
        """Ответить на уже пришедшее сообщение (например, после того как чат разрешили)."""
        chat = await self.db.get_chat(chat_id)
        if not chat or not chat.connection_id or chat.status != "allow":
            return
        if not await self.db.get_flag("enabled") or chat.paused_until > time.time():
            return
        if not await self.db.last_incoming(chat_id):
            return
        self._cancel(chat_id)
        self._pending[chat_id] = asyncio.create_task(self._respond(chat_id, chat.connection_id))

    # ---------- мои собственные сообщения в чатах ----------

    async def on_owner_message(self, message: Message) -> None:
        chat_id = message.chat.id
        text = message_text(message)
        await self.db.upsert_chat(
            chat_id, message.chat.full_name, message.chat.username, message.business_connection_id
        )
        incoming = await self.db.last_incoming(chat_id)
        await self.db.add_message(chat_id, from_owner=True, text=text)

        # Я ответил сам — бот молчит в этом чате
        self._cancel(chat_id)
        await self._expire_drafts(chat_id)
        if self.cfg.owner_pause_minutes:
            await self.db.pause_chat(chat_id, time.time() + self.cfg.owner_pause_minutes * 60)

        # Учимся на моём ответе (пропускаем стикеры/медиа без подписи)
        if message.text or message.caption:
            await self.learn(f"live:{chat_id}", message.text or message.caption or "", incoming, "live")
            self._live_added += 1
            if self._live_added >= PROFILE_REFRESH_EVERY:
                self._live_added = 0
                await self.refresh_stats()

    async def learn(self, burst_key: str, text: str, incoming: str | None, source: str) -> None:
        """Сохраняет пример; сообщения подряд в течение 2 минут склеивает в один ответ."""
        now = time.time()
        burst = self._bursts.get(burst_key)
        if burst and now - burst.ts < BURST_SECONDS:
            await self.db.append_to_example(burst.example_id, text)
            burst.ts = now
            return
        example_id = await self.db.add_example_id(text, incoming, source)
        self._bursts[burst_key] = _Burst(example_id, now)

    def break_burst(self, burst_key: str) -> None:
        self._bursts.pop(burst_key, None)

    # ---------- стиль ----------

    async def refresh_stats(self) -> dict:
        examples = await self.db.examples()
        profile = compute_profile([e["reply"] for e in examples])
        style = await self.db.get_style()
        profile["_summary_n"] = style["profile"].get("_summary_n", 0)
        profile["_summary_desc"] = style["profile"].get("_summary_desc", "")
        await self.db.set_style_profile(profile, style["summary"])
        return profile

    async def refresh_profile(self, force_summary: bool = False) -> tuple[dict, str]:
        """Пересчитывает статистику и, если данные заметно изменились, описание стиля от Claude."""
        profile = await self.refresh_stats()
        style = await self.db.get_style()
        summary = style["summary"]
        n = profile.get("replies", 0)
        stale = (
            force_summary
            or not summary
            or n - profile.get("_summary_n", 0) >= 10
            or profile.get("_summary_desc", "") != style["description"]
        )
        if stale and (n or style["description"]):
            replies = [e["reply"] for e in await self.db.examples()]
            summary = await self.llm.summarize_style(self.cfg.owner_name, style["description"], replies)
            profile["_summary_n"] = n
            profile["_summary_desc"] = style["description"]
            await self.db.set_style_profile(profile, summary)
        return profile, summary

    # ---------- черновики ----------

    async def _expire_drafts(self, chat_id: int) -> None:
        await self.db.expire_pending_drafts(chat_id)

    # ---------- уведомления ----------

    async def notify(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        try:
            await self.bot.send_message(self.cfg.owner_id, text, reply_markup=reply_markup)
        except Exception:
            log.exception("Не удалось отправить уведомление владельцу (он нажал /start у бота?)")
