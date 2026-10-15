"""Каталог цен подарков с Getgems: флоры коллекций → каталог (в звёздах), названия — как в Telegram."""
import base64

import pytest

from app import getgems as gg
from app.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "g.db"))
    await d.connect()
    yield d
    await d.close()


def test_raw_address_and_floor():
    raw = bytes([0x11, 0x00]) + bytes(range(32)) + b"\x00\x00"
    friendly = base64.urlsafe_b64encode(raw).decode()
    assert gg.raw_address(friendly) == "0:" + bytes(range(32)).hex()
    assert gg.raw_address("0:abc") == "0:abc"
    assert gg.floor_ton({"floorPriceNano": "2500000000"}) == 2.5
    assert gg.floor_ton({"floorPrice": 3.2}) == 3.2
    assert gg.floor_ton({"floorPrice": 4_000_000_000}) == 4.0           # пришло в нанотонах
    assert gg.floor_ton({"floorPrice": 0}) is None and gg.floor_ton({}) is None


async def test_catalog_from_getgems(db, monkeypatch):
    calls = []

    async def fetch(self, path, **params):
        calls.append(path)
        if path == "gifts/collections":
            return {"items": [{"address": "EQ1", "name": "Plush Pepes"}, {"address": "EQ2", "name": "Lol Pops"},
                              {"address": "EQ3", "name": "Nobody Sells"}], "cursor": None}
        if path == "gifts/collections/top":
            return {"items": [{"collection": {"address": "EQ1", "name": "Plush Pepes"}, "floorPrice": 3000}]}
        if path.startswith("collection/stats/"):
            return {"floorPriceNano": "5000000000"} if path.endswith("EQ2") else {}
        raise AssertionError(path)

    class Market:
        async def ton_rate(self):
            return 200.0

    class Relayer:
        ready = True
        market = Market()

        async def collections(self):
            return ["Plush Pepe", "Lol Pop"]

    monkeypatch.setattr(gg.Getgems, "fetch", fetch)
    gems = gg.Getgems(db, key="k")
    st = await gg.catalog_from_getgems(db, Relayer(), gems)
    assert (st["collections"], st["priced"]) == (3, 2)
    rows = {r["collection_name"]: r["price"] for r in await db.all("SELECT collection_name, price FROM nft_models")}
    assert rows == {"Plush Pepe": 600_000, "Lol Pop": 1000}                # названия как в Telegram, TON × курс
    assert "collection/stats/EQ3" in calls and "collection/stats/EQ1" not in calls   # у кого флор из топа — без лишних запросов
