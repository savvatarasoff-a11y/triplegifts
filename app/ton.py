"""Пополнение TON: игрок переводит TON на кошелёк казино с комментарием-кодом, бот находит перевод.

Кошелёк казино задаёт админ (/tonwallet). Входящие переводы читаются через публичный API
toncenter.com (ключ не обязателен, с ключом TONCENTER_KEY лимиты выше). Каждая транзакция
зачисляется один раз — по её хэшу.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any
from urllib.parse import quote

import aiohttp
from aiogram import Bot

from . import money
from .db import REFERRAL_RATE, Database

log = logging.getLogger(__name__)

API = "https://toncenter.com/api/v2/getTransactions"
WALLET_KEY = "ton:wallet"
SINCE_KEY = "ton:since"          # переводы до подключения кошелька не зачисляются
COMMENT_RE = re.compile(r"^\s*SG\s*(\d{1,15})\s*$", re.IGNORECASE)


def deposit_comment(user_id: int) -> str:
    return f"SG{user_id}"


def transfer_link(wallet: str, comment: str, amount: int | None = None) -> str:
    """ton://transfer — открывает Tonkeeper/Telegram Wallet с уже заполненным переводом."""
    link = f"ton://transfer/{wallet}?text={quote(comment)}"
    return f"{link}&amount={amount}" if amount else link


async def wallet(db: Database) -> str | None:
    return await db.kv_get(WALLET_KEY)


async def set_wallet(db: Database, address: str | None) -> None:
    await db.kv_set(WALLET_KEY, address)
    await db.kv_set(SINCE_KEY, str(time.time()) if address else None)


async def fetch(address: str, limit: int = 50) -> list[dict[str, Any]]:
    params = {"address": address, "limit": str(limit), "archival": "false"}
    headers = {"Accept": "application/json"}
    key = os.environ.get("TONCENTER_KEY")
    if key:
        headers["X-API-Key"] = key
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(API, params=params, headers=headers) as r:
            data = await r.json(content_type=None)
    if not data.get("ok"):
        raise RuntimeError(f"toncenter: {data.get('error') or data.get('code')}")
    return list(data.get("result") or [])


def parse(tx: dict[str, Any]) -> tuple[str, int, int, float] | None:
    """(хэш, id игрока, сумма в nanoTON, время) для входящего перевода с кодом казино."""
    msg = tx.get("in_msg") or {}
    if not msg.get("source"):          # внешнее сообщение — это не перевод от игрока
        return None
    match = COMMENT_RE.match(msg.get("message") or "")
    value = int(msg.get("value") or 0)
    if not match or value <= 0:
        return None
    tx_hash = (tx.get("transaction_id") or {}).get("hash")
    if not tx_hash:
        return None
    return tx_hash, int(match.group(1)), value, float(tx.get("utime") or 0)


async def scan(db: Database, fetcher: Any = fetch) -> list[dict[str, Any]]:
    """Зачисляет новые переводы. Возвращает зачисленные (для уведомлений игрокам)."""
    address = await wallet(db)
    if not address:
        return []
    since = float(await db.kv_get(SINCE_KEY) or 0)
    credited = []
    for tx in await fetcher(address):
        parsed = parse(tx)
        if not parsed:
            continue
        tx_hash, user_id, amount, utime = parsed
        if utime < since:
            continue
        if await db.credit_ton(tx_hash, user_id, amount):
            credited.append({"user_id": user_id, "amount": amount, "hash": tx_hash})
    return credited


async def notify(bot: Bot, db: Database, credited: list[dict[str, Any]]) -> None:
    for item in credited:
        user = await db.get_user(item["user_id"])
        try:
            await bot.send_message(item["user_id"], f"💎 Зачислено <b>{money.fmt(item['amount'], money.TON)}</b>\n"
                                                    f"Баланс TON: {money.fmt(user['ton'], money.TON)}")
        except Exception:
            pass
        if user and user.get("referrer_id") and int(item["amount"] * REFERRAL_RATE) > 0:
            try:
                await bot.send_message(user["referrer_id"], "🤝 Ваш реферал пополнил баланс TON — вам "
                                       f"<b>+{money.fmt(int(item['amount'] * 0.10), money.TON)}</b>")
            except Exception:
                pass
