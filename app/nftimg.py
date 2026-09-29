"""Картинки NFT через наш сервер: берём один раз, ужимаем до маленького WebP и кэшируем.

Источники по очереди: стикер модели из самого Telegram (через релейер, отрисовываем TGS в картинку),
затем changes.tg / Fragment. Игроку отдаём превью ~5–10 КБ с долгим Cache-Control, а модели из кейсов
и апгрейда прогреваем заранее — поэтому в рулетке они появляются сразу.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import logging
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)

SIZE = 160                 # px — хватает для карточек на ретине
QUALITY = 80
MEM_MAX = 600              # превью в памяти
FAIL_TTL = 600             # не долбим источники, если картинки нет
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=15)
MAX_SOURCE = 5 * 1024 * 1024

Source = Callable[[], Awaitable[bytes | None]]


def slug(collection: str) -> str:
    return re.sub(r"[^a-z0-9]", "", collection.lower())


def norm(name: str) -> str:
    """Имя коллекции/модели для сравнения: «Durov's Cap» и «DurovsCap» совпадают."""
    return slug(name or "")


def model_url(collection: str, model: str) -> str:
    return f"https://cdn.changes.tg/gifts/models/{quote(collection)}/png/{quote(model)}.png"


def gift_url(collection: str, number: int) -> str:
    return f"https://nft.fragment.com/gift/{slug(collection)}-{number}.webp"


def source_url(collection: str, model: str | None = None, number: int | None = None) -> str | None:
    if not collection:
        return None
    if number:
        return gift_url(collection, number)
    if model:
        return model_url(collection, model)
    return None


def render_tgs(data: bytes) -> bytes | None:
    """Первый кадр анимированного стикера (TGS = gzip Lottie) в PNG. Нужен rlottie-python."""
    try:
        from rlottie_python import LottieAnimation
    except ImportError:
        log.warning("rlottie-python не установлен — модели из Telegram не отрисовать")
        return None
    try:
        raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
        anim = LottieAnimation.from_data(raw.decode("utf-8"))
        img = anim.render_pillow_frame(frame_num=0, width=SIZE * 2, height=SIZE * 2)
        out = io.BytesIO()
        img.save(out, "PNG")
        return out.getvalue()
    except Exception:
        log.debug("Не удалось отрисовать TGS", exc_info=True)
        return None


async def render_sticker(client: Any, doc: Any) -> bytes | None:
    """Стикер-документ Telegram → картинка: TGS отрисовываем, WebP/PNG отдаём как есть, иначе — миниатюра."""
    mime = getattr(doc, "mime_type", "") or ""
    data = await client.download_media(doc, file=bytes)
    if data and ("tgsticker" in mime or data[:2] == b"\x1f\x8b"):
        png = await asyncio.to_thread(render_tgs, data)
        if png:
            return png
    elif data and mime.startswith("image/"):
        return data
    if getattr(doc, "thumbs", None):
        try:
            return await client.download_media(doc, file=bytes, thumb=-1) or None
        except Exception:
            log.debug("Нет миниатюры стикера", exc_info=True)
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
    def __init__(self, cache_dir: str | Path | None = None, fetch=None, relayer: Any = None):
        self.dir = Path(cache_dir) if cache_dir else None
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
        self.relayer = relayer
        self.mem: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self.failed: dict[str, float] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self._fetch = fetch or self._http_fetch
        self._session: aiohttp.ClientSession | None = None
        self.stats = {"telegram": 0, "web": 0, "failed": 0}

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

    def _sources(self, collection: str, model: str | None, number: int | None) -> list[tuple[str, Source]]:
        sources: list[tuple[str, Source]] = []
        if number:
            sources.append(("web", lambda: self._fetch(gift_url(collection, number))))
        if self.relayer is not None and getattr(self.relayer, "ready", False):
            sources.append(("telegram", lambda: self.relayer.model_image(collection, model)))
        if model:
            sources.append(("web", lambda: self._fetch(model_url(collection, model))))
        return sources

    async def nft(self, collection: str, model: str | None = None, number: int | None = None) -> tuple[bytes, str] | None:
        """Превью NFT: конкретный номер (Fragment) или модель (Telegram, затем changes.tg)."""
        key = f"n|{collection}|{number}" if number else f"m|{collection}|{model}"
        return await self.get(key, self._sources(collection, model, number))

    async def get(self, key: str, sources: list[tuple[str, Source]] | None = None) -> tuple[bytes, str] | None:
        """Превью по ключу: (байты, content-type) или None. Без sources ключ — это адрес картинки."""
        hit = self.mem.get(key)
        if hit:
            self.mem.move_to_end(key)
            return hit
        path = self._path(key)
        if path and path.exists():
            data = path.read_bytes()
            value = (data, _guess_type(data))
            self._remember(key, value)
            return value
        if time.time() - self.failed.get(key, 0) < FAIL_TTL:
            return None
        if key in self.pending:                       # то же уже качается — ждём его
            return await asyncio.shield(self.pending[key])
        if sources is None:
            sources = [("web", lambda: self._fetch(key))]
        fut = asyncio.get_running_loop().create_future()
        self.pending[key] = fut
        value = None
        try:
            for name, source in sources:
                try:
                    raw = await source()
                except Exception as e:
                    log.debug("Картинка %s: источник %s недоступен (%s)", key, name, type(e).__name__)
                    continue
                if raw:
                    value = await asyncio.to_thread(shrink, raw)
                    self.stats[name] = self.stats.get(name, 0) + 1
                    break
            if value:
                self._remember(key, value)
                if path:
                    path.write_bytes(value[0])
            else:
                self.stats["failed"] += 1
                self.failed[key] = time.time()
        finally:
            self.pending.pop(key, None)
            fut.set_result(value)
        return value

    async def prewarm(self, items: list[Any], parallel: int = 4) -> int:
        """Заранее готовит картинки. items — ключи-адреса или кортежи (коллекция, модель). Возвращает, сколько готово."""
        sem = asyncio.Semaphore(parallel)

        async def one(it: Any) -> bool:
            async with sem:
                if isinstance(it, tuple):
                    return await self.nft(it[0], it[1]) is not None
                return await self.get(it) is not None

        return sum(await asyncio.gather(*(one(it) for it in dict.fromkeys(items))))
