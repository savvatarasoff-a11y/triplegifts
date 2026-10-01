"""Канал проекта (@TripleGifts): оформление, приветственный пост и автопостинг крупных выигрышей.

Бот должен быть админом канала с правами «публикация», «изменение профиля канала» и «закрепление».
"""
from __future__ import annotations

import html
import io
import json
import logging
import os
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from .casino import display_name
from .db import Database

log = logging.getLogger(__name__)

CHANNEL = os.getenv("CHANNEL", "").strip() or "@TripleGifts"
TITLE = "Triple Gifts — казино на подарках"
DESCRIPTION = ("🎁 Казино на подарках Telegram: слоты, краш, мины, кейсы с NFT, апгрейд и PvP.\n"
               "Пополнение — ⭐ Stars и 💎 TON, вывод — подарками и NFT.\n"
               "Новости, чеки и крупные выигрыши — здесь. 18+")
AVATAR = Path(__file__).resolve().parent.parent / "webapp" / "avatar.png"
WINS_KEY = "channel:wins_after"
WINS_ON_KEY = "channel:wins"
BIG_WIN_STARS = 1000     # выигрыш от 1000 ⭐ …
BIG_WIN_X = 10           # … и от ×10 — в канал; NFT — всегда

GAME_NAMES = {"slots": "🎰 Слоты", "crash": "🚀 Краш", "mines": "💣 Мины", "plinko": "🟣 Plinko", "pickaxe": "⛏ Кирка", "case": "📦 Кейс",
              "upgrade": "⬆️ Апгрейд", "pvp": "⚔️ PvP-рулетка", "hockey": "🏒 PvP-хоккей"}


def welcome_text() -> str:
    return (
        "🎁 <b>Triple Gifts — казино на подарках Telegram</b>\n\n"
        "Добро пожаловать! Играем на звёзды ⭐, TON 💎 и NFT-подарки — всё прямо в Telegram.\n\n"
        "🎰 <b>Слоты</b> — 777 даёт NFT-джекпот\n"
        "🚀 <b>Краш</b> — ставь звёзды или свои NFT\n"
        "💣 <b>Мины</b> и 🟣 <b>Plinko</b>\n"
        "📦 <b>Кейсы</b> — подарки и NFT, можно открыть сразу несколько\n"
        "⬆️ <b>Апгрейд</b> — поставь свои NFT и забери модель дороже\n"
        "⚔️ <b>PvP</b> — рулетка и хоккей игрок против игрока\n\n"
        "💸 Пополнение — Telegram Stars и TON\n"
        "🎁 Вывод — подарками и NFT прямо в Telegram\n"
        "👑 VIP-уровни с рейкбеком, ежедневный бонус и 10% с пополнений друзей\n"
        "🧾 Чеки — раздавай звёзды друзьям\n\n"
        "В канале: новости, раздачи чеков и крупные выигрыши игроков.\n\n"
        "<i>Играйте ответственно. 18+</i>"
    )


BANNER = Path(__file__).resolve().parent.parent / "webapp" / "banner.png"


def banner() -> bytes:
    """Баннер канала 1280×640: слот «777», подарки и монеты, одна подпись TRIPLE GIFTS (исходник — webapp/banner.svg)."""
    return BANNER.read_bytes()


def play_keyboard(bot_username: str | None) -> InlineKeyboardMarkup | None:
    if not bot_username:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Играть в Triple Gifts", url=f"https://t.me/{bot_username}?start=channel")],
    ])


async def setup(bot: Bot) -> list[str]:
    """Оформляет канал и публикует закреплённый приветственный пост. Возвращает отчёт по шагам."""
    report = []
    me = await bot.get_me()

    async def step(name: str, coro) -> Any:
        try:
            res = await coro
            report.append(f"✅ {name}")
            return res
        except Exception as e:
            report.append(f"⚠️ {name}: {html.escape(str(getattr(e, 'message', e)))[:150]}")
            return None

    await step("название", bot.set_chat_title(CHANNEL, TITLE))
    await step("описание", bot.set_chat_description(CHANNEL, DESCRIPTION))
    await step("аватарка", bot.set_chat_photo(CHANNEL, BufferedInputFile(AVATAR.read_bytes(), "avatar.png")))
    msg = await step("приветственный пост", bot.send_photo(
        CHANNEL, BufferedInputFile(banner(), "triple_gifts.png"), caption=welcome_text(),
        reply_markup=play_keyboard(me.username)))
    if msg is not None:
        await step("закреп", bot.pin_chat_message(CHANNEL, msg.message_id, disable_notification=True))
    return report


def win_text(r: dict[str, Any]) -> str | None:
    """Текст поста о выигрыше из строки bets (+ имя игрока)."""
    try:
        detail = json.loads(r["detail"] or "{}")
    except ValueError:
        detail = {}
    name = html.escape(display_name({"id": r["user_id"], "first_name": r["first_name"], "username": r["username"]}))
    game = GAME_NAMES.get(r["game"], r["game"])
    model = detail.get("model") or detail.get("demo_nft")
    if model and (detail.get("nft_win") or detail.get("demo_nft")):
        title = detail.get("title") or "NFT"
        demo = " (демо)" if detail.get("demo_nft") else ""
        return f"💎 <b>{name}</b> выбил NFT <b>{html.escape(title)} «{html.escape(str(model))}»</b>{demo} — {game}!"
    if r["cur"] != "stars":
        return None
    x = r["win"] / r["bet"] if r["bet"] else 0
    return f"🔥 <b>{name}</b> выиграл <b>{r['win']:,} ⭐</b> (×{x:.1f}) — {game}!".replace(",", " ")


async def post_wins(bot: Bot, db: Database) -> int:
    """Публикует в канал новые крупные выигрыши (NFT — всегда). Возвращает, сколько опубликовано."""
    if await db.kv_get(WINS_ON_KEY) == "off":
        return 0
    last = await db.kv_get(WINS_KEY)
    if last is None:                               # первый запуск — старые выигрыши не постим
        row = await db.one("SELECT COALESCE(MAX(id),0) m FROM bets")
        await db.kv_set(WINS_KEY, str(row["m"]))
        return 0
    rows = await db.all(
        "SELECT b.id, b.user_id, b.game, b.bet, b.win, b.detail, b.cur, u.first_name, u.username FROM bets b "
        "LEFT JOIN users u ON u.id=b.user_id WHERE b.id > ? AND b.win > 0 AND ("
        "b.detail LIKE '%nft_win%' OR b.detail LIKE '%demo_nft%' OR "
        "(b.cur='stars' AND b.win >= ? AND b.win >= b.bet * ?)) ORDER BY b.id LIMIT 10",
        int(last), BIG_WIN_STARS, BIG_WIN_X)
    me = await bot.get_me()
    posted = 0
    for r in rows:
        text = win_text(r)
        if text:
            try:
                await bot.send_message(CHANNEL, text, reply_markup=play_keyboard(me.username),
                                       disable_notification=True)
                posted += 1
            except Exception as e:
                log.warning("Не удалось запостить выигрыш в канал: %s", e)
                break
        await db.kv_set(WINS_KEY, str(r["id"]))
    return posted
