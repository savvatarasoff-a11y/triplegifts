"""Цены NFT с маркетплейса MRKT (@mrkt, tgmrkt.io).

Флор модели — самый дешёвый лот этой коллекции и модели на MRKT. Цены на MRKT в TON,
казино считает в звёздах: курс звёзд за 1 TON задаёт админ (/tonrate) или он считается
автоматически по цене TON в долларах и цене звезды STAR_USD.

Вход на MRKT — через аккаунт-релейер: он открывает мини-приложение @mrkt (как игрок в Telegram)
и обменивает его initData на токен API.
"""
from __future__ import annotations

import logging
import random
import re
import time
from typing import Any
from urllib.parse import unquote

import aiohttp

log = logging.getLogger(__name__)

API = "https://api.tgmrkt.io/api/v1"
NANO = 1_000_000_000
PRICE_TTL = 15 * 60
TOKEN_TTL = 12 * 3600
RATE_TTL = 5 * 60
STAR_USD = 0.015          # во сколько долларов обходится звезда — для курса TON → звёзды по цене TON в $
PRICE_CHANNEL = "tonprices"   # канал с актуальной ценой TON в долларах (обновляется раз в 5 минут)
USD_RE = (re.compile(r"\$\s*(\d+(?:[.,]\d+)?)"), re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:\$|usd|usdt)", re.I))


def parse_usd(text: str) -> float | None:
    """Цена в долларах из текста вида «TON $5.43», «5,43$», «1 TON = 5.43 USD»."""
    for rx in USD_RE:
        for m in rx.finditer(text or ""):
            value = float(m.group(1).replace(",", "."))
            if 0.01 <= value <= 10_000:
                return value
    return None


TON_USD_URL = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=usd"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://cdn.tgmrkt.io",
    "Referer": "https://cdn.tgmrkt.io/",
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/141.0.0.0 Mobile Safari/537.36",
}

# У MRKT нет эмодзи моделей — показываем узнаваемый значок коллекции
EMOJI = {"pepe": "🐸", "frog": "🐸", "cap": "🧢", "hat": "🎩", "cake": "🎂", "pop": "🍭", "candy": "🍬",
         "heart": "❤️", "rose": "🌹", "ring": "💍", "diamond": "💎", "star": "⭐", "cat": "🐱", "dog": "🐶",
         "bear": "🧸", "egg": "🥚", "rocket": "🚀", "clover": "🍀", "bell": "🔔", "cookie": "🍪",
         "calendar": "📅", "lamp": "🪔", "skull": "💀", "ghost": "👻", "witch": "🧹", "pumpkin": "🎃",
         "snake": "🐍", "helmet": "⛑️", "sword": "⚔️", "bunny": "🐰", "duck": "🦆", "monkey": "🐵",
         "genie": "🧞", "berry": "🍓", "cigar": "🚬", "potion": "🧪", "crystal": "🔮", "ball": "🔮",
         "box": "🎁", "bag": "👜", "boots": "👢", "gem": "💎", "flower": "🌸", "dragon": "🐉", "ice": "🧊"}


def emoji_for(collection: str) -> str:
    low = collection.lower()
    return next((e for k, e in EMOJI.items() if k in low), "🎁")


class MrktError(Exception):
    pass


class Mrkt:
    def __init__(self, relayer: Any):
        self.relayer = relayer
        self._token: str | None = None
        self._token_at = 0.0
        self._prices: dict[tuple[str, str], tuple[float, float | None]] = {}
        self._rate: tuple[float, float] | None = None
        self.rate_source: str | None = None
        self._session: aiohttp.ClientSession | None = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    # ---------- вход ----------

    async def _init_data(self) -> str:
        """initData мини-приложения @mrkt от имени релейера (как будто он открыл маркет в Telegram)."""
        if not self.relayer.ready:
            raise MrktError("Релейер не подключён — без него MRKT не открыть (/relayer)")
        from telethon import utils
        from telethon.tl.functions.messages import RequestAppWebViewRequest
        from telethon.tl.types import InputBotAppShortName
        client = self.relayer.client
        peer = await client.get_input_entity("mrkt")
        res = await client(RequestAppWebViewRequest(
            peer=peer, app=InputBotAppShortName(bot_id=utils.get_input_user(peer), short_name="app"),
            platform="android",
        ))
        return unquote(res.url.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion", 1)[0])

    async def _auth(self) -> str:
        session = await self._http()
        async with session.post(f"{API}/auth", json={"data": await self._init_data()}, headers=HEADERS) as r:
            if r.status != 200:
                raise MrktError(f"MRKT не пустил: HTTP {r.status}")
            token = (await r.json()).get("token")
        if not token:
            raise MrktError("MRKT не выдал токен")
        self._token, self._token_at = token, time.time()
        return token

    async def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        if not self._token or time.time() - self._token_at > TOKEN_TTL:
            await self._auth()
        session = await self._http()
        for attempt in (1, 2):
            headers = {**HEADERS, "Authorization": self._token or ""}
            async with session.request(method, f"{API}{path}", json=body, headers=headers) as r:
                if r.status == 401 and attempt == 1:
                    await self._auth()
                    continue
                if r.status != 200:
                    raise MrktError(f"MRKT {path}: HTTP {r.status}")
                return await r.json(content_type=None)
        raise MrktError("MRKT: нет доступа")

    # ---------- данные маркета ----------

    async def saling(self, collection: str | None = None, model: str | None = None, count: int = 20,
                     cheap_first: bool = True) -> list[dict[str, Any]]:
        """Лоты на продаже (цены в nanoTON)."""
        data = await self._request("POST", "/gifts/saling", {
            "collectionNames": [collection] if collection else [], "modelNames": [model] if model else [],
            "backdropNames": [], "symbolNames": [], "ordering": "Price", "lowToHigh": cheap_first,
            "maxPrice": None, "minPrice": None, "mintable": None, "number": None, "count": count,
            "cursor": "", "query": None, "promotedFirst": False,
        })
        return list((data or {}).get("gifts") or [])

    async def collections(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/gifts/collections")
        return list(data or [])

    async def floor_ton(self, collection: str, model: str) -> float | None:
        """Флор модели в TON: самый дешёвый лот этой коллекции и модели на MRKT."""
        key = (collection, model)
        cached = self._prices.get(key)
        if cached and time.time() - cached[0] < PRICE_TTL:
            return cached[1]
        lots = await self.saling(collection, model, count=5)
        prices = [g["salePrice"] for g in lots if g.get("salePrice") and g.get("collectionName", collection) == collection
                  and g.get("modelName", model) == model]
        floor = min(prices) / NANO if prices else None
        self._prices[key] = (time.time(), floor)
        return floor

    # ---------- курс TON → звёзды ----------

    async def ton_usd_channel(self) -> tuple[float, str] | None:
        """Цена TON в долларах из канала @tonprices (название канала и последние посты) — через релейер."""
        if not self.relayer.ready:
            return None
        client = self.relayer.client
        entity = await client.get_entity(PRICE_CHANNEL)
        texts = [getattr(entity, "title", "") or ""]
        texts += [m.message or "" for m in await client.get_messages(entity, limit=5)]
        for text in texts:
            usd = parse_usd(text)
            if usd:
                return usd, text.strip()[:120]
        return None

    async def ton_rate(self) -> float | None:
        """Сколько звёзд стоит 1 TON: ручной курс админа или по цене TON из @tonprices (запасной — CoinGecko)."""
        manual = await self.relayer.db.kv_get("mrkt:ton_stars")
        if manual:
            return float(manual)
        if self._rate and time.time() - self._rate[0] < RATE_TTL:
            return self._rate[1]
        usd, source = None, None
        try:
            found = await self.ton_usd_channel()
            if found:
                usd, source = found[0], f"@{PRICE_CHANNEL}: {found[1]}"
        except Exception as e:
            log.warning("Канал @%s недоступен: %s", PRICE_CHANNEL, type(e).__name__)
        if usd is None:
            try:
                session = await self._http()
                async with session.get(TON_USD_URL, headers={"Accept": "application/json"}) as r:
                    usd = float((await r.json())["the-open-network"]["usd"])
                source = "CoinGecko"
            except Exception as e:
                log.warning("Курс TON недоступен: %s", type(e).__name__)
        if usd is None:
            last = await self.relayer.db.kv_get("mrkt:ton_stars_auto")
            return float(last) if last else None
        rate = usd / STAR_USD
        self._rate = (time.time(), rate)
        self.rate_source = f"{source} — ${usd:g}"
        await self.relayer.db.kv_set("mrkt:ton_stars_auto", str(rate))
        return rate

    async def floor_stars(self, collection: str, model: str) -> int | None:
        ton = await self.floor_ton(collection, model)
        if not ton:
            return None
        rate = await self.ton_rate()
        return int(ton * rate) if rate else None

    # ---------- настоящие модели для демо-NFT ----------

    async def sample_models(self, count: int, per_collection: int = 3) -> list[dict[str, Any]]:
        """Случайные настоящие модели с MRKT с их флором в звёздах (для демо-NFT)."""
        rate = await self.ton_rate()
        if not rate:
            raise MrktError("Нет курса TON → звёзды, задайте его: /tonrate 200")
        colls = [c for c in await self.collections() if c.get("floorPriceNanoTons") and not c.get("isHidden")]
        random.shuffle(colls)
        picked: list[dict[str, Any]] = []
        seen = set()
        for coll in colls:
            if len(picked) >= count:
                break
            name = coll.get("name") or coll.get("title")
            lots = await self.saling(name, count=30)
            random.shuffle(lots)
            taken = 0
            for lot in lots:
                if taken >= per_collection or len(picked) >= count:
                    break
                key = (lot.get("collectionName") or name, lot.get("modelName"))
                if not key[1] or key in seen or str(key[1]).isdigit():   # число вместо модели — не улучшен
                    continue
                floor = await self.floor_ton(*key)
                if not floor:
                    continue
                seen.add(key)
                taken += 1
                picked.append({"collection": key[0], "title": coll.get("title") or key[0], "model": key[1],
                               "rarity": (lot.get("modelRarityPerMille") or 0) / 10 or None,
                               "emoji": emoji_for(key[0]), "price": max(1, int(floor * rate))})
        return picked
