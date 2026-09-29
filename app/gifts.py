"""Подарки игроков: игрок отправляет подарок аккаунту-релейеру — он появляется в казино.

NFT зачисляется в «Мои подарки» по цене пола маркета модели: его можно поставить в PvP,
продать казино за звёзды или вывести обратно. Обычный подарок сразу зачисляется звёздами
по курсу обмена Telegram (сколько звёзд даёт его конвертация).
"""
from __future__ import annotations

import html
import logging
import time
from typing import Any

from aiogram import Bot

from .casino import Casino, GameError
from .config import Config
from .db import Database
from .nft import PRICE_MAX_AGE
from .relayer import Relayer, RelayerError

log = logging.getLogger(__name__)

SINCE_KEY = "gifts:since"     # подарки, полученные релейером до включения функции, не зачисляются


async def scan(db: Database, cfg: Config, relayer: Relayer) -> list[dict[str, Any]]:
    """Находит новые подарки от игроков и зачисляет их. Возвращает зачисленные (для уведомлений)."""
    if not relayer.ready:
        return []
    since = await db.kv_get(SINCE_KEY)
    if since is None:
        await db.kv_set(SINCE_KEY, str(time.time()))
        return []
    since_ts = float(since)
    received = await relayer.received()
    known = {r["ref"] for r in await db.all("SELECT ref FROM user_gifts")}
    now = time.time()
    credited = []
    for item in received:
        ref = str(item["ref"])
        sender = item.get("from_user")
        if ref in known or not sender or item["date"] < since_ts:
            continue
        if sender in cfg.admin_ids:
            # подарки от админов — пополнение запаса казино, а не депозит
            async with db.tx() as c:
                await c.execute(
                    "INSERT OR IGNORE INTO user_gifts(user_id, ref, kind, collection_id, collection_name, number, "
                    "model, status, from_user, created_at) VALUES (?,?,?,?,?,?,?,'stock',?,?)",
                    (sender, ref, item["kind"], item["collection_id"], item["collection_name"], item["number"],
                     item["model"], sender, now))
            continue
        value = None
        if item["kind"] == "nft":
            try:
                value = await relayer.floor_price(item["collection_id"], item["model"], item["collection_name"])
            except Exception as e:
                log.warning("Цена подаренной модели %s недоступна: %s", item["model"], e)
        else:
            value = item.get("convert_stars") or 0
        status = "owned" if item["kind"] == "nft" else "credited"
        async with db.tx() as c:
            await c.execute("INSERT OR IGNORE INTO users(id, created_at, last_seen) VALUES (?,?,?)",
                            (sender, now, now))
            cur = await c.execute(
                "INSERT OR IGNORE INTO user_gifts(user_id, ref, kind, collection_id, collection_name, number, model, "
                "emoji, rarity, value, priced_at, transfer_at, status, from_user, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sender, ref, item["kind"], item["collection_id"], item["collection_name"], item["number"],
                 item["model"], item["emoji"], item["rarity"], value, now if value else None,
                 item.get("transfer_at") or 0, status, sender, now),
            )
            if cur.rowcount != 1:
                continue
            if item["kind"] == "gift" and value:
                await db.change_balance(c, sender, value, "gift_deposit", ref)
        credited.append({**item, "user_id": sender, "value": value})
    await reprice(db, relayer)
    return credited


async def reprice(db: Database, relayer: Relayer) -> None:
    """Обновляет цены NFT игроков (ставить можно только подарок с проверенной за час ценой)."""
    now = time.time()
    rows = await db.all(
        "SELECT id, collection_id, collection_name, model FROM user_gifts WHERE kind='nft' AND status='owned' "
        "AND (priced_at IS NULL OR priced_at < ?)", now - PRICE_MAX_AGE / 2,
    )
    for r in rows:
        try:
            price = await relayer.floor_price(r["collection_id"], r["model"], r["collection_name"])
        except Exception:
            continue
        if price:
            async with db.tx() as c:
                await c.execute("UPDATE user_gifts SET value=?, priced_at=? WHERE id=?", (price, now, r["id"]))


def deposit_text(item: dict[str, Any]) -> str:
    emoji = item.get("emoji") or "🎁"
    if item["kind"] == "gift":
        return (f"{emoji} Подарок «{html.escape(item['collection_name'])}» получен — "
                f"на баланс зачислено <b>{item['value'] or 0} ⭐</b>.")
    title = html.escape(f"{item['collection_name']} #{item['number']}")
    price = f"оценён в <b>{item['value']} ⭐</b> (пол маркета модели)" if item["value"] else "цена уточняется"
    return (f"{emoji} NFT <b>{title}</b> («{html.escape(item['model'])}») получен и {price}.\n"
            "Он в разделе «Мои подарки»: его можно поставить в PvP, продать казино или вывести обратно.")


async def notify_deposits(bot: Bot, credited: list[dict[str, Any]]) -> None:
    for item in credited:
        try:
            await bot.send_message(item["user_id"], deposit_text(item))
        except Exception:
            pass


async def withdraw(casino: Casino, relayer: Relayer, user_id: int, gift_id: Any) -> dict:
    """Возвращает NFT игроку с аккаунта релейера."""
    if not relayer.ready:
        raise GameError("Вывод подарков временно недоступен")
    gift = await casino.gift_withdraw_claim(user_id, gift_id)
    ok = False
    try:
        item = next((i for i in await relayer.inventory() if str(i["ref"]) == gift["ref"]), None)
        if item is None:
            raise GameError("Telegram пока не даёт передать этот подарок — попробуйте позже")
        user = await casino.db.get_user(user_id)
        await relayer.transfer(item, user_id, (user or {}).get("username"))
        ok = True
    except RelayerError as e:
        if str(e) == "NEED_CONTACT":
            name = await relayer.username()
            raise GameError(f"Напишите любое сообщение @{name} и повторите вывод" if name else
                            "Напишите любое сообщение аккаунту казино и повторите вывод") from None
        log.warning("Вывод подарка %s не удался: %s", gift_id, e)
        raise GameError("Не удалось передать подарок, попробуйте позже") from None
    except GameError:
        raise
    except Exception as e:
        log.warning("Вывод подарка %s не удался: %s", gift_id, type(e).__name__)
        raise GameError("Не удалось передать подарок, попробуйте позже") from None
    finally:
        await casino.gift_withdraw_finish(gift["id"], ok)
    return {"ok": True, "title": Casino.gift_title(gift)}
