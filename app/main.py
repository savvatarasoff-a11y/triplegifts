"""Точка входа: бот (long polling) + веб-сервер мини-приложения в одном процессе."""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommand, BotCommandScopeChat, MenuButtonWebApp, WebAppInfo
from aiohttp import web

from .bot import build_router
from .casino import Casino
from .config import Config, ConfigError
from .db import Database
from .logging_setup import setup_logging
from .nft import deliver_waiting, sync as sync_nfts
from .relayer import Relayer
from .web import build_app

log = logging.getLogger("casino")

PLAYER_COMMANDS = [
    ("start", "Открыть Svag Gifts"),
    ("deposit", "Пополнить звёздами"),
    ("balance", "Баланс"),
    ("help", "Правила"),
    ("paysupport", "Вопросы по оплате"),
]
ADMIN_COMMANDS = PLAYER_COMMANDS + [
    ("admin", "Команды администратора"),
    ("check", "Создать чек"),
    ("checks", "Активные чеки"),
    ("revoke", "Отозвать чек"),
    ("stats", "Статистика"),
    ("user", "Инфо об игроке"),
    ("withdrawals", "Заявки на вывод"),
    ("stars", "Баланс звёзд бота"),
    ("nfts", "NFT-модели и цены"),
    ("relayer", "Релейер NFT"),
]


async def crash_loop(casino: Casino) -> None:
    """Двигает общие раунды краша: 10 раз в секунду."""
    while True:
        try:
            await casino.crash_tick()
        except Exception:
            log.exception("Ошибка раунда краша")
        await asyncio.sleep(0.1)


async def background(casino: Casino, bot: Bot) -> None:
    """Раз в секунду завершает раунды PvP."""
    while True:
        try:
            for result in await casino.pvp_tick():
                try:
                    await bot.send_message(
                        result["winner"],
                        f"🏆 Вы выиграли раунд {'PvP-хоккея' if result['game'] == 'hockey' else 'PvP-рулетки'} "
                        f"№{result['round']}: <b>+{result['payout']} ⭐</b>",
                    )
                except Exception:
                    pass
        except Exception:
            log.exception("Ошибка фоновой задачи")
        await asyncio.sleep(1)


async def nft_loop(bot: Bot, cfg: Config, casino: Casino, relayer: Relayer) -> None:
    """Каждые 10 минут обновляет запас моделей у релейера и цены с маркета."""
    while True:
        try:
            await sync_nfts(casino.db, relayer)
        except Exception:
            log.warning("Не удалось обновить NFT-модели")
        await asyncio.sleep(600)


async def setup_bot_ui(bot: Bot, cfg: Config) -> None:
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in PLAYER_COMMANDS])
        for admin_id in cfg.admin_ids:
            try:
                await bot.set_my_commands(
                    [BotCommand(command=c, description=d) for c, d in ADMIN_COMMANDS],
                    scope=BotCommandScopeChat(chat_id=admin_id),
                )
            except Exception:
                log.warning("Меню админа %s не установлено: он ещё не нажал /start", admin_id)
        if cfg.webapp_url:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Svag Gifts", web_app=WebAppInfo(url=cfg.webapp_url))
            )
    except Exception:
        log.exception("Не удалось настроить меню бота")


async def run() -> None:
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        print(f"Ошибка настройки: {e}", file=sys.stderr)
        sys.exit(1)
    setup_logging(cfg.log_level, [cfg.bot_token])
    if not cfg.admin_ids:
        log.warning("ADMIN_IDS не задан — выдавать чеки никто не сможет")
    if not cfg.webapp_url:
        log.warning("WEBAPP_URL не задан и RAILWAY_PUBLIC_DOMAIN нет — кнопка мини-приложения не появится")

    db = Database(cfg.db_path)
    await db.connect()
    casino = Casino(db, cfg)
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    relayer = Relayer(db, cfg.bot_token)
    if await relayer.start():
        relayer.on_private_message(lambda user_id: deliver_waiting(bot, db, cfg, relayer, user_id))

    runner = web.AppRunner(build_app(cfg, casino, bot, relayer), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.port).start()
    dp = Dispatcher()
    dp.include_router(build_router(
        cfg, casino, relayer,
        on_relayer_ready=lambda: relayer.on_private_message(lambda uid: deliver_waiting(bot, db, cfg, relayer, uid)),
    ))
    await setup_bot_ui(bot, cfg)
    bg = asyncio.create_task(background(casino, bot))
    crash_task = asyncio.create_task(crash_loop(casino))
    nft_task = asyncio.create_task(nft_loop(bot, cfg, casino, relayer))

    try:
        me = await bot.get_me()
        log.info("Бот @%s запущен, мини-приложение %s, порт %s", me.username, cfg.webapp_url or "—", cfg.port)
    except Exception as e:
        log.error("Нет связи с Telegram при запуске (%s), продолжаю попытки", type(e).__name__)
    try:
        while True:
            try:
                await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
                break
            except TelegramNetworkError as e:
                log.error("Нет связи с Telegram (%s), повтор через 5 секунд", type(e).__name__)
                await asyncio.sleep(5)
    finally:
        bg.cancel()
        crash_task.cancel()
        nft_task.cancel()
        await relayer.stop()
        await runner.cleanup()
        await db.close()
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
