"""Картинки NFT через наш сервер: скачиваем один раз, ужимаем до маленького WebP и кэшируем.

Оригиналы с changes.tg — PNG по 150–400 КБ, в рулетке кейса их десятки, и в Telegram они грузятся
долго. Мы отдаём превью ~5–10 КБ с долгим Cache-Control, а популярные модели прогреваем заранее.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)

SIZE = 160                 # px — хватает для карточек на ретине
QUALITY = 80
MEM_MAX = 600              # превью в памяти
FAIL_TTL = 600             # не долбим источник, если картинки нет
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=15)
MAX_SOURCE = 5 * 1024 * 1024


def slug(collection: str) -> str:
    return re.sub(r"[^a-z0-9]", "", collection.lower())


def source_url(collection: str, model: str | None = None, number: int | None = None) -> str | None:
    if not collection:
        return None
    if number:
        return f"https://nft.fragment.com/gift/{slug(collection)}-{number}.webp"
    if model:
        return f"https://cdn.changes.tg/gifts/models/{quote(collection)}/png/{quote(model)}.png"
    return None


def shrink(data: bytes) -> tuple[bytes, str]:
    """Уменьшает картинку до SIZE px WebP. Без Pillow (или на битом файле) отдаёт оригинал."""
    try:
        from PIL import Image
    except ImportError:
        return data, _guess_type(data)
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if im.mode not in ("RGBA", "RGB"):
                im = im.convert("RGBA")
            im.thumbnail((SIZE, SIZE), Image.LANCZOS)
            out = io.BytesIO()
            im.save(out, "WEBP", quality=QUALITY, method=4)
            return out.getvalue(), "image/webp"
    except Exception:
        log.debug("Не удалось ужать картинку NFT", exc_info=True)
        return data, _guess_type(data)


def _guess_type(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class NftImages:
    def __init__(self, cache_dir: str | Path | None = None, fetch=None):
        self.dir = Path(cache_dir) if cache_dir else None
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
        self.mem: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self.failed: dict[str, float] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self._fetch = fetch or self._http_fetch
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def _http_fetch(self, url: str) -> bytes | None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        async with self._session.get(url) as r:
            if r.status != 200:
                return None
            data = await r.content.read(MAX_SOURCE + 1)
            return data if 0 < len(data) <= MAX_SOURCE else None

    def _path(self, key: str) -> Path | None:
        return self.dir / (hashlib.sha1(key.encode()).hexdigest() + ".img") if self.dir else None

    def _remember(self, key: str, value: tuple[bytes, str]) -> None:
        self.mem[key] = value
        self.mem.move_to_end(key)
        while len(self.mem) > MEM_MAX:
            self.mem.popitem(last=False)

    async def get(self, url: str) -> tuple[bytes, str] | None:
        """Превью картинки по адресу источника: (байты, content-type) или None."""
        hit = self.mem.get(url)
        if hit:
            self.mem.move_to_end(url)
            return hit
        path = self._path(url)
        if path and path.exists():
            data = path.read_bytes()
            value = (data, _guess_type(data))
            self._remember(url, value)
            return value
        if time.time() - self.failed.get(url, 0) < FAIL_TTL:
            return None
        if url in self.pending:                       # тот же файл уже качается — ждём его
            return await asyncio.shield(self.pending[url])
        fut = asyncio.get_running_loop().create_future()
        self.pending[url] = fut
        value = None
        try:
            raw = await self._fetch(url)
            if raw:
                value = await asyncio.to_thread(shrink, raw)
                self._remember(url, value)
                if path:
                    path.write_bytes(value[0])
            else:
                self.failed[url] = time.time()
        except Exception:
            log.debug("Картинка NFT недоступна: %s", url, exc_info=True)
            self.failed[url] = time.time()
        finally:
            self.pending.pop(url, None)
            fut.set_result(value)
        return value

    async def prewarm(self, urls: list[str], parallel: int = 6) -> int:
        """Заранее качает картинки (например, всех моделей из кейсов). Возвращает, сколько готово."""
        sem = asyncio.Semaphore(parallel)

        async def one(u: str) -> bool:
            async with sem:
                return await self.get(u) is not None

        return sum(await asyncio.gather(*(one(u) for u in dict.fromkeys(urls))))
