import json
import time

import pytest

from app import casino as casino_mod
from app.casino import Casino, GameError
from app.config import Config
from app.db import Database
from app.games import logic as g


def make_cfg(tmp_path) -> Config:
    return Config(
        bot_token="1:x", admin_ids=frozenset({7}), webapp_url="https://example.com",
        db_path=str(tmp_path / "c.db"), port=0, min_bet=1, max_bet=10000, start_bonus=0, log_level="INFO",
    )


@pytest.fixture
async def casino(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    await db.connect()
    for uid in (1, 2, 3):
        await db.touch_user(uid, f"u{uid}", f"User{uid}")
    yield Casino(db, cfg)
    await db.close()


async def balance(casino, uid):
    return (await casino.db.get_user(uid))["balance"]


async def fund(casino, uid, amount):
    assert await casino.db.credit_payment(f"ch-{uid}-{amount}-{time.time()}", uid, amount)


async def test_payment_idempotent(casino):
    assert await casino.db.credit_payment("abc", 1, 100)
    assert not await casino.db.credit_payment("abc", 1, 100)
    assert await balance(casino, 1) == 100
    assert (await casino.db.get_user(1))["deposited"] == 100


async def test_bet_validation(casino):
    with pytest.raises(GameError, match="Недостаточно"):
        await casino.slots(1, 10)
    await fund(casino, 1, 50)
    for bad in (0, -5, 1.5, "10", True, 10001):
        with pytest.raises(GameError):
            await casino.slots(1, bad)
    assert await balance(casino, 1) == 50


async def test_slots_balance_consistent(casino):
    await fund(casino, 1, 1000)
    total_win = 0
    for _ in range(50):
        r = await casino.slots(1, 10)
        total_win += r["win"]
        assert r["balance"] == await balance(casino, 1)
    assert await balance(casino, 1) == 1000 - 500 + total_win
    ledger_sum = (await casino.db.one("SELECT SUM(delta) s FROM ledger WHERE user_id=1"))["s"]
    assert ledger_sum == await balance(casino, 1)


async def test_dice_and_roulette(casino):
    await fund(casino, 1, 1000)
    r = await casino.dice(1, 10, 50)
    assert r["win"] in (0, 19)
    with pytest.raises(GameError):
        await casino.dice(1, 10, 99)
    r = await casino.roulette(1, 10, "number", 7)
    assert r["win"] in (0, 360)
    with pytest.raises(GameError):
        await casino.roulette(1, 10, "number", 37)
    with pytest.raises(GameError):
        await casino.roulette(1, 10, "banana")


async def test_checks(casino):
    code = await casino.create_check(7, 100, 2)
    amount, bal = await casino.activate_check(1, code)
    assert (amount, bal) == (100, 100)
    with pytest.raises(GameError, match="уже активировали"):
        await casino.activate_check(1, code)
    await casino.activate_check(2, code)
    with pytest.raises(GameError, match="полностью"):
        await casino.activate_check(3, code)
    code2 = await casino.create_check(7, 5, 1)
    assert await casino.revoke_check(code2)
    with pytest.raises(GameError, match="отозван"):
        await casino.activate_check(3, code2)
    with pytest.raises(GameError):
        await casino.activate_check(3, "../../etc")


async def test_mines_win_and_lose(casino, monkeypatch):
    await fund(casino, 1, 100)
    monkeypatch.setattr(g, "mines_place", lambda mines, rng=None: [0, 1, 2])
    await casino.mines_start(1, 10, 3)
    with pytest.raises(GameError, match="закончите"):
        await casino.mines_start(1, 10, 3)
    s = await casino.mines_open(1, 10)
    assert s["opened"] == [10] and s["multiplier"] == g.mines_multiplier(3, 1)
    with pytest.raises(GameError):
        await casino.mines_open(1, 10)
    r = await casino.mines_cashout(1)
    assert r["win"] == g.payout(10, g.mines_multiplier(3, 1))
    assert r["layout"] == [0, 1, 2]
    assert await balance(casino, 1) == 90 + r["win"]

    await casino.mines_start(1, 10, 3)
    r = await casino.mines_open(1, 1)
    assert r["boom"] == 1 and r["win"] == 0
    assert await casino.mines_state(1) is None


async def test_crash_cashout_and_crash(casino, monkeypatch):
    await fund(casino, 1, 100)
    monkeypatch.setattr(g, "crash_point", lambda rng=None: 2.0)
    await casino.crash_start(1, 10)
    r = await casino.crash_cashout(1)
    assert r["status"] == "cashed" and r["win"] == 10  # множитель ~1.00 сразу после старта

    # Игра, начатая 20 секунд назад, уже упала на 2.0
    await casino.crash_start(1, 10)
    await casino.db.conn.execute("UPDATE crash_games SET started_at=? WHERE user_id=1", (time.time() - 20,))
    r = await casino.crash_cashout(1)
    assert r["status"] == "crashed" and r["win"] == 0 and r["point"] == 2.0

    # Автовывод на 1.5 срабатывает, даже если игрок закрыл приложение
    await casino.crash_start(1, 10, auto=1.5)
    await casino.db.conn.execute("UPDATE crash_games SET started_at=? WHERE user_id=1", (time.time() - 20,))
    assert await casino.crash_sweep() == 1
    assert await casino.crash_state(1) == {"status": "idle"}
    assert await balance(casino, 1) == 100 - 10 + 10 - 10 - 10 + 15


async def test_crash_instant(casino, monkeypatch):
    await fund(casino, 1, 10)
    monkeypatch.setattr(g, "crash_point", lambda rng=None: 1.0)
    await casino.crash_start(1, 10)
    r = await casino.crash_cashout(1)
    assert r["status"] == "crashed" and r["win"] == 0


async def test_cases(casino):
    await fund(casino, 1, 10)
    r = await casino.open_case(1, "bronze")
    assert r["balance"] == r["prize"]
    with pytest.raises(GameError):
        await casino.open_case(1, "nope")


async def test_pvp_round(casino, monkeypatch):
    await fund(casino, 1, 100)
    await fund(casino, 2, 100)
    s = await casino.pvp_bet(1, 30)
    assert s["round"]["ends_in"] is None and s["round"]["my_bet"] == 30
    await casino.pvp_bet(1, 20)
    s = await casino.pvp_bet(2, 50)
    assert s["round"]["pot"] == 100 and 29 <= s["round"]["ends_in"] <= 30
    assert [p["chance"] for p in s["round"]["players"]] == [50.0, 50.0]

    assert await casino.pvp_tick() == []  # время ещё не вышло
    await casino.db.conn.execute("UPDATE pvp_rounds SET ends_at=?", (time.time() - 1,))
    monkeypatch.setattr(g, "pvp_pick_winner", lambda bets, rng=None: (2, 70))
    results = await casino.pvp_tick()
    assert results[0]["winner"] == 2 and results[0]["payout"] == 95
    assert await balance(casino, 1) == 50
    assert await balance(casino, 2) == 50 + 95
    s = await casino.pvp_state(1)
    assert s["round"] is None and s["last"]["winner"]["id"] == 2
    with pytest.raises(GameError, match="Недостаточно"):
        await casino.pvp_bet(3, 10)


async def test_pvp_refund_lonely(casino, monkeypatch):
    await fund(casino, 1, 100)
    await casino.pvp_bet(1, 40)
    monkeypatch.setattr(casino_mod, "PVP_IDLE_REFUND", -1)
    await casino.pvp_tick()
    assert await balance(casino, 1) == 100


async def test_stats(casino):
    await fund(casino, 1, 100)
    await casino.slots(1, 10)
    code = await casino.create_check(7, 5, 3)
    await casino.activate_check(2, code)
    st = await casino.db.stats()
    assert st["deposited"] == 100 and st["wagered"] == 10 and st["checks_liability"] == 10
    assert st["checks_redeemed"] == 5
    detail = (await casino.db.one("SELECT detail FROM bets"))["detail"]
    assert "reels" in json.loads(detail)
