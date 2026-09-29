"""Апгрейд NFT: ставка — только свои NFT, цель — модель казино подороже."""
import time

import pytest

from app import gifts, nft
from app.casino import Casino, GameError
from app.config import Config
from app.db import Database
from app.games import logic as g
from tests.test_gifts import FakeRelayer, sent


@pytest.fixture
async def env(tmp_path):
    cfg = Config(bot_token="1:x", admin_ids=frozenset({7}), webapp_url="", db_path=str(tmp_path / "u.db"),
                 port=0, min_bet=1, max_bet=1000, start_bonus=0, log_level="INFO")
    db = Database(cfg.db_path)
    await db.connect()
    for uid in (42, 43):
        await db.touch_user(uid, f"u{uid}", f"U{uid}")
    await db.kv_set(gifts.SINCE_KEY, str(time.time() - 60))
    relayer = FakeRelayer([sent(1, 42), sent(2, 42, number=2, model="Toad")], {"Frog": 1000, "Toad": 500})
    await gifts.scan(db, cfg, relayer)
    await db.conn.execute(
        "INSERT INTO nft_models(collection_id, collection_name, model, emoji, stock, price, price_at) VALUES "
        "('9','Durov''s Cap','Black','🧢',1,6000,?), ('9','Durov''s Cap','Old','🧢',1,6000,?)",
        (time.time(), time.time() - 7200))
    yield db, Casino(db, cfg)
    await db.close()


def test_upgrade_chance_math():
    assert g.upgrade_chance(1000, 6000) == pytest.approx(0.15)
    assert g.upgrade_chance(5900, 6000) == g.UPGRADE_MAX_CHANCE          # потолок шанса
    assert g.upgrade_chance(0, 6000) == 0


async def test_upgrade_targets_only_fresh_with_stock(env):
    db, casino = env
    assert [t["model"] for t in await casino.upgrade_targets()] == ["Black"]


async def test_upgrade_win(env, monkeypatch):
    db, casino = env
    ids = [x["id"] for x in await casino.gifts(42)]
    monkeypatch.setattr(g, "upgrade_roll", lambda rng=None: 0.0)
    r = await casino.upgrade(42, ids, 1)
    assert r["won"] and r["stake"] == 1500 and r["chance"] == pytest.approx(0.9 * 1500 / 6000)
    assert r["nft"]["model"] == "Black" and r["nft"]["win_id"] == 1
    assert (await db.one("SELECT stock, reserved FROM nft_models WHERE id=1")) == {"stock": 1, "reserved": 1}
    assert await casino.gifts(42) == []                                   # поставленные NFT ушли казино
    assert {r["status"] for r in await db.all("SELECT status FROM user_gifts")} == {"lost"}
    stock = await nft.casino_stock(db, [sent(1, 42), sent(2, 42)])
    assert len(stock) == 2                                                # и стали запасом для кейсов


async def test_upgrade_lose(env, monkeypatch):
    db, casino = env
    gift = (await casino.gifts(42))[0]
    monkeypatch.setattr(g, "upgrade_roll", lambda rng=None: 0.99)
    r = await casino.upgrade(42, [gift["id"]], 1)
    assert not r["won"] and r["nft"] is None
    assert (await db.one("SELECT reserved FROM nft_models WHERE id=1"))["reserved"] == 0
    assert len(await casino.gifts(42)) == 1


async def test_upgrade_rejects(env):
    db, casino = env
    ids = [x["id"] for x in await casino.gifts(42)]
    for gifts_, target, msg in (([], 1, "NFT"), (ids, 2, "нет"), (ids, 99, "нет"), (ids, "1", "цель")):
        with pytest.raises(GameError, match=msg):
            await casino.upgrade(42, gifts_, target)
    with pytest.raises(GameError):
        await casino.upgrade(43, ids, 1)                                  # чужие NFT
    await db.conn.execute("UPDATE nft_models SET price=1200 WHERE id=1")
    with pytest.raises(GameError, match="дороже"):
        await casino.upgrade(42, ids, 1)
    assert all(x["status"] == "owned" for x in await casino.gifts(42))    # всё откатилось


async def test_demo_nfts_visible_to_all_won_into_profile(env, monkeypatch):
    db, casino = env
    admin = 7
    await db.touch_user(admin, "adm", "Admin")
    models = [{"title": t, "model": m, "emoji": "🎁", "rarity": 1.0, "price": p} for t, m, p in (
        ("Lol Pop", "Pink", 300), ("Snoop Dogg", "Gold", 800), ("Plush Pepe", "Frog", 9000))]
    with pytest.raises(GameError):
        await casino.demo_add(42, models)
    mine, all_ = await casino.demo_add(admin, models, gifts_for_admin=2)
    assert [m["price"] for m in mine] == [300, 800]
    # демо-цели видит каждый игрок
    demo = [t for t in await casino.upgrade_targets(42) if t["demo"]]
    assert {t["model"] for t in demo} == {"Pink", "Gold", "Frog"}
    # игрок апгрейдит свои настоящие NFT в демо-цель → выигрыш звёздами по флору
    monkeypatch.setattr(g, "upgrade_roll", lambda rng=None: 0.0)
    ids = [x["id"] for x in await casino.gifts(42)]
    frog = next(t for t in demo if t["model"] == "Frog")
    before = (await db.get_user(42))["balance"]
    r = await casino.upgrade(42, ids, frog["id"])
    assert r["won"] and r["nft"]["demo"] and r["nft"]["win_id"] is None
    won = [x for x in await casino.gifts(42) if x["id"] == r["nft"]["gift_id"]][0]   # демо-NFT — в профиль
    assert won["demo"] and won["model"] == "Frog" and won["sell"] == 9000
    assert (await casino.gift_sell(42, won["id"]))["balance"] == before + 9000
    assert (await db.one("SELECT COUNT(*) n FROM nft_wins"))["n"] == 0            # передавать нечего
    # дюп админа нельзя вывести, но можно продать
    dupe = (await casino.gifts(admin))[0]
    assert dupe["demo"]
    with pytest.raises(GameError, match="Демо"):
        await casino.gift_withdraw_claim(admin, dupe["id"])
    assert (await casino.gift_sell(admin, dupe["id"]))["amount"] == dupe["value"]
    # синхронизация релейера не обнуляет демо-модели
    await nft.sync(db, FakeRelayer([]))
    assert (await db.one("SELECT MIN(stock) s FROM nft_models WHERE test=1"))["s"] == 999
    assert await casino.demo_clear() == 3


async def test_demo_nft_from_case_goes_to_profile(env):
    db, casino = env
    await db.conn.execute("UPDATE users SET balance=1000 WHERE id=42")
    case = {"id": "nft_x", "price": 100, "prizes": [
        {"kind": "nft", "model_id": 99, "emoji": "🐸", "title": "Plush Pepe", "model": "Frog", "amount": 5000,
         "weight": 1, "demo": True}]}
    r = await casino.open_case(42, case)
    assert r["kind"] == "nft" and r["nft"]["demo"] and r["nft"]["win_id"] is None
    assert r["balance"] == 1000 - 100                                   # не звёздами, а подарком в профиль
    gift = (await casino.gifts(42))[0]
    assert gift["id"] == r["nft"]["gift_id"] and gift["demo"] and gift["value"] == 5000 and gift["sell"] == 5000
