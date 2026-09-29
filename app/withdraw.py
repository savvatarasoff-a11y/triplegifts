"""Вывод звёзд подарками Telegram: каталог подарков, заявки админу, отправка."""
from __future__ import annotations

import html
import logging
import time
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .casino import Casino, display_name
from .config import Config
from .relayer import Relayer, RelayerError

log = logging.getLogger(__name__)

CATALOG_TTL = 600  # список подарков обновляем раз в 10 минут


class GiftCatalog:
    """Обычные подарки Telegram, которые бот может отправить за свои звёзды."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self._cache: list[dict[str, Any]] = []
        self._ts = 0.0

    async def list(self) -> list[dict[str, Any]]:
        if self._cache and time.time() - self._ts < CATALOG_TTL:
            return self._cache
        gifts = await self.bot.get_available_gifts()
        items = []
        for gift in gifts.gifts:
            if getattr(gift, "is_premium", False) or gift.remaining_count == 0:
                continue
            items.append({
                "id": gift.id,
                "stars": gift.star_count,
                "emoji": (gift.sticker.emoji if gift.sticker else None) or "🎁",
            })
        items.sort(key=lambda g: (g["stars"], g["id"]))
        self._cache, self._ts = items, time.time()
        return items

    async def get(self, gift_id: str) -> dict[str, Any] | None:
        return next((g for g in await self.list() if g["id"] == gift_id), None)


def admin_keyboard(wd_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data=f"w:ok:{wd_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"w:no:{wd_id}"),
    ]])


async def notify_admins(bot: Bot, cfg: Config, casino: Casino, wd: dict) -> None:
    user = await casino.db.get_user(wd["user_id"])
    stats = (
        f"пополнил {user['deposited']} ⭐, поставил {user['wagered']} ⭐, выиграл {user['won']} ⭐"
        if user else "нет данных"
    )
    username = f" @{html.escape(user['username'])}" if user and user.get("username") else ""
    text = (
        f"💸 <b>Заявка на вывод №{wd['id']}</b>\n"
        f"Игрок: {html.escape(display_name(user))}{username} (<code>{wd['user_id']}</code>)\n"
        f"Подарок: {wd.get('emoji') or wd.get('gift_emoji') or '🎁'} за <b>{wd['amount']} ⭐</b>\n"
        f"Статистика: {stats}"
    )
    for admin_id in cfg.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=admin_keyboard(wd["id"]))
        except Exception:
            log.warning("Не удалось уведомить админа %s о заявке", admin_id)


NEED_CONTACT = "NEED_CONTACT"   # релейер не нашёл игрока: отправим, как только игрок ему напишет


async def approve(bot: Bot, casino: Casino, wd_id: int, admin_id: int,
                  relayer: Relayer | None = None) -> tuple[bool, str]:
    """Отправляет подарок со звёзд аккаунта-релейера. Возвращает (успех, текст для админа)."""
    if relayer is not None and not relayer.ready:
        return False, "Релейер не подключён — выводы идут с его звёзд. Войдите: /relayer"
    wd = await casino.withdraw_claim(wd_id, admin_id)
    if wd is None:
        return False, "Заявка уже обработана"
    text = "Вывод из Svag Gifts 🎁"
    try:
        if relayer is not None:
            user = await casino.db.get_user(wd["user_id"])
            await relayer.send_gift(wd["gift_id"], wd["user_id"], (user or {}).get("username"), text)
        else:
            await bot.send_gift(gift_id=wd["gift_id"], user_id=wd["user_id"], text=text)
    except RelayerError as e:
        if str(e) != NEED_CONTACT:
            return await _failed(casino, wd_id, str(e))
        # Игрок без @username: подарок уйдёт автоматически, как только он напишет релейеру
        await casino.withdraw_finish(wd_id, ok=False, error=NEED_CONTACT)
        name = await relayer.username() if relayer else None
        contact = f"@{name}" if name else "аккаунту казино"
        try:
            await bot.send_message(wd["user_id"], f"🎁 Вывод №{wd_id} одобрен! Чтобы получить подарок, напишите любое "
                                                  f"сообщение {contact} — он придёт сразу.")
        except Exception:
            pass
        return False, f"Игрок не найден релейером — попросили его написать {contact}, подарок уйдёт автоматически"
    except Exception as e:
        return await _failed(casino, wd_id, str(getattr(e, "message", None) or type(e).__name__))
    await casino.withdraw_finish(wd_id, ok=True)
    try:
        await bot.send_message(
            wd["user_id"],
            f"✅ Вывод №{wd_id} выполнен: вам отправлен подарок {wd['gift_emoji'] or '🎁'} "
            f"за {wd['amount']} ⭐. Его можно оставить или обменять на звёзды в своём профиле.",
        )
    except Exception:
        pass
    return True, f"✅ Подарок по заявке №{wd_id} отправлен"


async def _failed(casino: Casino, wd_id: int, reason: str) -> tuple[bool, str]:
    await casino.withdraw_finish(wd_id, ok=False, error=reason[:200])
    log.warning("Подарок по заявке %s не отправлен: %s", wd_id, reason)
    return False, (
        f"Не получилось отправить подарок: {reason}.\n"
        "Проверьте баланс звёзд релейера (/stars). Заявка снова ждёт решения."
    )


async def approve_waiting(bot: Bot, casino: Casino, relayer: Relayer, user_id: int) -> None:
    """Игрок написал релейеру — отправляем одобренные выводы, которые ждали контакта."""
    for wd in await casino.db.all("SELECT id, admin_id FROM withdrawals WHERE user_id=? AND status='pending' "
                                  "AND error=?", user_id, NEED_CONTACT):
        await approve(bot, casino, wd["id"], wd["admin_id"] or 0, relayer)


async def reject(bot: Bot, casino: Casino, wd_id: int, admin_id: int) -> tuple[bool, str]:
    wd = await casino.withdraw_reject(wd_id, admin_id)
    if wd is None:
        return False, "Заявка уже обработана"
    try:
        await bot.send_message(
            wd["user_id"],
            f"❌ Заявка на вывод №{wd_id} отклонена, {wd['amount']} ⭐ вернулись на баланс.",
        )
    except Exception:
        pass
    return True, f"❌ Заявка №{wd_id} отклонена, звёзды возвращены игроку"
