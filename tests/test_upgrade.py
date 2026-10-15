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
    assert r["nft"]["model"] == "Black" and r["nft"]["pending"]
    mine = await casino.gifts(42)                                         # поставленные NFT ушли казино,
    assert [(x["model"], x["status"]) for x in mine] == [("Black", "pending")]   # выигрыш ждёт админа
    stock = await nft.casino_stock(db, [sent(1, 42), sent(2, 42)])
    assert len(stock) == 2                                                # проигранные NFT — запас релейера
    # админ одобряет: подарка нужной модели у релейера нет → просим купить
    with pytest.raises(GameError, match="Купите"):
        await casino.upgrade_approve(r["nft"]["gift_id"], stock)
    cap = {**sent(5, 7, number=77, model="Black"), "collection_id": "9", "collection_name": "Durov's Cap"}
    done = await casino.upgrade_approve(r["nft"]["gift_id"], stock + [cap])
    assert (done["status"], done["test"], done["ref"], done["number"]) == ("owned", 0, "5", 77)
    with pytest.raises(GameError, match="обработана"):
        await casino.upgrade_approve(r["nft"]["gift_id"], [cap])
    # вывод — за 30 ★ с баланса
    # вывод выигранного и одобренного NFT (уровень вывода VIP — лимит 10 000 ★ в неделю)
    await db.conn.execute("UPDATE users SET balance=10, deposited=10000 WHERE id=42")
    await db.credit_payment("p42", 42, 100)
    claimed = await casino.gift_withdraw_claim(42, done["id"])
    assert claimed["ref"] == "5" and (await db.get_user(42))["balance"] == 110 - g.NFT_WITHDRAW_FEE
    await casino.gift_withdraw_finish(done["id"], False)                  # не передали — комиссия вернулась
    assert (await db.get_user(42))["balance"] == 110


async def test_upgrade_win_rejected_returns_stake(env, monkeypatch):
    db, casino = env
    ids = [x["id"] for x in await casino.gifts(42)]
    monkeypatch.setattr(g, "upgrade_roll", lambda rng=None: 0.0)
    r = await casino.upgrade(42, ids, 1)
    await casino.upgrade_reject(r["nft"]["gift_id"])
    assert sorted(x["id"] for x in await casino.gifts(42)) == ids and all(
        x["status"] == "owned" for x in await casino.gifts(42))


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
    assert r["won"] and r["nft"]["pending"] and r["nft"]["win_id"] is None
    won = [x for x in await casino.gifts(42) if x["id"] == r["nft"]["gift_id"]][0]   # выигрыш ждёт одобрения
    assert won["status"] == "pending" and won["model"] == "Frog"
    with pytest.raises(GameError):
        await casino.gift_sell(42, won["id"])                                    # пока ждёт — не продать
    assert (await db.one("SELECT COUNT(*) n FROM nft_wins"))["n"] == 0
    assert (await db.get_user(42))["balance"] == before
    # дюп админа (не улучшенный) нельзя вывести, но можно продать
    dupe = (await casino.gifts(admin))[0]
    assert dupe["demo"]
    with pytest.raises(GameError, match="апгрейда"):
        await casino.gift_withdraw_claim(admin, dupe["id"])
    assert (await casino.gift_sell(admin, dupe["id"]))["amount"] == dupe["value"]
    # синхронизация релейера не обнуляет демо-модели
    await nft.sync(db, FakeRelayer([]))
    assert (await db.one("SELECT MIN(stock) s FROM nft_models WHERE test=1"))["s"] == 999
    assert await casino.demo_clear() == 3


async def test_shop_buy_goes_to_inventory(env):
    db, casino = env
    await db.conn.execute("UPDATE users SET balance=7000 WHERE id=43")
    with pytest.raises(GameError, match="нет в магазине"):
        await casino.shop_buy(43, 2)                                             # цена устарела
    r = await casino.shop_buy(43, 1)
    assert r["balance"] == 1000 and r["model"] == "Black"
    item = (await casino.gifts(43))[0]
    assert item["status"] == "owned" and item["value"] == 6000 and item["demo"]   # купленный — для апгрейда
    await db.credit_payment("p43", 43, 100)
    with pytest.raises(GameError, match="после апгрейда"):
        await casino.gift_withdraw_claim(43, item["id"])                     # сразу вывести нельзя — только поставить
    with pytest.raises(GameError, match="Недостаточно"):
        await casino.shop_buy(43, 1)


async def test_daily_quest(env):
    db, casino = env
    with pytest.raises(GameError, match="подпишитесь"):
        await casino.quest_claim(43, False)
    r = await casino.quest_claim(43, True)
    assert r["reward"] == g.QUEST_REWARD and r["balance"] == g.QUEST_REWARD and not r["ready"]
    with pytest.raises(GameError, match="завтра"):
        await casino.quest_claim(43, True)
    await db.conn.execute("UPDATE users SET quest_at=quest_at-? WHERE id=43", (g.QUEST_EVERY,))
    assert (await casino.quest_status(43))["ready"]


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


async def test_top_up_demo_skips_models_without_upgrades(env):
    from app import main
    db, casino = env

    class Market:
        async def sample_models(self, count, per_collection=3):
            return [{"title": t, "model": m, "emoji": "🎁", "rarity": 1.0, "price": 500} for t, m in (
                ("Lol Pop", "Pink"), ("Lol Pop", "Blue"), ("New Gift", "Premarket"))]

    class Rel:
        ready, market = True, Market()

        async def has_model(self, collection, model):
            return collection == "Lol Pop"          # у «New Gift» улучшения ещё не вышли

    await casino.demo_fill([{"title": "Old Gift", "model": "X", "emoji": "🎁", "price": 300}])
    await main.top_up_demo(casino, Rel())
    rows = await db.all("SELECT collection_name, model, enabled FROM nft_models WHERE test=1 ORDER BY id")
    assert [(r["collection_name"], r["model"], r["enabled"]) for r in rows] == [
        ("Old Gift", "X", 0), ("Lol Pop", "Pink", 1), ("Lol Pop", "Blue", 1)]


async def test_unupgraded_gifts_never_shown(env):
    """Неулучшенный подарок на MRKT: вместо модели — число. Такой не попадает ни в кейсы, ни в апгрейд."""
    from app import main
    db, casino = env
    now = time.time()
    await db.conn.execute(
        "INSERT INTO nft_models(collection_id, collection_name, model, emoji, stock, price, price_at, test) "
        "VALUES ('demo:Airplane','Airplane','5818708013426410448','✈️',999,549500,?,1)", (now,))
    assert await casino.demo_fill([{"title": "Airplane", "model": "123456", "emoji": "✈️", "price": 5}]) == 0
    assert all(t["model"] != "5818708013426410448" for t in await casino.upgrade_targets(42))

    class Rel:
        ready = False

    await main.top_up_demo(casino, Rel())
    assert (await db.one("SELECT enabled FROM nft_models WHERE collection_name='Airplane'"))["enabled"] == 0
