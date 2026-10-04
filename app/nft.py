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
from .relayer import Relayer

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
        await c.execute("UPDATE nft_models SET stock=0 WHERE test=0")   # демо-модели не лежат у релейера
        await c.executemany(
            "INSERT INTO nft_models(collection_id, collection_name, model, rarity, emoji, stock, price, price_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(collection_id, model) DO UPDATE SET "
            "collection_name=excluded.collection_name, rarity=excluded.rarity, emoji=excluded.emoji, "
            "stock=excluded.stock, price=COALESCE(excluded.price, nft_models.price), "
            "price_at=COALESCE(excluded.price_at, nft_models.price_at)",
            rows,
        )
    return len(groups), price_error


CATALOG_KEEP = 6 * 3600   # модель из каталога, не встреченная на маркете столько времени, пропадает из кейсов


async def catalog_store(db: Database, collection: str, emoji: str, floors: list[dict[str, Any]]) -> int:
    """Модели коллекции с маркета Telegram — в общий список NFT (как демо: подарков у релейера нет,
    выигрыш платится флором). Выключенные админом (/nftoff) остаются выключенными."""
    now = time.time()
    async with db.tx() as c:
        for f in floors:
            if str(f["model"]).isdigit():
                continue
            await c.execute(
                "INSERT INTO nft_models(collection_id, collection_name, model, rarity, emoji, stock, price, price_at, "
                "seen_at, test) VALUES (?,?,?,?,?,999,?,?,?,1) ON CONFLICT(collection_id, model) DO UPDATE SET "
                "stock=999, rarity=COALESCE(excluded.rarity, nft_models.rarity), price=excluded.price, "
                "price_at=excluded.price_at, seen_at=excluded.seen_at, test=1",
                (f"demo:{collection}", collection, f["model"], f.get("rarity"), emoji, f["price"], now, now))
            await c.execute("UPDATE user_gifts SET value=?, priced_at=? WHERE test=1 AND collection_name=? AND model=? "
                            "AND status IN ('owned','staked')", (f["price"], now, collection, f["model"]))
    return len(floors)


async def catalog_keep_fresh(db: Database) -> None:
    """Каталог обходится дольше часа: цена модели, встреченной на маркете за CATALOG_KEEP, считается актуальной."""
    now = time.time()
    async with db.tx() as c:
        await c.execute("UPDATE nft_models SET price_at=? WHERE test=1 AND seen_at > ?", (now, now - CATALOG_KEEP))


async def reprice_demo(db: Database, relayer: Relayer) -> None:
    """Демо-NFT, взятые с MRKT (не из каталога): флор раз в 10 минут. Если MRKT недоступен — остаётся последняя цена."""
    now = time.time()
    for m in await db.all("SELECT id, collection_name, model, price FROM nft_models WHERE test=1 AND seen_at IS NULL"):
        price = None
        try:
            price = await relayer.floor_price(None, m["model"], m["collection_name"])
        except Exception as e:
            log.warning("Цена демо-модели %s недоступна: %s", m["model"], type(e).__name__)
        price = price or m["price"]
        async with db.tx() as c:
            await c.execute("UPDATE nft_models SET price=?, price_at=? WHERE id=?", (price, now, m["id"]))
            await c.execute("UPDATE user_gifts SET value=?, priced_at=? WHERE test=1 AND collection_name=? AND model=? "
                            "AND status IN ('owned','staked')", (price, now, m["collection_name"], m["model"]))


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
    """Кладёт победителю в «Мои подарки» случайный подарок выигранной модели с аккаунта-релейера."""
    async with db.tx() as c:
        cur = await c.execute(
            "UPDATE nft_wins SET status='sending' WHERE id=? AND status IN ('won','failed','waiting')", (win_id,)
        )
    if cur.rowcount != 1:
        return False, "Выигрыш уже передан или не найден"
    win = await db.one("SELECT * FROM nft_wins WHERE id=?", win_id)
    model = await db.one("SELECT * FROM nft_models WHERE id=?", win["model_id"])

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
    # Выигрыш кладём в «Мои подарки» игрока: оттуда его можно вывести в Telegram, продать или поставить
    now = time.time()
    ref = str(item["ref"])
    async with db.tx() as c:
        values = (win["user_id"], item["collection_id"], item["collection_name"], item["number"], item["model"],
                  item.get("emoji") or model.get("emoji"), item.get("rarity"), model["price"], model["price_at"] or now,
                  item.get("transfer_at") or 0)
        res = await c.execute(
            "UPDATE user_gifts SET user_id=?, kind='nft', collection_id=?, collection_name=?, number=?, model=?, "
            "emoji=?, rarity=?, value=?, priced_at=?, transfer_at=?, status='owned', round_id=NULL WHERE ref=?",
            (*values, ref))
        if res.rowcount == 0:
            await c.execute(
                "INSERT INTO user_gifts(user_id, kind, collection_id, collection_name, number, model, emoji, rarity, "
                "value, priced_at, transfer_at, status, ref, created_at) VALUES (?,'nft',?,?,?,?,?,?,?,?,?,'owned',?,?)",
                (*values, ref, now))
        await c.execute("UPDATE nft_wins SET status='sent', owned_gift_id=?, error=NULL, sent_at=? WHERE id=?",
                        (ref, now, win_id))
        await c.execute("UPDATE nft_models SET stock=MAX(stock-1,0), reserved=MAX(reserved-1,0) WHERE id=?",
                        (model["id"],))
    try:
        await bot.send_message(
            win["user_id"],
            f"🎉 Вы выиграли NFT {model.get('emoji') or '💎'} <b>{html.escape(item['collection_name'])} "
            f"#{item['number']}</b>, модель «{html.escape(model['model'])}»!\nОн уже в профиле → «Мои подарки»: "
            f"его можно вывести в Telegram, продать казино или поставить.",
        )
    except Exception:
        pass
    return True, f"{item['collection_name']} #{item['number']} — в профиле игрока"


async def deliver_waiting(bot: Bot, db: Database, cfg: Config, relayer: Relayer, user_id: int) -> None:
    """Игрок написал релейеру — отдаём ему всё, что ждало контакта."""
    for win in await db.all("SELECT id FROM nft_wins WHERE user_id=? AND status='waiting'", user_id):
        await deliver(bot, db, cfg, relayer, win["id"])
