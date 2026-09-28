"""NFT-подарки админа: синхронизация с бизнес-аккаунтом, цены и передача победителям."""
from __future__ import annotations

import html
import logging
import time

from aiogram import Bot
from aiogram.types import OwnedGiftUnique

from .config import Config
from .db import Database

log = logging.getLogger(__name__)


async def admin_connection(db: Database, cfg: Config) -> dict | None:
    """Активное бизнес-подключение админа с правом управлять подарками."""
    rows = await db.all(
        "SELECT * FROM business_connections WHERE is_enabled=1 AND can_gifts=1 ORDER BY updated_at DESC"
    )
    return next((r for r in rows if r["user_id"] in cfg.admin_ids), None)


async def sync(bot: Bot, db: Database, cfg: Config) -> tuple[int, str | None]:
    """Подтягивает NFT с бизнес-аккаунта админа. Возвращает (число NFT, ошибка)."""
    conn = await admin_connection(db, cfg)
    if not conn:
        return 0, ("Бот не подключён к вашему аккаунту с правом управлять подарками. "
                   "Telegram → Настройки → Telegram для бизнеса → Чат-боты → выберите бота и "
                   "включите «Подарки и звёзды» (просмотр и передача подарков).")
    seen: set[str] = set()
    offset = None
    while True:
        page = await bot.get_business_account_gifts(
            business_connection_id=conn["id"], exclude_unlimited=True, exclude_limited_upgradable=True,
            exclude_limited_non_upgradable=True, offset=offset, limit=100,
        )
        rows = []
        for owned in page.gifts:
            if not isinstance(owned, OwnedGiftUnique) or not owned.can_be_transferred:
                continue
            gift = owned.gift
            model = gift.model
            rarity = (model.rarity_per_mille / 10) if model and model.rarity_per_mille is not None else None
            emoji = model.sticker.emoji if model and model.sticker else None
            seen.add(owned.owned_gift_id)
            rows.append((owned.owned_gift_id, conn["id"], f"{gift.base_name} #{gift.number}",
                         model.name if model else None, rarity, emoji, owned.transfer_star_count or 0, time.time()))
        async with db.tx() as c:
            await c.executemany(
                "INSERT INTO nft_prizes(owned_gift_id, connection_id, title, model, rarity, emoji, transfer_cost, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(owned_gift_id) DO UPDATE SET connection_id=excluded.connection_id, "
                "title=excluded.title, model=excluded.model, rarity=excluded.rarity, emoji=excluded.emoji, "
                "transfer_cost=excluded.transfer_cost, updated_at=excluded.updated_at, "
                "status=CASE WHEN nft_prizes.status='gone' THEN 'available' ELSE nft_prizes.status END",
                rows,
            )
        offset = page.next_offset
        if not offset:
            break
    # NFT, которых больше нет в аккаунте, выводим из кейса
    stale = [r["id"] for r in await db.all("SELECT id, owned_gift_id FROM nft_prizes WHERE status='available'")
             if r["owned_gift_id"] not in seen]
    if stale:
        async with db.tx() as c:
            await c.executemany("UPDATE nft_prizes SET status='gone' WHERE id=?", [(i,) for i in stale])
    return len(seen), None


def describe(n: dict) -> str:
    rarity = f", редкость модели {n['rarity']:g}%" if n.get("rarity") is not None else ""
    model = f" · модель «{html.escape(n['model'])}»{rarity}" if n.get("model") else ""
    price = f"{n['price']} ⭐" if n.get("price") else "цена не задана"
    return f"{n.get('emoji') or '💎'} <b>{html.escape(n['title'])}</b>{model} — {price}"


async def deliver(bot: Bot, db: Database, cfg: Config, nft_id: int) -> tuple[bool, str]:
    """Передаёт выигранный NFT победителю через бизнес-аккаунт админа."""
    async with db.tx() as c:
        cur = await c.execute(
            "UPDATE nft_prizes SET status='sending', updated_at=? WHERE id=? AND status IN ('won','failed')",
            (time.time(), nft_id),
        )
    if cur.rowcount != 1:
        return False, "NFT уже передан или недоступен"
    n = await db.one("SELECT * FROM nft_prizes WHERE id=?", nft_id)
    try:
        await bot.transfer_gift(
            business_connection_id=n["connection_id"], owned_gift_id=n["owned_gift_id"],
            new_owner_chat_id=n["winner_id"], star_count=n["transfer_cost"] or None,
        )
    except Exception as e:
        reason = str(getattr(e, "message", None) or type(e).__name__)[:200]
        async with db.tx() as c:
            await c.execute("UPDATE nft_prizes SET status='failed', error=?, updated_at=? WHERE id=?",
                            (reason, time.time(), nft_id))
        for admin_id in cfg.admin_ids:
            try:
                await bot.send_message(
                    admin_id,
                    f"⚠️ Не удалось передать выигранный NFT игроку <code>{n['winner_id']}</code>:\n{describe(n)}\n"
                    f"Причина: {html.escape(reason)}\nПовторить: /nftsend {nft_id}",
                )
            except Exception:
                pass
        return False, reason
    async with db.tx() as c:
        await c.execute("UPDATE nft_prizes SET status='sent', error=NULL, updated_at=? WHERE id=?",
                        (time.time(), nft_id))
    try:
        await bot.send_message(n["winner_id"], f"🎉 Вам передан NFT-подарок из кейса: {describe(n)}")
    except Exception:
        pass
    return True, "NFT передан"
