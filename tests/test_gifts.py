"""Подарки игроков: зачисление присланных релейеру подарков, ставки NFT в PvP, продажа и вывод."""
import time

import pytest

from app import gifts, nft
from app.casino import Casino, GameError
from app.config import Config
from app.db import Database
from app.relayer import RelayerError


def sent(ref, sender, kind="nft", model="Frog", number=1, date=None, convert=0):
    return {"ref": ref, "from_user": sender, "date": date if date is not None else time.time(), "transfer_at": 0.0,
            "transfer_stars": 0, "kind": kind, "collection_id": "100",
            "collection_name": "Plush Pepe" if kind == "nft" else "Мишка", "number": number if kind == "nft" else None,
            "model": model if kind == "nft" else None, "rarity": 1.5 if kind == "nft" else None, "emoji": "🐸",
            "convert_stars": convert}


class FakeRelayer:
    ready = True

    def __init__(self, gifts_, prices=None):
        self.gifts = gifts_
        self.prices = prices or {}
        self.transfers = []
        self.fail = None

    async def received(self):
        return list(self.gifts)

    async def inventory(self):
        return [g for g in self.gifts if g["kind"] == "nft"]

    async def floor_price(self, collection_id, model, collection_name=None):
        return self.prices.get(model)

    async def transfer(self, item, user_id, username):
        if self.fail:
            raise RelayerError(self.fail)
        self.transfers.append((item["ref"], user_id))
        self.gifts = [g for g in self.gifts if g["ref"] != item["ref"]]

    async def username(self):
        return "svag_relayer"


@pytest.fixture
async def env(tmp_path):
    cfg = Config(bot_token="1:x", admin_ids=frozenset({7}), webapp_url="", db_path=str(tmp_path / "g.db"),
                 port=0, min_bet=1, max_bet=1000, start_bonus=0, log_level="INFO")
    db = Database(cfg.db_path)
    await db.connect()
    for uid in (42, 43, 7):
        await db.touch_user(uid, f"u{uid}", f"U{uid}")
    await db.kv_set(gifts.SINCE_KEY, str(time.time() - 60))
    yield cfg, db, Casino(db, cfg)
    await db.close()


async def balance(db, uid):
    return (await db.get_user(uid))["balance"]


async def test_scan_credits_nft_and_regular_gifts(env):
    cfg, db, casino = env
    relayer = FakeRelayer([
        sent(1, 42), sent(2, 42, kind="gift", convert=85),
        sent(3, 7),                                   # от админа — это пополнение запаса казино
        sent(4, 43, date=time.time() - 3600),         # пришёл до включения функции
    ], {"Frog": 9000})
    credited = await gifts.scan(db, cfg, relayer)
    assert sorted(c["ref"] for c in credited) == [1, 2]
    assert await balance(db, 42) == 85                # обычный подарок — звёздами по курсу обмена
    mine = await casino.gifts(42)
    assert [(g["title"], g["value"], g["priced"], g["sell"]) for g in mine] == [("Plush Pepe #1", 9000, True, 8100)]
    assert await casino.gifts(43) == []
    assert await gifts.scan(db, cfg, relayer) == []   # повторно не зачисляется
    assert await balance(db, 42) == 85
    assert "Мои подарки" in gifts.deposit_text(credited[0]) or "Мои подарки" in gifts.deposit_text(credited[1])


async def test_first_scan_sets_watermark(env):
    cfg, db, casino = env
    await db.kv_set(gifts.SINCE_KEY, None)
    relayer = FakeRelayer([sent(1, 42)], {"Frog": 9000})
    assert await gifts.scan(db, cfg, relayer) == []   # старые подарки релейера не раздаются игрокам
    assert await casino.gifts(42) == []


async def test_player_gifts_are_not_casino_stock(env):
    cfg, db, casino = env
    relayer = FakeRelayer([sent(1, 42), sent(2, 7, number=2)], {"Frog": 9000})
    await nft.sync(db, relayer)                    # сканер ещё не видел подарки — их нельзя отдать в кейсах
    assert await db.one("SELECT stock FROM nft_models WHERE stock > 0") is None
    await gifts.scan(db, cfg, relayer)
    await nft.sync(db, relayer)
    assert (await db.one("SELECT stock FROM nft_models"))["stock"] == 1   # только подарок админа
    gift_id = (await casino.gifts(42))[0]["id"]
    await casino.gift_sell(42, gift_id)                                   # продал — стал запасом казино
    await nft.sync(db, relayer)
    assert (await db.one("SELECT stock FROM nft_models"))["stock"] == 2


async def test_pvp_with_gift_stake(env):
    cfg, db, casino = env
    relayer = FakeRelayer([sent(1, 42)], {"Frog": 900})
    await gifts.scan(db, cfg, relayer)
    await db.conn.execute("UPDATE users SET balance=1000 WHERE id=43")
    gift_id = (await casino.gifts(42))[0]["id"]
    state = await casino.pvp_bet(42, None, "roulette", [gift_id])
    assert state["round"]["pot"] == 900 and state["round"]["players"][0]["gifts"][0]["title"] == "Plush Pepe #1"
    with pytest.raises(GameError):
        await casino.pvp_bet(42, None, "roulette", [gift_id])            # уже в банке
    with pytest.raises(GameError):
        await casino.gift_sell(42, gift_id)
    with pytest.raises(GameError):
        await casino.pvp_bet(43, None, "roulette", [gift_id])            # чужой подарок
    await casino.pvp_bet(43, 100, "roulette")
    await db.conn.execute("UPDATE pvp_rounds SET ends_at=1")
    [result] = await casino.pvp_tick()
    winner = result["winner"]
    assert result["gifts_value"] == 900 and result["stars"] == result["payout"] - 900   # звёзды — остаток банка
    assert [g["id"] for g in await casino.gifts(winner)] == [gift_id]    # подарок у победителя
    loser = 43 if winner == 42 else 42
    assert await casino.gifts(loser) == []
    assert (await db.one("SELECT status, round_id FROM user_gifts")) == {"status": "owned", "round_id": None}


async def test_pvp_gift_refund_when_alone(env):
    cfg, db, casino = env
    relayer = FakeRelayer([sent(1, 42)], {"Frog": 900})
    await gifts.scan(db, cfg, relayer)
    await db.conn.execute("UPDATE users SET balance=50 WHERE id=42")
    gift_id = (await casino.gifts(42))[0]["id"]
    await casino.pvp_bet(42, 50, "hockey", [gift_id])
    assert await balance(db, 42) == 0
    await db.conn.execute("UPDATE pvp_bets SET joined=0")
    await casino.pvp_tick()
    assert await balance(db, 42) == 50                                   # звёзды вернулись
    assert (await casino.gifts(42))[0]["status"] == "owned"             # и подарок тоже


async def test_unpriced_gift_cannot_be_staked(env):
    cfg, db, casino = env
    relayer = FakeRelayer([sent(1, 42, model="Rare")])                  # модели нет на маркете
    await gifts.scan(db, cfg, relayer)
    [g] = await casino.gifts(42)
    assert not g["priced"] and g["sell"] is None
    with pytest.raises(GameError, match="проверяется"):
        await casino.pvp_bet(42, None, "roulette", [g["id"]])
    relayer.prices["Rare"] = 5000                                        # появилась цена
    await gifts.reprice(db, relayer)
    assert (await casino.gifts(42))[0]["value"] == 5000


async def test_withdraw_gift(env):
    cfg, db, casino = env
    relayer = FakeRelayer([sent(1, 42)], {"Frog": 900})
    await gifts.scan(db, cfg, relayer)
    gift_id = (await casino.gifts(42))[0]["id"]
    with pytest.raises(GameError, match="пополнение"):
        await gifts.withdraw(casino, relayer, 42, gift_id)              # без пополнения за неделю — нельзя
    assert await db.credit_payment("p42", 42, 100)
    relayer.fail = "NEED_CONTACT"
    with pytest.raises(GameError, match="@svag_relayer"):
        await gifts.withdraw(casino, relayer, 42, gift_id)
    assert (await casino.gifts(42))[0]["status"] == "owned"             # не удалось — подарок остался
    relayer.fail = None
    with pytest.raises(GameError):
        await gifts.withdraw(casino, relayer, 43, gift_id)               # чужой
    assert (await gifts.withdraw(casino, relayer, 42, gift_id))["ok"]
    assert relayer.transfers == [(1, 42)]
    assert await casino.gifts(42) == []


async def test_crash_with_gift_stake(env, monkeypatch):
    from app.games import logic as g
    cfg, db, casino = env
    relayer = FakeRelayer([sent(1, 42), sent(2, 43, number=2)], {"Frog": 1000})
    await gifts.scan(db, cfg, relayer)
    await db.conn.execute("UPDATE users SET balance=100 WHERE id IN (42, 43)")
    monkeypatch.setattr(g, "crash_point", lambda rng=None: 2.0)
    await casino.crash_tick(now=0)                                         # раунд приёма ставок
    await db.conn.execute("UPDATE crash_rounds SET betting_until=?", (time.time() + 60,))
    g42 = (await casino.gifts(42))[0]["id"]
    g43 = (await casino.gifts(43))[0]["id"]
    with pytest.raises(GameError, match="★"):
        await casino.crash_bet(42, None, None, "ton", [g42])               # NFT — только на звёзды
    r = await casino.crash_bet(42, 50, None, "stars", [g42])
    assert r["bet"] == 1050 and r["gifts"][0]["value"] == 1000 and r["balance"] == 50
    await casino.crash_bet(43, None, None, "stars", [g43])
    state = await casino.crash_state(42)
    assert state["my"]["gifts"][0]["id"] == g42 and state["players"][0]["gifts"]
    await db.conn.execute("UPDATE crash_rounds SET status='running', started_at=?", (time.time() - 5,))
    out = await casino.crash_cashout(42)                                    # вывел примерно на ×1.4
    assert out["win"] > 1050
    assert (await casino.gifts(42))[0]["status"] == "owned"                 # NFT вернулся
    assert (await db.get_user(42))["balance"] == 50 + out["win"] - 1000     # прибыль — звёздами
    await db.conn.execute("UPDATE crash_rounds SET started_at=?", (time.time() - 100,))
    await casino.crash_tick()                                               # ракета взорвалась
    assert await casino.gifts(43) == []                                     # не успел — NFT ушёл казино
    assert (await db.one("SELECT status FROM user_gifts WHERE id=?", g43))["status"] == "lost"
