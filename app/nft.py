"""NFT-призы по моделям.

На сервере хранятся только модели (коллекция + модель) с запасом у релейера и реальной ценой —
полом маркета Telegram в звёздах. Выигравшему релейер передаёт случайный подарок этой модели.
Инвентарь и передача — через бизнес-подключение релейера (Bot API), цены — через MTProto (relayer.py).
"""
from __future__ import annotations

import html
import logging
import random
import time
from collections import defaultdict

from aiogram import Bot
from aiogram.types import OwnedGiftUnique

from .config import Config
from .db import Database
from .relayer import Relayer

log = logging.getLogger(__name__)

PRICE_MAX_AGE = 3600   # модель без проверенной за час цены в кейс не попадает


async def relayer_connection(db: Database, cfg: Config) -> dict | None:
    """Бизнес-подключение аккаунта-релейера (аккаунт админа) с правом управлять подарками."""
    rows = await db.all(
        "SELECT * FROM business_connections WHERE is_enabled=1 AND can_gifts=1 ORDER BY updated_at DESC"
    )
    return next((r for r in rows if r["user_id"] in cfg.admin_ids), None)


async def relayer_inventory(bot: Bot, conn: dict) -> list[OwnedGiftUnique]:
    gifts: list[OwnedGiftUnique] = []
    offset = None
    while True:
        page = await bot.get_business_account_gifts(
            business_connection_id=conn["id"], exclude_unlimited=True, exclude_limited_upgradable=True,
            exclude_limited_non_upgradable=True, offset=offset, limit=100,
        )
        gifts += [g for g in page.gifts if isinstance(g, OwnedGiftUnique) and g.can_be_transferred]
        offset = page.next_offset
        if not offset:
            return gifts


def model_key(owned: OwnedGiftUnique) -> tuple[str, str]:
    gift = owned.gift
    return gift.gift_id, gift.model.name if gift.model else "—"


async def sync(bot: Bot, db: Database, cfg: Config, relayer: Relayer) -> tuple[int, str | None]:
    """Пересчитывает запас моделей у релейера и обновляет их рыночные цены. Возвращает (моделей, ошибка)."""
    conn = await relayer_connection(db, cfg)
    if not conn:
        return 0, ("Релейер не подключён к боту. На аккаунте с NFT: Настройки → Telegram для бизнеса → "
                   "Чат-боты → выберите бота и включите права на подарки и звёзды.")
    inventory = await relayer_inventory(bot, conn)
    groups: dict[tuple[str, str], list[OwnedGiftUnique]] = defaultdict(list)
    for owned in inventory:
        groups[model_key(owned)].append(owned)
    now = time.time()
    price_error = None
    rows = []
    for (collection_id, model), items in groups.items():
        gift = items[0].gift
        rarity = gift.model.rarity_per_mille / 10 if gift.model and gift.model.rarity_per_mille is not None else None
        emoji = gift.model.sticker.emoji if gift.model and gift.model.sticker else None
        price = None
        if relayer.ready:
            try:
                price = await relayer.floor_price(int(collection_id), model)
            except Exception as e:
                price_error = f"не удалось получить цены с маркета: {type(e).__name__}"
                log.warning("Цена модели %s/%s недоступна: %s", collection_id, model, e)
        rows.append((collection_id, gift.base_name, model, rarity, emoji, len(items), price, now if price else None))
    if not relayer.ready:
        price_error = "MTProto-вход релейера не выполнен — цены с маркета недоступны (/relayer)"
    async with db.tx() as c:
        await c.execute("UPDATE nft_models SET stock=0")
        await c.executemany(
            "INSERT INTO nft_models(collection_id, collection_name, model, rarity, emoji, stock, price, price_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(collection_id, model) DO UPDATE SET "
            "collection_name=excluded.collection_name, rarity=excluded.rarity, emoji=excluded.emoji, "
            "stock=excluded.stock, price=COALESCE(excluded.price, nft_models.price), "
            "price_at=COALESCE(excluded.price_at, nft_models.price_at)",
            rows,
        )
    return len(groups), price_error


def describe(m: dict) -> str:
    rarity = f" · {m['rarity']:g}%" if m.get("rarity") is not None else ""
    fresh = m.get("price_at") and time.time() - m["price_at"] < PRICE_MAX_AGE
    price = f"пол маркета {m['price']} ⭐" if m.get("price") and fresh else "цена не проверена"
    free = m["stock"] - m["reserved"]
    off = "" if m.get("enabled", 1) else " · выключена"
    return (f"{m.get('emoji') or '💎'} <b>{html.escape(m['collection_name'])}</b> · модель "
            f"«{html.escape(m['model'])}»{rarity} — {price}, у релейера {m['stock']} (свободно {free}){off}")


async def deliver(bot: Bot, db: Database, cfg: Config, win_id: int) -> tuple[bool, str]:
    """Передаёт победителю случайный подарок выигранной модели с аккаунта-релейера."""
    async with db.tx() as c:
        cur = await c.execute(
            "UPDATE nft_wins SET status='sending' WHERE id=? AND status IN ('won','failed')", (win_id,)
        )
    if cur.rowcount != 1:
        return False, "Выигрыш уже передан или не найден"
    win = await db.one("SELECT * FROM nft_wins WHERE id=?", win_id)
    model = await db.one("SELECT * FROM nft_models WHERE id=?", win["model_id"])

    async def fail(reason: str) -> tuple[bool, str]:
        async with db.tx() as c:
            await c.execute("UPDATE nft_wins SET status='failed', error=? WHERE id=?", (reason[:200], win_id))
        for admin_id in cfg.admin_ids:
            try:
                await bot.send_message(
                    admin_id,
                    f"⚠️ Не удалось передать NFT игроку <code>{win['user_id']}</code>: {describe(model)}\n"
                    f"Причина: {html.escape(reason)}\nПовторить: /nftsend {win_id}",
                )
            except Exception:
                pass
        return False, reason

    conn = await relayer_connection(db, cfg)
    if not conn:
        return await fail("релейер не подключён к боту")
    try:
        inventory = await relayer_inventory(bot, conn)
    except Exception as e:
        return await fail(f"не удалось получить подарки релейера: {type(e).__name__}")
    busy = {r["owned_gift_id"] for r in await db.all(
        "SELECT owned_gift_id FROM nft_wins WHERE status IN ('sending','sent') AND owned_gift_id IS NOT NULL")}
    candidates = [g for g in inventory
                  if model_key(g) == (model["collection_id"], model["model"]) and g.owned_gift_id not in busy]
    if not candidates:
        return await fail("у релейера не осталось подарков этой модели")
    owned = random.SystemRandom().choice(candidates)
    try:
        await bot.transfer_gift(
            business_connection_id=conn["id"], owned_gift_id=owned.owned_gift_id,
            new_owner_chat_id=win["user_id"], star_count=owned.transfer_star_count or None,
        )
    except Exception as e:
        return await fail(str(getattr(e, "message", None) or type(e).__name__))
    async with db.tx() as c:
        await c.execute("UPDATE nft_wins SET status='sent', owned_gift_id=?, error=NULL, sent_at=? WHERE id=?",
                        (owned.owned_gift_id, time.time(), win_id))
        await c.execute("UPDATE nft_models SET stock=MAX(stock-1,0), reserved=MAX(reserved-1,0) WHERE id=?",
                        (model["id"],))
    gift = owned.gift
    try:
        await bot.send_message(
            win["user_id"],
            f"🎉 Вам передан NFT из кейса: {model.get('emoji') or '💎'} <b>{html.escape(gift.base_name)} "
            f"#{gift.number}</b>, модель «{html.escape(model['model'])}». Он уже в вашем профиле.",
        )
    except Exception:
        pass
    return True, f"Передан {gift.base_name} #{gift.number}"
