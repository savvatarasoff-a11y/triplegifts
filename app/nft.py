"""NFT-призы по моделям.

На сервере хранятся только модели (коллекция + модель) с запасом у релейера и реальной ценой —
полом маркета Telegram в звёздах. Выигравшему релейер передаёт случайный подарок этой модели.
Всё через MTProto-аккаунт релейера (relayer.py): Telegram временно запрещает бизнес-ботам,
подключённым к аккаунту владельца, управлять подарками.
"""
from __future__ import annotations

import html
import logging
import random
import time
from collections import defaultdict
from typing import Any

from aiogram import Bot

from .config import Config
from .db import Database
from .relayer import Relayer, RelayerError

log = logging.getLogger(__name__)

PRICE_MAX_AGE = 3600   # модель без проверенной за час цены в кейс не попадает


async def casino_stock(db: Database, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Оставляет только подарки казино: не присланные игроками (они принадлежат игрокам, см. gifts.py).

    Подарок, полученный после включения приёма подарков, считается запасом, только когда сканер
    его учёл (прислал админ или игрок продал казино) — иначе его могли бы отдать до зачисления.
    """
    since = await db.kv_get("gifts:since")
    since_ts = float(since) if since is not None else float("inf")
    status = {r["ref"]: r["status"] for r in await db.all("SELECT ref, status FROM user_gifts")}
    return [i for i in items
            if status.get(str(i["ref"])) in ("stock", "sold", "lost")
            or (str(i["ref"]) not in status and (not i.get("from_user") or i.get("date", 0) < since_ts))]


async def sync(db: Database, relayer: Relayer) -> tuple[int, str | None]:
    """Пересчитывает запас моделей у релейера и обновляет их рыночные цены. Возвращает (моделей, ошибка)."""
    if not relayer.ready:
        return 0, "Релейер не подключён — выполните вход: /relayer"
    inventory = await casino_stock(db, await relayer.inventory())
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in inventory:
        groups[(item["collection_id"], item["model"])].append(item)
    now = time.time()
    price_error = None
    rows = []
    for (collection_id, model), items in groups.items():
        first = items[0]
        try:
            price = await relayer.floor_price(collection_id, model, first["collection_name"])
        except Exception as e:
            price, price_error = None, f"не удалось получить цены с маркета: {type(e).__name__}"
            log.warning("Цена модели %s/%s недоступна: %s", collection_id, model, e)
        rows.append((collection_id, first["collection_name"], model, first["rarity"], first["emoji"], len(items),
                     price, now if price else None))
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
    price = f"флор MRKT {m['price']} ⭐" if m.get("price") and fresh else "цена не проверена"
    free = m["stock"] - m["reserved"]
    off = "" if m.get("enabled", 1) else " · выключена"
    return (f"{m.get('emoji') or '💎'} <b>{html.escape(m['collection_name'])}</b> · модель "
            f"«{html.escape(m['model'])}»{rarity} — {price}, у релейера {m['stock']} (свободно {free}){off}")


async def _notify_admins(bot: Bot, cfg: Config, text: str) -> None:
    for admin_id in cfg.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


async def deliver(bot: Bot, db: Database, cfg: Config, relayer: Relayer, win_id: int) -> tuple[bool, str]:
    """Передаёт победителю случайный подарок выигранной модели с аккаунта-релейера."""
    async with db.tx() as c:
        cur = await c.execute(
            "UPDATE nft_wins SET status='sending' WHERE id=? AND status IN ('won','failed','waiting')", (win_id,)
        )
    if cur.rowcount != 1:
        return False, "Выигрыш уже передан или не найден"
    win = await db.one("SELECT * FROM nft_wins WHERE id=?", win_id)
    model = await db.one("SELECT * FROM nft_models WHERE id=?", win["model_id"])
    user = await db.get_user(win["user_id"])

    async def set_status(status: str, error: str | None = None) -> None:
        async with db.tx() as c:
            await c.execute("UPDATE nft_wins SET status=?, error=? WHERE id=?", (status, error, win_id))

    async def fail(reason: str) -> tuple[bool, str]:
        await set_status("failed", reason[:200])
        await _notify_admins(bot, cfg,
                             f"⚠️ Не удалось передать NFT игроку <code>{win['user_id']}</code>: {describe(model)}\n"
                             f"Причина: {html.escape(reason)}\nПовторить: /nftsend {win_id}")
        return False, reason

    try:
        inventory = await relayer.inventory()
    except Exception as e:
        return await fail(f"не удалось получить подарки релейера: {type(e).__name__}")
    busy = {r["owned_gift_id"] for r in await db.all(
        "SELECT owned_gift_id FROM nft_wins WHERE status='sent' AND owned_gift_id IS NOT NULL")}
    candidates = [i for i in await casino_stock(db, inventory) if (i["collection_id"], i["model"]) == (model["collection_id"], model["model"])
                  and str(i["ref"]) not in busy]
    if not candidates:
        return await fail("у релейера не осталось подарков этой модели")
    item = random.SystemRandom().choice(candidates)
    try:
        await relayer.transfer(item, win["user_id"], (user or {}).get("username"))
    except RelayerError as e:
        if str(e) == "NEED_CONTACT":
            # Релейер не может найти игрока без @username — просим игрока написать релейеру
            await set_status("waiting", "игрок должен написать релейеру")
            relayer_name = await relayer.username()
            contact = f"@{relayer_name}" if relayer_name else "аккаунту-релейеру"
            try:
                await bot.send_message(
                    win["user_id"],
                    f"🎁 Вы выиграли NFT {model.get('emoji') or '💎'} <b>{html.escape(model['collection_name'])}</b> "
                    f"(модель «{html.escape(model['model'])}»)!\nЧтобы получить его, напишите любое сообщение "
                    f"{contact} — подарок придёт сразу.",
                )
            except Exception:
                pass
            return False, "ждём, пока игрок напишет релейеру"
        return await fail(str(e))
    except Exception as e:
        return await fail(str(getattr(e, "message", None) or type(e).__name__))
    async with db.tx() as c:
        await c.execute("UPDATE nft_wins SET status='sent', owned_gift_id=?, error=NULL, sent_at=? WHERE id=?",
                        (str(item["ref"]), time.time(), win_id))
        await c.execute("UPDATE nft_models SET stock=MAX(stock-1,0), reserved=MAX(reserved-1,0) WHERE id=?",
                        (model["id"],))
    try:
        await bot.send_message(
            win["user_id"],
            f"🎉 Вам передан NFT из кейса: {model.get('emoji') or '💎'} <b>{html.escape(item['collection_name'])} "
            f"#{item['number']}</b>, модель «{html.escape(model['model'])}». Он уже в вашем профиле.",
        )
    except Exception:
        pass
    return True, f"Передан {item['collection_name']} #{item['number']}"


async def deliver_waiting(bot: Bot, db: Database, cfg: Config, relayer: Relayer, user_id: int) -> None:
    """Игрок написал релейеру — отдаём ему всё, что ждало контакта."""
    for win in await db.all("SELECT id FROM nft_wins WHERE user_id=? AND status='waiting'", user_id):
        await deliver(bot, db, cfg, relayer, win["id"])
