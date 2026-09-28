"""Точка входа: long polling 24/7."""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat, Message

from .config import Config, ConfigError
from .db import Database
from .handlers.business import build_business_router
from .handlers.callbacks import build_callbacks_router
from .handlers.owner import build_owner_router
from .llm import LLM
from .logging_setup import setup_logging
from .responder import Responder

log = logging.getLogger("bot")

COMMANDS = [
    ("help", "Помощь и статус"),
    ("on", "Включить автоответы"),
    ("off", "Выключить автоответы"),
    ("draft", "Режим черновиков вкл/выкл"),
    ("allow", "Разрешить чат"),
    ("block", "Заблокировать чат"),
    ("stats", "Статистика ответов"),
    ("style", "Описать свой стиль"),
    ("examples", "Добавить примеры сообщений"),
    ("done", "Закончить сбор примеров"),
    ("profile", "Показать профиль стиля"),
    ("reset_style", "Стереть стиль"),
    ("cancel", "Отменить действие"),
]


def build_strangers_router(owner_id: int) -> Router:
    router = Router(name="strangers")

    @router.message(F.chat.type == "private", F.from_user.id != owner_id)
    async def stranger(message: Message) -> None:
        await message.answer("Это личный бот, он работает только для владельца.")

    return router


async def run() -> None:
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        print(f"Ошибка настройки: {e}", file=sys.stderr)
        sys.exit(1)
    setup_logging(cfg.log_level, cfg.secrets())

    db = Database(cfg.db_path)
    await db.connect()
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    llm = LLM(cfg.anthropic_api_key, cfg.claude_model)
    responder = Responder(bot, cfg, db, llm)

    dp = Dispatcher()
    dp.include_router(build_business_router(cfg, db, responder))
    dp.include_router(build_callbacks_router(cfg, db, responder))
    dp.include_router(build_owner_router(cfg, db, llm, responder))
    dp.include_router(build_strangers_router(cfg.owner_id))

    try:
        await bot.set_my_commands(
            [BotCommand(command=c, description=d) for c, d in COMMANDS],
            scope=BotCommandScopeChat(chat_id=cfg.owner_id),
        )
    except Exception:
        log.warning("Не удалось установить меню команд (владелец ещё не нажал /start у бота?)")

    me = await bot.get_me()
    log.info("Бот @%s запущен, модель %s, база %s", me.username, cfg.claude_model, cfg.db_path)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await db.close()
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
