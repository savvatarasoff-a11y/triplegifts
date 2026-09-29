"""TON как вторая валюта: пополнение переводом, игры, раздельные PvP-раунды, вывод."""
import time

import pytest

from app import money, ton
from app.casino import Casino, GameError
from app.config import Config
from app.db import Database
from app.games import logic as g

NANO = money.NANO
ADDR = "UQ" + "A" * 46


@pytest.fixture
async def casino(tmp_path):
    cfg = Config(bot_token="1:x", admin_ids=frozenset({7}), webapp_url="", db_path=str(tmp_path / "t.db"),
                 port=0, min_bet=1, max_bet=1000, start_bonus=0, log_level="INFO")
    db = Database(cfg.db_path)
    await db.connect()
    for uid in (1, 2, 3):
        await db.touch_user(uid, f"u{uid}", f"U{uid}")
    c = Casino(db, cfg)

    async def rate():
        return 200.0                                   # 1 TON = 200 ⭐
    c.ton_rate = rate
    yield c
    await db.close()


async def ton_balance(casino, uid):
    return (await casino.db.get_user(uid))["ton"]


def tx(hash_, comment, value, source="EQsender", utime=None):
    return {"utime": utime or time.time(), "transaction_id": {"lt": "1", "hash": hash_},
            "in_msg": {"source": source, "value": str(value), "message": comment}}


async def test_deposit_scan_idempotent_with_referral(casino):
    db = casino.db
    assert await db.set_referrer(2, 1)
    async def fetcher(address):
        assert address == ADDR
        return [tx("h1", "SG2", 5 * NANO, utime=now + 1), tx("h2", " sg2 ", NANO, utime=now + 1),
                tx("h3", "hello", NANO), tx("h4", "SG2", NANO, source=""), tx("h5", "SG3", NANO, utime=1)]

    now = time.time()

    assert await ton.scan(db, fetcher) == []                                    # кошелёк не задан
    await ton.set_wallet(db, ADDR)
    credited = await ton.scan(db, fetcher)
    assert [(c["hash"], c["user_id"], c["amount"]) for c in credited] == [("h1", 2, 5 * NANO), ("h2", 2, NANO)]
    assert await ton.scan(db, fetcher) == []                                    # второй раз не зачисляется
    assert await ton_balance(casino, 2) == 6 * NANO
    assert await ton_balance(casino, 1) == 6 * NANO // 10                        # 10% пригласившему, в TON
    assert (await db.get_user(2))["balance"] == 0                                # звёзды не тронуты
    assert (await casino.wager_status(1, money.TON))["left"] == 6 * NANO // 10   # реф-бонус нужно отыграть
    assert ton.transfer_link(ADDR, "SG2") == f"ton://transfer/{ADDR}?text=SG2"


async def test_games_in_ton(casino, monkeypatch):
    db = casino.db
    await db.credit_ton("d", 1, 10 * NANO)
    with pytest.raises(GameError, match="0.01 TON"):
        await casino.plinko(1, NANO // 1000, 8, "low", cur="ton")
    monkeypatch.setattr(g, "plinko_drop", lambda rows, risk, rng=None: ([0] * rows, 0, 1.9))
    r = await casino.plinko(1, NANO, 8, "low", cur="ton")
    assert r["cur"] == "ton" and r["win"] == int(NANO * 1.9) and r["balance"] == 9 * NANO + int(NANO * 1.9)
    assert (await db.get_user(1))["balance"] == 0 and (await db.get_user(1))["ton_wagered"] == NANO
    with pytest.raises(GameError, match="TON"):
        await casino.plinko(2, NANO, 8, "low", cur="ton")                               # у игрока 2 нет TON
    with pytest.raises(GameError):
        await casino.plinko(1, NANO, 8, "low", cur="btc")
    m = await casino.mines_start(1, NANO, 3, cur="ton")
    assert m["cur"] == "ton"
    bets = await db.all("SELECT cur FROM bets")
    assert {b["cur"] for b in bets} == {"ton"}


async def test_case_in_ton_uses_rate(casino):
    await casino.db.credit_ton("d", 1, 10 * NANO)
    case = {"id": "bear", "price": 25, "prizes": [{"kind": "gift", "emoji": "🧸", "amount": 15, "weight": 1}]}
    r = await casino.open_case(1, case, 2, cur="ton")
    assert r["price"] == 125_000_000 and r["cost"] == 250_000_000                 # 25 ⭐ / 200 = 0.125 TON
    assert r["total"] == 2 * 75_000_000 and r["cur"] == "ton"
    casino.ton_rate = None
    with pytest.raises(GameError, match="Курс"):
        await casino.open_case(1, case, 1, cur="ton")


async def test_slots_777_nft_in_ton(casino, monkeypatch):
    db = casino.db
    await db.credit_ton("d", 1, 10 * NANO)
    await db.conn.execute("INSERT INTO nft_models(collection_id, collection_name, model, stock, price, price_at) "
                          "VALUES ('1','Plush Pepe','Frog',1,8000,?)", (time.time(),))
    monkeypatch.setattr(g, "slots_spin", lambda rng=None: (64, ["seven"] * 3, 40))
    r = await casino.slots(1, NANO, cur="ton")                                    # ×40 от 1 TON = 8000 ⭐ → NFT
    assert r["nft"]["model"] == "Frog" and r["win"] == 40 * NANO and r["balance"] == 9 * NANO


async def test_pvp_rounds_separate_by_currency(casino):
    db = casino.db
    for uid in (1, 2):
        await db.credit_payment(f"s{uid}", uid, 100)
        await db.credit_ton(f"t{uid}", uid, 5 * NANO)
    await casino.pvp_bet(1, 10, "roulette", cur="stars")
    s = await casino.pvp_bet(2, NANO, "roulette", cur="ton")
    assert s["cur"] == "ton" and len(s["round"]["players"]) == 1                  # не смешалось со звёздным
    await casino.pvp_bet(1, NANO, "roulette", cur="ton")
    await db.conn.execute("UPDATE pvp_rounds SET ends_at=1 WHERE cur='ton'")
    [res] = await casino.pvp_tick()
    assert res["cur"] == "ton" and res["payout"] == 2 * NANO - int(2 * NANO * g.PVP_COMMISSION)
    winner = res["winner"]
    assert await ton_balance(casino, winner) == 4 * NANO + res["payout"]
    assert (await casino.pvp_state(1, "roulette", "stars"))["round"]["pot"] == 10  # звёздный раунд живёт отдельно
    with pytest.raises(GameError, match="звёзды"):
        await casino.pvp_bet(1, None, "roulette", [1], cur="ton")                  # NFT — только в звёздный раунд


async def test_ton_withdraw(casino):
    db = casino.db
    await db.credit_ton("d", 1, 3 * NANO)
    with pytest.raises(GameError, match="адрес"):
        await casino.ton_withdraw_request(1, NANO, "not an address")
    with pytest.raises(GameError, match="от 0.1 TON"):
        await casino.ton_withdraw_request(1, NANO // 100, ADDR)
    with pytest.raises(GameError, match="поставьте"):
        await casino.ton_withdraw_request(1, 2 * NANO, ADDR)                    # оборот 1x: пополнил — сразу не вывести
    await db.conn.execute("UPDATE users SET ton_wagered=? WHERE id=1", (3 * NANO,))
    wd = await casino.ton_withdraw_request(1, 2 * NANO, ADDR)
    assert wd["balance"] == NANO
    with pytest.raises(GameError, match="уже есть"):
        await casino.ton_withdraw_request(1, NANO // 2, ADDR)
    assert (await casino.ton_withdraw_done(wd["id"], 7))["status"] == "sent"
    assert await casino.ton_withdraw_reject(wd["id"], 7) is None                   # уже отправлено
    wd2 = await casino.ton_withdraw_request(1, NANO, ADDR)
    assert (await casino.ton_withdraw_reject(wd2["id"], 7))["balance"] == NANO
    with pytest.raises(GameError, match="Недостаточно TON"):
        await casino.ton_withdraw_request(1, 5 * NANO, ADDR)
    stats = await db.stats()
    assert stats["ton_withdrawn"] == 2 * NANO and stats["ton_deposited"] == 3 * NANO


def test_money_format():
    assert money.fmt(1_500_000_000, "ton") == "1.5 TON"
    assert money.fmt(125_000_000, "ton") == "0.125 TON"
    assert money.fmt(42, "stars") == "42 ⭐"
    assert money.stars_to(25, "ton", 200, up=True) == 125_000_000 and money.to_stars(NANO, "ton", 200) == 200


async def test_default_wallet_and_off(casino):
    db = casino.db
    from app.casino import TON_ADDRESS_RE
    assert TON_ADDRESS_RE.match(ton.DEFAULT_WALLET)
    assert await ton.wallet(db) == ton.DEFAULT_WALLET                            # кошелёк казино по умолчанию
    fetched = []

    async def fetcher(address):
        fetched.append(address)
        return [tx("old", "SG2", NANO, utime=1)]

    assert await ton.scan(db, fetcher) == [] and fetched == []                  # первый запуск: только отметка времени
    assert await ton.scan(db, fetcher) == [] and fetched == [ton.DEFAULT_WALLET]  # старый перевод не зачислен
    await ton.set_wallet(db, None)
    assert await ton.wallet(db) is None                                          # /tonwallet off
