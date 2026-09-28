"""Команды управления и загрузка стиля в личке с ботом (только для OWNER_ID)."""
from __future__ import annotations

import html
import io
import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginHiddenUser,
    MessageOriginUser,
)

from ..config import Config
from ..db import Chat, Database
from ..llm import LLM, LLMError
from ..responder import Responder, chat_label
from ..style import parse_pairs, profile_to_text

log = logging.getLogger(__name__)

FORWARD_PAIR_SECONDS = 90  # пересланное чужое сообщение ждёт моего ответа столько секунд

HELP = """<b>Управление</b>
/on, /off — включить или выключить автоответы
/draft — режим черновиков (сейчас: {draft})
/allow — разрешить чат (@username, ID или выбрать из списка)
/block — заблокировать чат
/stats — статистика ответов

<b>Стиль</b>
/style — описать свою манеру общения текстом
/examples — режим сбора примеров, /done — закончить
/profile — показать профиль стиля
/reset_style — стереть стиль и все примеры
/cancel — отменить текущее действие

В любой момент можно переслать мне свои сообщения из чатов или прислать скриншот переписки — добавлю в примеры.

Сейчас: автоответы <b>{enabled}</b>, примеров: {examples}."""


def build_owner_router(cfg: Config, db: Database, llm: LLM, responder: Responder) -> Router:
    router = Router(name="owner")
    router.message.filter(F.chat.type == "private", F.from_user.id == cfg.owner_id)

    last_foreign_forward: dict[str, float | str] = {}

    async def help_text() -> str:
        return HELP.format(
            draft="вкл" if await db.get_flag("draft_mode") else "выкл",
            enabled="включены" if await db.get_flag("enabled") else "выключены",
            examples=await db.examples_count(),
        )

    # ---------- общие ----------

    @router.message(Command("start", "help"))
    async def cmd_start(message: Message) -> None:
        await message.answer(await help_text())

    @router.message(Command("on"))
    async def cmd_on(message: Message) -> None:
        await db.set_flag("enabled", True)
        await message.answer("✅ Автоответы включены.")

    @router.message(Command("off"))
    async def cmd_off(message: Message) -> None:
        await db.set_flag("enabled", False)
        await message.answer("⏸ Автоответы выключены. Сообщения продолжу запоминать.")

    @router.message(Command("draft"))
    async def cmd_draft(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip().lower()
        if arg in ("on", "вкл", "1"):
            value = True
        elif arg in ("off", "выкл", "0"):
            value = False
        else:
            value = not await db.get_flag("draft_mode")
        await db.set_flag("draft_mode", value)
        if value:
            await message.answer("📝 Режим черновиков включён: сначала присылаю ответ тебе на проверку.")
        else:
            await message.answer("🤖 Режим черновиков выключен: отвечаю в чатах сам.")

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message) -> None:
        await db.set_setting("awaiting", "")
        draft = await db.editing_draft()
        if draft:
            await db.set_draft_status(draft["id"], "pending")
        await message.answer("Ок, отменил.")

    # ---------- списки чатов ----------

    async def resolve_chat(arg: str) -> Chat | None:
        arg = arg.strip()
        if arg.lstrip("-").isdigit():
            chat = await db.get_chat(int(arg))
            if chat:
                return chat
            await db.set_chat_status(int(arg), "new")
            return await db.get_chat(int(arg))
        return await db.find_chat_by_username(arg)

    def chats_keyboard(chats: list[Chat]) -> InlineKeyboardMarkup:
        rows = []
        for chat in chats:
            mark = {"allow": "✅", "block": "🚫"}.get(chat.status, "•")
            label = f"{mark} {chat_label(chat, chat.chat_id)}"[:40]
            rows.append([
                InlineKeyboardButton(text=label, callback_data=f"c:allow:{chat.chat_id}"),
                InlineKeyboardButton(text="🚫", callback_data=f"c:block:{chat.chat_id}"),
            ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def set_status(message: Message, command: CommandObject, status: str) -> None:
        if not command.args:
            chats = await db.recent_chats(15)
            if not chats:
                await message.answer("Чатов пока нет. Они появятся, когда тебе кто-нибудь напишет.")
                return
            allowed = ", ".join(chat_label(c, c.chat_id) for c in await db.chats_by_status("allow")) or "нет"
            await message.answer(
                f"Разрешённые: {html.escape(allowed)}\n\nНажми на чат, чтобы разрешить, или 🚫, чтобы заблокировать:",
                reply_markup=chats_keyboard(chats),
            )
            return
        chat = await resolve_chat(command.args)
        if not chat:
            await message.answer("Не нашёл такой чат. Укажи @username собеседника, который тебе уже писал, или его числовой ID.")
            return
        await db.set_chat_status(chat.chat_id, status)
        verb = "разрешён ✅" if status == "allow" else "заблокирован 🚫"
        await message.answer(f"Чат {html.escape(chat_label(chat, chat.chat_id))} {verb}")
        if status == "allow":
            await responder.kick(chat.chat_id)

    @router.message(Command("allow"))
    async def cmd_allow(message: Message, command: CommandObject) -> None:
        await set_status(message, command, "allow")

    @router.message(Command("block"))
    async def cmd_block(message: Message, command: CommandObject) -> None:
        await set_status(message, command, "block")

    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        rows = await db.stats()
        total = sum(r["n"] for r in rows)
        lines = [f"📊 Отправлено ответов: <b>{total}</b>"]
        for r in rows[:30]:
            name = r["title"] or str(r["chat_id"])
            if r["username"]:
                name += f" (@{r['username']})"
            last = time.strftime("%d.%m %H:%M", time.gmtime(r["last_ts"]))
            lines.append(f"• {html.escape(name)}: {r['n']} (последний {last} UTC)")
        lines.append(f"\nПримеров стиля: {await db.examples_count()}")
        lines.append(f"Разрешённых чатов: {len(await db.chats_by_status('allow'))}")
        await message.answer("\n".join(lines))

    # ---------- стиль ----------

    @router.message(Command("style"))
    async def cmd_style(message: Message, command: CommandObject) -> None:
        if command.args:
            await db.set_style_description(command.args.strip())
            await message.answer("✅ Описание стиля сохранил. /profile — посмотреть профиль.")
            return
        style = await db.get_style()
        current = html.escape(style["description"]) or "<i>пока пусто</i>"
        await db.set_setting("awaiting", "style")
        await message.answer(
            f"Текущее описание:\n{current}\n\n"
            "Пришли одним сообщением, как ты пишешь: сленг, эмодзи, мат, длинные или короткие сообщения, "
            "пишешь ли с большой буквы, ставишь ли точки, как здороваешься и прощаешься. /cancel — отмена."
        )

    @router.message(Command("examples"))
    async def cmd_examples(message: Message) -> None:
        await db.set_flag("collecting", True)
        await message.answer(
            "📥 Режим сбора примеров включён. Присылай:\n"
            "• пересланные свои сообщения из чатов (можно чужое сообщение, а следом свой ответ — сохраню парой);\n"
            "• текст в формате\n<code>мне: го гулять?\nя: ща, через час</code>\n"
            "• просто свои сообщения текстом;\n"
            "• скриншоты переписок.\n\nКогда закончишь — /done."
        )

    @router.message(Command("done"))
    async def cmd_done(message: Message) -> None:
        await db.set_flag("collecting", False)
        await message.answer("Готово, обновляю профиль стиля…")
        await show_profile(message, force=False)

    async def show_profile(message: Message, force: bool) -> None:
        try:
            profile, summary = await responder.refresh_profile(force_summary=force)
        except LLMError as e:
            await message.answer(f"⚠️ {html.escape(str(e))}")
            profile, summary = (await db.get_style())["profile"], (await db.get_style())["summary"]
        style = await db.get_style()
        parts = [
            "<b>Профиль стиля</b>",
            f"\n<b>Твоё описание:</b>\n{html.escape(style['description']) or '<i>не задано, /style</i>'}",
            f"\n<b>Статистика:</b>\n{html.escape(profile_to_text(profile))}",
        ]
        if summary:
            parts.append(f"\n<b>Как я понял твой стиль:</b>\n{html.escape(summary)}")
        text = "\n".join(parts)
        for i in range(0, len(text), 4000):
            await message.answer(text[i : i + 4000])

    @router.message(Command("profile"))
    async def cmd_profile(message: Message, command: CommandObject) -> None:
        await show_profile(message, force=(command.args or "").strip() == "new")

    @router.message(Command("reset_style"))
    async def cmd_reset(message: Message) -> None:
        await message.answer(
            "Точно стереть описание стиля и все примеры?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑 Да, стереть", callback_data="s:reset"),
                InlineKeyboardButton(text="Отмена", callback_data="s:keep"),
            ]]),
        )

    # ---------- скриншоты ----------

    async def ingest_image(message: Message, bot: Bot, file_id: str, media_type: str) -> None:
        status = await message.answer("🔍 Читаю скриншот…")
        buf = io.BytesIO()
        await bot.download(file_id, destination=buf)
        try:
            pairs = await llm.read_screenshot(buf.getvalue(), media_type, hint=message.caption or "")
        except LLMError as e:
            await status.edit_text(f"⚠️ {html.escape(str(e))}")
            return
        for incoming, reply in pairs:
            await db.add_example(reply, incoming, "screenshot")
        if not pairs:
            await status.edit_text("Не нашёл на скриншоте твоих сообщений 🤷")
            return
        preview = "\n".join(f"• {html.escape(r[:80])}" for _, r in pairs[:5])
        await status.edit_text(f"✅ Добавил примеров: {len(pairs)}\n{preview}")

    @router.message(F.photo)
    async def on_photo(message: Message, bot: Bot) -> None:
        await ingest_image(message, bot, message.photo[-1].file_id, "image/jpeg")

    @router.message(F.document.mime_type.in_({"image/png", "image/jpeg", "image/webp", "image/gif"}))
    async def on_image_doc(message: Message, bot: Bot) -> None:
        await ingest_image(message, bot, message.document.file_id, message.document.mime_type)

    # ---------- пересланные сообщения ----------

    def is_own_forward(message: Message) -> bool:
        origin = message.forward_origin
        if isinstance(origin, MessageOriginUser):
            return origin.sender_user.id == cfg.owner_id
        if isinstance(origin, MessageOriginHiddenUser):
            return origin.sender_user_name == message.from_user.full_name
        return False

    @router.message(F.forward_origin)
    async def on_forward(message: Message) -> None:
        text = message.text or message.caption
        if not text:
            await message.answer("Сохраняю только текстовые сообщения.")
            return
        now = time.time()
        if not is_own_forward(message):
            # Чужое сообщение: запомним, свой ответ придёт следующим
            responder.break_burst("forward")
            if now - float(last_foreign_forward.get("ts", 0)) < 5 and last_foreign_forward.get("text"):
                last_foreign_forward["text"] = f"{last_foreign_forward['text']}\n{text}"
            else:
                last_foreign_forward["text"] = text
            last_foreign_forward["ts"] = now
            return
        incoming = None
        if now - float(last_foreign_forward.get("ts", 0)) < FORWARD_PAIR_SECONDS:
            incoming = str(last_foreign_forward.get("text") or "") or None
        await responder.learn("forward", text, incoming, "forward")
        count = await db.examples_count()
        # Не спамим ответом на каждое пересланное сообщение
        if count % 5 == 0 or incoming:
            note = " (в паре с сообщением собеседника)" if incoming else ""
            await message.answer(f"✅ Сохранил{note}. Всего примеров: {count}")

    # ---------- обычный текст ----------

    @router.message(F.text & ~F.text.startswith("/"))
    async def on_text(message: Message, bot: Bot) -> None:
        text = message.text or ""

        draft = await db.editing_draft()
        if draft:
            parts = [p.strip() for p in text.split("\n") if p.strip()]
            if not await db.claim_draft(draft["id"], "editing", "sent"):
                await message.answer("Этот черновик уже неактуален.")
                return
            incoming = await db.last_incoming(draft["chat_id"])
            await message.answer("Отправляю ✅")
            await responder.send_parts(draft["chat_id"], draft["connection_id"], parts, wait=False)
            await db.log_reply(draft["chat_id"], "draft")
            await db.add_example("\n".join(parts), incoming, "edit")
            return

        awaiting = await db.get_setting("awaiting")
        if awaiting == "style":
            await db.set_setting("awaiting", "")
            await db.set_style_description(text.strip())
            await message.answer("✅ Описание стиля сохранил. /profile — посмотреть профиль.")
            return

        if await db.get_flag("collecting"):
            pairs = parse_pairs(text)
            for incoming, reply in pairs:
                await db.add_example(reply, incoming, "manual")
            with_pair = sum(1 for i, _ in pairs if i)
            await message.answer(
                f"✅ Добавил примеров: {len(pairs)} (из них пар «мне → я»: {with_pair}). "
                f"Всего: {await db.examples_count()}. Продолжай или /done."
            )
            return

        await message.answer(
            "Чтобы добавить это как пример моего стиля, включи /examples. Все команды — /help."
        )

    return router
