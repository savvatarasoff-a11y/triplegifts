"""Цены подарков Telegram с Getgems (публичное API): флор каждой коллекции подарков.

Ключ API — из переменной GETGEMS_API_KEY или командой админа /getgems <ключ> (хранится в базе).
Лимит Getgems — 400 запросов за 5 минут с IP, поэтому запросы идут не чаще раза в 0.8 с.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Any

import aiohttp

from .db import Database

log = logging.getLogger(__name__)

BASE = "https://api.getgems.io/public-api/v1/"
KEY_KV = "getgems:key"
STATUS_KV = "getgems:status"
PAUSE = 0.8


class GetgemsError(Exception):
    pass


def raw_address(addr: str) -> str:
    """Адрес TON в «сыром» виде 0:hex (так его ждут пути Getgems). Сырой адрес возвращается как есть."""
    if ":" in addr:
        return addr
    data = base64.urlsafe_b64decode(addr + "=" * (-len(addr) % 4))
    if len(data) != 36:
        return addr
    wc = data[1] - 256 if data[1] > 127 else data[1]
    return f"{wc}:{data[2:34].hex()}"


def floor_ton(item: dict) -> float | None:
    """Флор в TON из ответа Getgems: поле в нанотонах или в TON (разные эндпоинты отдают по-разному)."""
    for key in ("floorPriceNano", "floorPrice"):
        v = item.get(key)
        if v in (None, "", 0, "0"):
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        return v / 1e9 if key == "floorPriceNano" or v >= 1e6 else v
    return None


class Getgems:
    def __init__(self, db: Database, key: str | None = None):
        self.db = db
        self.env_key = key if key is not None else os.environ.get("GETGEMS_API_KEY", "")
        self._session: aiohttp.ClientSession | None = None
        self._last = 0.0

    async def key(self) -> str:
        return (await self.db.kv_get(KEY_KV)) or self.env_key or ""

    async def fetch(self, path: str, **params: Any) -> Any:
        key = await self.key()
        if not key:
            raise GetgemsError("Нет ключа Getgems — /getgems <ключ>")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        query = {k: v for k, v in params.items() if v is not None}
        for attempt in range(4):
            wait = self._last + PAUSE - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
            async with self._session.get(BASE + path, params=query, headers={
                    "accept": "application/json", "Authorization": key}) as r:
                if r.status in (429, 500, 502, 503):
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                if r.status in (401, 403):
                    raise GetgemsError("Getgems не принял ключ — проверьте /getgems")
                if r.status >= 400:
                    raise GetgemsError(f"Getgems ответил {r.status}")
                body = await r.json(content_type=None)
            if isinstance(body, dict) and body.get("success") is False:
                raise GetgemsError(str(body.get("error") or body)[:200])
            return body.get("response", body) if isinstance(body, dict) else body
        raise GetgemsError("Getgems перегружен — попробуем позже")

    async def paged(self, path: str, pages: int = 20, **params: Any) -> list[dict]:
        items, after = [], None
        for _ in range(pages):
            res = await self.fetch(path, limit=100, after=after, **params)
            batch = res.get("items", []) if isinstance(res, dict) else (res or [])
            items += batch
            after = res.get("cursor") if isinstance(res, dict) else None
            if not after or not batch:
                break
        return items

    async def gift_floors(self) -> list[dict]:
        """Все коллекции подарков с флором: [{"name", "address", "floor_ton"}]. Флор — из списка топа,
        у кого его нет — из статистики коллекции."""
        found: dict[str, dict] = {}
        for item in await self.paged("gifts/collections"):
            coll = item.get("collection") or item
            if coll.get("address"):
                found[coll["address"]] = {"name": coll.get("name") or "", "address": coll["address"],
                                          "floor_ton": floor_ton(item) or floor_ton(coll)}
        try:
            for item in await self.paged("gifts/collections/top", pages=5, kind="all"):
                coll = item.get("collection") or item
                addr = coll.get("address")
                if not addr:
                    continue
                row = found.setdefault(addr, {"name": coll.get("name") or "", "address": addr, "floor_ton": None})
                row["floor_ton"] = floor_ton(item) or floor_ton(coll) or row["floor_ton"]
        except GetgemsError as e:
            log.info("Топ коллекций подарков Getgems недоступен: %s", e)
        for row in found.values():
            if row["floor_ton"]:
                continue
            try:
                row["floor_ton"] = floor_ton(await self.fetch(f"collection/stats/{raw_address(row['address'])}") or {})
            except GetgemsError as e:
                log.info("Getgems: нет статистики %s: %s", row["name"], e)
        return [r for r in found.values() if r["name"]]

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


GEMS_MODEL = "Флор"   # каталог по Getgems — одна цена на коллекцию; модель до улучшения не важна


def match_title(name: str, titles: dict[str, str]) -> str:
    """Название коллекции Getgems → название подарка в Telegram (у Getgems бывает во множественном числе)."""
    from .nftimg import norm
    key = norm(name)
    for cand in (key, key[:-1] if key.endswith("s") else None, key[:-2] if key.endswith("es") else None):
        if cand and cand in titles:
            return titles[cand]
    return name


async def catalog_from_getgems(db: Database, relayer: Any, gems: Getgems) -> dict:
    """Обновляет каталог флорами коллекций с Getgems (TON → звёзды по курсу). Возвращает итог для лога."""
    import json
    from .mrkt import emoji_for
    from .nft import catalog_keep_fresh, catalog_store
    from .nftimg import norm
    rate = await relayer.market.ton_rate()
    if not rate:
        raise GetgemsError("Нет курса TON → звёзды (/tonrate)")
    titles: dict[str, str] = {}
    if getattr(relayer, "ready", False):
        try:
            titles = {norm(t): t for t in await relayer.collections()}
        except Exception:
            titles = {}
    rows = await gems.gift_floors()
    priced = 0
    for r in rows:
        if not r["floor_ton"]:
            continue
        name = match_title(r["name"], titles)
        await catalog_store(db, name, emoji_for(name),
                            [{"model": GEMS_MODEL, "rarity": None, "price": max(1, int(r["floor_ton"] * rate))}])
        priced += 1
    await catalog_keep_fresh(db)
    status = {"at": time.time(), "collections": len(rows), "priced": priced, "rate": rate}
    await db.kv_set(STATUS_KV, json.dumps(status))
    return status
