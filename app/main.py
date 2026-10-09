"""Точка входа: бот (long polling) + веб-сервер мини-приложения в одном процессе."""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

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
from .gifts import notify_deposits, scan as gifts_scan
from .nftimg import NftImages
from .mrkt import emoji_for
from .nft import catalog_keep_fresh, catalog_store, deliver_waiting, reprice_demo, sync as sync_nfts
from .relayer import Relayer
from .sports import Sportsbook
from . import ton as ton_mod
from .web import build_app
from .withdraw import approve_waiting

log = logging.getLogger("casino")

PLAYER_COMMANDS = [
    ("start", "Открыть Triple Gifts"),
]
ADMIN_COMMANDS = PLAYER_COMMANDS + [
    ("admin", "Команды администратора"),
    ("check", "Чек (бесплатный)"),
    ("mychecks", "Мои чеки"),
    ("checks", "Активные чеки"),
    ("revoke", "Отозвать чек"),
    ("stats", "Статистика"),
    ("user", "Инфо об игроке"),
    ("withdrawals", "Заявки на вывод"),
    ("stars", "Баланс звёзд бота"),
    ("nfts", "NFT-модели и цены"),
    ("relayer", "Релейер NFT"),
    ("tonrate", "Курс TON → звёзды"),
    ("nftimg", "Проверка картинок NFT"),
    ("channel_setup", "Оформить канал"),
    ("rebrand", "Оформление Triple Gifts"),
    ("leaders_reset", "Очистить таблицу лидеров"),
    ("channel_wins", "Выигрыши в канал"),
    ("dupe", "Демо-NFT (модели с MRKT)"),
    ("tonwallet", "Кошелёк для пополнений TON"),
    ("broadcast", "Рассылка игрокам"),
    ("take", "Списать баланс игрока"),
    ("give", "Начислить баланс игроку"),
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
                        f"№{result['round']}: <b>+{result['stars']} ⭐</b>"
                        + (f" и подарки на {result['gifts_value']} ⭐ (раздел «Мои подарки»)"
                           if result.get("gifts_value") else ""),
                    )
                except Exception:
                    pass
        except Exception:
            log.exception("Ошибка фоновой задачи")
        await asyncio.sleep(1)


async def prewarm_images(casino: Casino, images: NftImages) -> None:
    """Качает и ужимает картинки моделей из кейсов и апгрейда, чтобы у игроков они открывались мгновенно."""
    rows = await casino.db.all(
        "SELECT collection_name, model FROM nft_models WHERE enabled=1 AND price > 0 ORDER BY price DESC LIMIT 400")
    items = [(r["collection_name"], r["model"]) for r in rows if r["collection_name"] and r["model"]]
    ready = await images.prewarm(items)
    log.info("Картинки NFT: готово %s из %s (источники: %s)", ready, len(items), images.stats)


DEMO_TARGET = 100  # сколько разных NFT-моделей держать в кейсах (демо — с MRKT, пока идёт разработка)


async def top_up_demo(casino: Casino, relayer: Relayer) -> None:
    """Убирает демо-модели без улучшений (модели нет в Telegram) и добирает новые с MRKT до DEMO_TARGET."""
    # неулучшенный подарок на MRKT: вместо имени модели — число (id подарка)
    async with casino.db.tx() as c:
        await c.execute("UPDATE nft_models SET enabled=0 WHERE test=1 AND enabled=1 AND model NOT GLOB '*[^0-9]*'")
        await c.execute("DELETE FROM user_gifts WHERE test=1 AND status='owned' AND model NOT GLOB '*[^0-9]*'")
    if not relayer.ready:
        return
    if (await casino.db.one("SELECT COUNT(*) n FROM nft_models WHERE seen_at IS NOT NULL"))["n"]:
        return                                   # есть каталог с маркета Telegram — случайные модели не нужны
    for m in await casino.db.all("SELECT id, collection_name, model FROM nft_models WHERE test=1 AND enabled=1"):
        if await relayer.has_model(m["collection_name"], m["model"]) is False:
            async with casino.db.tx() as c:
                await c.execute("UPDATE nft_models SET enabled=0 WHERE id=?", (m["id"],))
            log.info("Демо-NFT %s «%s» убран: у коллекции нет такой модели", m["collection_name"], m["model"])
    have = (await casino.db.one("SELECT COUNT(*) n FROM nft_models WHERE test=1 AND enabled=1"))["n"]
    if have >= DEMO_TARGET:
        return
    sampled = await relayer.market.sample_models(min(40, DEMO_TARGET - have))
    upgraded = [m for m in sampled if await relayer.has_model(m["title"], m["model"])]
    added = await casino.demo_fill(upgraded)
    log.info("Демо-NFT: было %s, добавлено %s (без улучшений отброшено %s)", have, added, len(sampled) - len(upgraded))


async def nft_loop(bot: Bot, cfg: Config, casino: Casino, relayer: Relayer, images: NftImages | None = None) -> None:
    """Каждые 10 минут обновляет запас моделей у релейера и цены с маркета."""
    while True:
        try:
            await sync_nfts(casino.db, relayer)
        except Exception:
            log.warning("Не удалось обновить NFT-модели")
        try:
            await catalog_keep_fresh(casino.db)
        except Exception:
            log.warning("Не удалось обновить каталог NFT")
        try:
            await reprice_demo(casino.db, relayer)
        except Exception:
            log.warning("Не удалось обновить цены демо-NFT")
        try:
            await top_up_demo(casino, relayer)
        except Exception as e:
            log.warning("Не удалось добавить демо-NFT: %s", type(e).__name__)
        if images is not None:
            try:
                await prewarm_images(casino, images)
            except Exception:
                log.warning("Не удалось прогреть картинки NFT")
        await asyncio.sleep(600)


async def catalog_loop(casino: Casino, relayer: Relayer) -> None:
    """Каталог NFT: по кругу обходит все коллекции на маркете Telegram и обновляет модели с их флором."""
    await asyncio.sleep(60)
    while True:
        if not relayer.ready:
            await asyncio.sleep(300)
            continue
        try:
            names = await relayer.collections()
        except Exception as e:
            log.warning("Список коллекций недоступен: %s", type(e).__name__)
            await asyncio.sleep(300)
            continue
        total = 0
        for name in names:
            try:
                rate = await relayer.market.ton_rate()
            except Exception:
                rate = None
            try:
                total += await catalog_store(casino.db, name, emoji_for(name), await relayer.market_floors(name, rate))
            except Exception as e:
                log.warning("Каталог: коллекция %s не обновлена: %s", name, type(e).__name__)
            await asyncio.sleep(2)
        log.info("Каталог NFT: %s коллекций, %s моделей на продаже", len(names), total)
        await asyncio.sleep(600)


async def sports_loop(bot: Bot, sports: Sportsbook) -> None:
    """Раз в 10 минут: коэффициенты (сама модель решает, пора ли) и расчёт сыгранных матчей."""
    async def notify(user_id: int, text: str) -> None:
        await bot.send_message(user_id, text)

    while True:
        try:
            await sports.refresh_odds()
            await sports.settle_pending(notify)
        except Exception:
            log.exception("Ошибка раздела футбола")
        await asyncio.sleep(600)


async def channel_loop(bot: Bot, casino: Casino) -> None:
    """Раз в минуту постит в канал крупные выигрыши и NFT."""
    from .channel import post_wins
    while True:
        try:
            await post_wins(bot, casino.db)
        except Exception as e:
            log.warning("Канал: не удалось опубликовать выигрыши: %s", type(e).__name__)
        await asyncio.sleep(60)


async def gifts_loop(bot: Bot, cfg: Config, casino: Casino, relayer: Relayer) -> None:
    """Каждые 20 секунд зачисляет подарки, которые игроки прислали релейеру."""
    while True:
        await scan_gifts(bot, cfg, casino, relayer)
        await asyncio.sleep(20)


async def scan_gifts(bot: Bot, cfg: Config, casino: Casino, relayer: Relayer) -> None:
    try:
        await notify_deposits(bot, await gifts_scan(casino.db, cfg, relayer))
    except Exception:
        log.warning("Не удалось проверить подарки игроков", exc_info=True)


async def ton_loop(bot: Bot, casino: Casino) -> None:
    """Каждые 20 секунд ищет новые переводы TON на кошелёк казино."""
    while True:
        try:
            await ton_mod.notify(bot, casino.db, await ton_mod.scan(casino.db))
        except Exception as e:
            log.warning("Не удалось проверить переводы TON: %s", e)
        await asyncio.sleep(20)


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
        try:
            await bot.set_my_name("Triple Gifts")
        except Exception:
            log.warning("Имя бота не обновлено")
        try:
            await bot.set_my_short_description("🎁 Казино на подарках Telegram: слоты, краш, кейсы с NFT, "
                                               "апгрейд и PvP. Новости — @TripleGifts")
            await bot.set_my_description(
                "🎁 Triple Gifts — казино на подарках Telegram.\n\n"
                "🎰 Слоты · 🚀 Краш · 💣 Мины · 🟣 Plinko · 📦 Кейсы с NFT · ⬆️ Апгрейд · ⚔️ PvP\n"
                "💸 Пополнение — Stars и TON, вывод — подарками и NFT.\n\n"
                "Нажмите «Старт» и откройте мини-приложение. Канал: @TripleGifts. 18+")
        except Exception:
            log.warning("Описание бота не обновлено")
        if cfg.webapp_url:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Triple Gifts", web_app=WebAppInfo(url=cfg.webapp_url))
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
    if not await db.kv_get("leaders:reset_v1"):          # разовый сброс таблицы лидеров при переезде на Triple Gifts
        await casino.leaders_reset()
        await db.kv_set("leaders:reset_v1", "1")
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    relayer = Relayer(db, cfg.bot_token)
    images = NftImages(Path(cfg.db_path).parent / "nftimg", relayer=relayer, db=db)
    log.info("Картинки NFT из базы: %s", await images.load_saved())
    casino.ton_rate = relayer.market.ton_rate          # курс TON → звёзды (ручной /tonrate или авто)

    async def on_relayer_message(user_id: int) -> None:
        # игрок написал релейеру или прислал подарок: отдаём ждущие NFT и зачисляем подарки
        await deliver_waiting(bot, db, cfg, relayer, user_id)
        try:
            await approve_waiting(bot, casino, relayer, user_id)
        except Exception:
            log.warning("Не удалось отправить ждущий вывод игроку %s", user_id, exc_info=True)
        await scan_gifts(bot, cfg, casino, relayer)

    if await relayer.start():
        relayer.on_private_message(on_relayer_message)

    sports = Sportsbook(casino)
    runner = web.AppRunner(build_app(cfg, casino, bot, relayer, images, sports), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.port).start()
    dp = Dispatcher()
    dp.include_router(build_router(
        cfg, casino, relayer,
        on_relayer_ready=lambda: relayer.on_private_message(on_relayer_message),
    ))
    await setup_bot_ui(bot, cfg)
    bg = asyncio.create_task(background(casino, bot))
    crash_task = asyncio.create_task(crash_loop(casino))
    nft_task = asyncio.create_task(nft_loop(bot, cfg, casino, relayer, images))
    gifts_task = asyncio.create_task(gifts_loop(bot, cfg, casino, relayer))
    ton_task = asyncio.create_task(ton_loop(bot, casino))
    channel_task = asyncio.create_task(channel_loop(bot, casino))
    sports_task = asyncio.create_task(sports_loop(bot, sports))
    catalog_task = asyncio.create_task(catalog_loop(casino, relayer))

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
        gifts_task.cancel()
        ton_task.cancel()
        channel_task.cancel()
        sports_task.cancel()
        catalog_task.cancel()
        await relayer.stop()
        await runner.cleanup()
        await db.close()
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
