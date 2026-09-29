"""Картинки NFT через наш сервер: ужатие, кэш, защита от лишних скачиваний."""
import asyncio
import io
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from app import nftimg
from app.casino import Casino
from app.config import Config
from app.db import Database
from app.nftimg import NftImages
from app.web import build_app
from tests.test_web import TOKEN, FakeBot, auth


def png(size=512) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), (139, 92, 246, 255)).save(buf, "PNG")
    return buf.getvalue()


def test_source_urls():
    assert nftimg.source_url("Plush Pepe", "Frog Prince") == \
        "https://cdn.changes.tg/gifts/models/Plush%20Pepe/png/Frog%20Prince.png"
    assert nftimg.source_url("Durov's Cap", number=15) == "https://nft.fragment.com/gift/durovscap-15.webp"
    assert nftimg.source_url("") is None and nftimg.source_url("X") is None


def test_shrink_to_small_webp():
    data, ctype = nftimg.shrink(png())
    assert ctype == "image/webp"
    with Image.open(io.BytesIO(data)) as im:
        assert max(im.size) == nftimg.SIZE
    assert nftimg.shrink(b"not an image") == (b"not an image", "image/jpeg")


async def test_cache_dedupe_disk_and_failures(tmp_path):
    calls = []

    async def fetch(url):
        calls.append(url)
        await asyncio.sleep(0.01)
        return None if "bad" in url else png()

    imgs = NftImages(tmp_path, fetch)
    a, b = await asyncio.gather(imgs.get("u1"), imgs.get("u1"))      # одновременные запросы — одно скачивание
    assert a == b and a[1] == "image/webp" and calls == ["u1"]
    assert await imgs.get("u1") == a and calls == ["u1"]             # из памяти
    fresh = NftImages(tmp_path, fetch)                               # после перезапуска — с диска
    assert (await fresh.get("u1"))[0] == a[0] and calls == ["u1"]
    assert await imgs.get("bad") is None and await imgs.get("bad") is None
    assert calls.count("bad") == 1                                   # неудача запоминается
    imgs.failed["bad"] = time.time() - nftimg.FAIL_TTL - 1
    await imgs.get("bad")
    assert calls.count("bad") == 2
    assert await imgs.prewarm(["u2", "u2", "bad"]) == 1


@pytest.fixture
async def web_env(tmp_path):
    cfg = Config(bot_token=TOKEN, admin_ids=frozenset({7}), webapp_url="", db_path=str(tmp_path / "i.db"),
                 port=0, min_bet=1, max_bet=1000, start_bonus=0, log_level="INFO")
    db = Database(cfg.db_path)
    await db.connect()
    await db.conn.execute(
        "INSERT INTO nft_models(collection_id, collection_name, model, emoji, stock, price, price_at) "
        "VALUES ('1','Plush Pepe','Frog',  '🐸',1,5000,?)", (time.time(),))
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return png()

    app = build_app(cfg, Casino(db, cfg), FakeBot(), None, NftImages(tmp_path / "img", fetch))
    async with TestClient(TestServer(app)) as c:
        yield c, db, fetched
    await db.close()


async def test_nftimg_route(web_env):
    c, db, fetched = web_env
    r = await c.get("/nftimg", params={"c": "Plush Pepe", "m": "Frog"})
    assert r.status == 200 and r.content_type == "image/webp"
    assert "immutable" in r.headers["Cache-Control"]
    assert len(await r.read()) < 20_000
    assert (await c.get("/nftimg", params={"c": "Plush Pepe", "n": "77"})).status == 200
    # чужие коллекции и мусор не качаем
    for params in ({"c": "Unknown", "m": "X"}, {"c": "Plush Pepe"}, {"c": "Plush Pepe", "n": "1x"}, {}):
        assert (await c.get("/nftimg", params=params)).status == 404
    assert len(fetched) == 2


async def test_case_drops_feed(web_env):
    c, db, _ = web_env
    await db.touch_user(5, "u5", "Вася")
    await db.conn.execute("UPDATE users SET balance=1000 WHERE id=5")
    casino = Casino(db, Config(bot_token=TOKEN, admin_ids=frozenset(), webapp_url="", db_path="", port=0,
                               min_bet=1, max_bet=1000, start_bonus=0, log_level="INFO"))
    case = {"id": "nft_x", "price": 100, "prizes": [
        {"kind": "nft", "model_id": 1, "emoji": "🐸", "title": "Plush Pepe", "model": "Frog", "amount": 5000,
         "weight": 1, "demo": True}]}
    await casino.open_case(5, case)
    r = await c.get("/api/case/drops", headers=auth(5))
    drops = (await r.json())["drops"]
    assert drops[0]["nft"] == {"title": "Plush Pepe", "model": "Frog", "demo": True}
    assert drops[0]["good"] and drops[0]["name"] and drops[0]["prize"] == 5000
