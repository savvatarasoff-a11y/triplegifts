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


async def test_admin_test_nfts_are_sandboxed(env, monkeypatch):
    db, casino = env
    admin = 7
    await db.touch_user(admin, "adm", "Admin")
    models = [{"title": t, "model": m, "emoji": "🎁", "rarity": 1.0, "price": p} for t, m, p in (
        ("Lol Pop", "Pink", 150), ("Desk Calendar", "Blue", 400), ("Homemade Cake", "Choco", 1200),
        ("Plush Pepe", "Frog", 3000), ("Durov's Cap", "Black", 12000), ("Snoop Dogg", "Gold", 500))]
    with pytest.raises(GameError):
        await casino.test_nfts_add(42, models)                            # не админ
    gifts_, targets_ = await casino.test_nfts_add(admin, models)
    assert [m["price"] for m in gifts_] == [150, 400, 500] and [m["price"] for m in targets_] == [1200, 3000, 12000]
    mine = await casino.gifts(admin)
    assert len(mine) == 3 and all(x["test"] and x["priced"] and x["sell"] is None for x in mine)
    # тестовые цели видит только админ, в кейсы и 777 они не попадают
    assert not any(t["test"] for t in await casino.upgrade_targets(42))
    targets = [t for t in await casino.upgrade_targets(admin) if t["test"]]
    assert [t["price"] for t in targets] == [1200, 3000, 12000]
    assert {x["title"].split(" #")[0] for x in mine} == {"Lol Pop", "Desk Calendar", "Snoop Dogg"}   # настоящие имена
    with pytest.raises(GameError, match="продать"):
        await casino.gift_sell(admin, mine[0]["id"])
    with pytest.raises(GameError, match="вывести"):
        await casino.gift_withdraw_claim(admin, mine[0]["id"])
    with pytest.raises(GameError, match="PvP"):
        await casino.pvp_bet(admin, None, "roulette", [mine[0]["id"]])
    real = next(t for t in await casino.upgrade_targets(admin) if not t["test"])
    with pytest.raises(GameError, match="тестовые цели"):
        await casino.upgrade(admin, [mine[0]["id"]], real["id"])          # заглушкой — в настоящую цель нельзя
    # выигрыш тестового апгрейда — новая заглушка, без передачи релейером и без записи в статистику
    monkeypatch.setattr(g, "upgrade_roll", lambda rng=None: 0.0)
    cheap = min(mine, key=lambda x: x["value"])                          # 150 → 3000 (Plush Pepe)
    r = await casino.upgrade(admin, [cheap["id"]], targets[1]["id"])
    assert r["won"] and r["nft"]["test"] and r["nft"]["win_id"] is None
    assert (await db.one("SELECT COUNT(*) n FROM nft_wins"))["n"] == 0
    assert (await db.one("SELECT COUNT(*) n FROM bets WHERE game='upgrade'"))["n"] == 0
    after = await casino.gifts(admin)
    assert len(after) == 3 and any(x["value"] == 3000 for x in after)
    # настоящая синхронизация релейера не трогает тестовые модели
    await nft.sync(db, FakeRelayer([]))
    assert (await db.one("SELECT stock FROM nft_models WHERE test=1 LIMIT 1"))["stock"] == 999
    assert await casino.test_nfts_clear() == 3
    assert await casino.gifts(admin) == [] and not any(t["test"] for t in await casino.upgrade_targets(admin))
