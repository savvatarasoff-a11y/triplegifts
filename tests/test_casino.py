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


async def test_slots_with_telegram_value(casino):
    await fund(casino, 1, 100)
    r = await casino.slots(1, 10, value=64)     # 777 без подходящего NFT -> ×40 звёздами
    assert r["reels"] == ["seven", "seven", "seven"] and r["win"] == 400 and r["balance"] == 490 and "nft" not in r
    r = await casino.slots(1, 10, value=2)      # 🍇 BAR BAR — пара больше ничего не даёт
    assert r["multiplier"] == 0 and r["win"] == 0
    r = await casino.slots(1, 10, value=48)     # 7 7 🍋 — две семёрки, возврат ставки
    assert r["reels"].count("seven") == 2 and r["win"] == 10


async def test_slots_777_gives_nft_near_x40(casino):
    import time as _t
    now = _t.time()
    await casino.db.conn.execute(
        "INSERT INTO nft_models(collection_id, collection_name, model, emoji, stock, price, price_at) VALUES "
        "('1','Plush Pepe','Frog','🐸',1,3900,?), ('2','Durov''s Cap','Black','🧢',1,3000,?), "
        "('3','Snoop Dogg','Old',NULL,1,4100,?)", (now, now, now))
    await fund(casino, 1, 1000)
    r = await casino.slots(1, 100, value=64)    # цель 4000 ⭐ -> 3900 (4100 ближе, но дороже ×40 — нельзя)
    assert r["nft"]["title"] == "Plush Pepe" and r["nft"]["price"] == 3900 and r["win"] == 3900
    assert r["balance"] == 900                   # NFT не зачисляется звёздами
    assert (await casino.db.one("SELECT reserved FROM nft_models WHERE collection_id='1'"))["reserved"] == 1
    r = await casino.slots(1, 100, value=64)    # 3900 уже зарезервирован -> следующий в допуске: 3000
    assert r["nft"]["price"] == 3000
    r = await casino.slots(1, 100, value=64)    # в допуске (×30…×40) больше ничего -> ×40 звёздами
    assert "nft" not in r and r["win"] == 4000


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


async def test_plinko(casino):
    await fund(casino, 1, 1000)
    r = await casino.plinko(1, 100, 16, "high")
    assert len(r["path"]) == 16 and r["bucket"] == sum(r["path"])
    assert r["multiplier"] == g.PLINKO_TABLES[(16, "high")][r["bucket"]] and r["win"] == g.payout(100, r["multiplier"])
    assert r["balance"] == 900 + r["win"]
    for rows, risk in ((10, "low"), (8, "insane"), ("8", "low")):
        with pytest.raises(GameError):
            await casino.plinko(1, 10, rows, risk)


async def test_pickaxe(casino):
    await fund(casino, 1, 1000)
    r = await casino.pickaxe(1, 100, "diamond")
    assert r["events"] and r["events"][-1]["hp"] == 0 and r["world"] and r["win"] == g.payout(100, r["multiplier"])
    assert r["balance"] == 900 + r["win"]
    with pytest.raises(GameError):
        await casino.pickaxe(1, 10, "wood")
    with pytest.raises(GameError):
        await casino.mines_start(1, 10, 1)


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
    assert await casino.revoke_check(code2) == 0          # чек админа: отозван, возвращать нечего
    with pytest.raises(GameError, match="отозван"):
        await casino.activate_check(3, code2)
    with pytest.raises(GameError):
        await casino.activate_check(3, "../../etc")


async def test_mines_win_and_lose(casino, monkeypatch):
    await fund(casino, 1, 100)
    monkeypatch.setattr(g, "mines_place", lambda mines, rng=None: [0, 1, 2])
    await casino.mines_start(1, 10, 5)
    with pytest.raises(GameError, match="закончите"):
        await casino.mines_start(1, 10, 5)
    s = await casino.mines_open(1, 10)
    assert s["opened"] == [10] and s["multiplier"] == g.mines_multiplier(5, 1)
    with pytest.raises(GameError):
        await casino.mines_open(1, 10)
    r = await casino.mines_cashout(1)
    assert r["win"] == g.payout(10, g.mines_multiplier(5, 1))
    assert r["layout"] == [0, 1, 2]
    assert await balance(casino, 1) == 90 + r["win"]

    await casino.mines_start(1, 10, 5)
    r = await casino.mines_open(1, 1)
    assert r["boom"] == 1 and r["win"] == 0
    assert await casino.mines_state(1) is None


async def crash_round(casino, point, monkeypatch, t0=1000.0):
    """Создаёт раунд с заданной точкой краша; возвращает функцию-«часы» для crash_tick."""
    monkeypatch.setattr(g, "crash_point", lambda rng=None: point)
    monkeypatch.setattr(casino_mod.time, "time", lambda: t0)
    await casino.crash_tick(t0)


async def test_crash_shared_round(casino, monkeypatch):
    await fund(casino, 1, 100)
    await fund(casino, 2, 100)
    await fund(casino, 3, 100)
    await crash_round(casino, 2.0, monkeypatch)
    await casino.crash_bet(1, 10)
    await casino.crash_bet(2, 20, auto=1.5)
    await casino.crash_bet(3, 30)
    with pytest.raises(GameError, match="уже сделали"):
        await casino.crash_bet(1, 10)
    st = await casino.crash_state(1)
    assert st["round"]["phase"] == "betting" and [p["id"] for p in st["players"]] == [3, 2, 1]

    t_start = 1000.0 + casino_mod.CRASH_BETTING_SECONDS
    await casino.crash_tick(t_start)                          # старт полёта
    with pytest.raises(GameError, match="перед стартом"):
        monkeypatch.setattr(casino_mod.time, "time", lambda: t_start + 1)
        await casino.crash_bet(1, 5)

    t_12 = t_start + g.crash_time_of(1.2) + 0.01              # игрок 1 забирает на ×1.2
    monkeypatch.setattr(casino_mod.time, "time", lambda: t_12)
    r = await casino.crash_cashout(1)
    assert r["cashout"] == 1.2 and r["win"] == 12

    t_16 = t_start + g.crash_time_of(1.6)                     # автовывод игрока 2 на ×1.5
    await casino.crash_tick(t_16)
    await casino.crash_tick(t_start + g.crash_time_of(2.0) + 0.01)  # краш на ×2
    st = await casino.crash_state(1)
    assert st["round"]["phase"] == "crashed" and st["round"]["point"] == 2.0
    by_id = {p["id"]: p for p in st["players"]}
    assert by_id[1]["win"] == 12 and by_id[2]["cashout"] == 1.5 and by_id[2]["win"] == 30 and by_id[3]["win"] == 0
    assert st["my"]["cashout"] == 1.2 and st["history"] == [2.0]
    assert await balance(casino, 1) == 102 and await balance(casino, 2) == 110 and await balance(casino, 3) == 70
    with pytest.raises(GameError):
        await casino.crash_cashout(3)

    # после паузы — новый раунд
    await casino.crash_tick(t_start + g.crash_time_of(2.0) + casino_mod.CRASH_PAUSE_SECONDS + 1)
    assert (await casino.crash_state())["round"]["phase"] == "betting"


async def test_crash_late_cashout_and_restart(casino, monkeypatch):
    await fund(casino, 1, 100)
    await crash_round(casino, 1.3, monkeypatch)
    await casino.crash_bet(1, 10, auto=5)
    t_start = 1000.0 + casino_mod.CRASH_BETTING_SECONDS
    await casino.crash_tick(t_start)
    # бот «лежал» 60 секунд: следующий тик сразу видит краш, автовывод выше точки не срабатывает
    monkeypatch.setattr(casino_mod.time, "time", lambda: t_start + 60)
    with pytest.raises(GameError, match="Не успели"):
        await casino.crash_cashout(1)
    await casino.crash_tick(t_start + 60)
    assert await balance(casino, 1) == 90
    assert (await casino.crash_state(1))["my"]["win"] == 0


async def test_crash_instant(casino, monkeypatch):
    await fund(casino, 1, 10)
    await crash_round(casino, 1.0, monkeypatch)
    await casino.crash_bet(1, 10)
    t_start = 1000.0 + casino_mod.CRASH_BETTING_SECONDS
    await casino.crash_tick(t_start)
    await casino.crash_tick(t_start + 0.05)
    assert (await casino.crash_state(1))["round"]["point"] == 1.0
    assert await balance(casino, 1) == 0


GIFT_CASE = {"id": "bear", "price": 25, "prizes": [
    {"kind": "gift", "emoji": "🧸", "gift_id": "g1", "amount": 15, "weight": 1},
]}


async def test_cases_gift(casino):
    await fund(casino, 1, 25)
    r = await casino.open_case(1, GIFT_CASE)
    assert r["kind"] == "gift" and r["prize"] == 15 and r["gift"] == "🧸" and r["balance"] == 15


async def test_cases_open_several(casino):
    await fund(casino, 1, 100)
    r = await casino.open_case(1, GIFT_CASE, 3)
    assert (r["count"], r["cost"], r["total"], len(r["items"])) == (3, 75, 45, 3)
    assert r["balance"] == 100 - 75 + 45
    assert (await casino.db.one("SELECT COUNT(*) n FROM bets WHERE game='case'"))["n"] == 3
    with pytest.raises(GameError, match="Недостаточно"):
        await casino.open_case(1, GIFT_CASE, 5)                  # 125 > 70 — ничего не списалось
    assert await balance(casino, 1) == 70
    for bad in (0, 6, "2", True):
        with pytest.raises(GameError):
            await casino.open_case(1, GIFT_CASE, bad)


async def test_cases_nft_model_reserved_not_credited(casino):
    import time as _t
    await casino.db.conn.execute(
        "INSERT INTO nft_models(collection_id, collection_name, model, stock, price, price_at) "
        "VALUES ('555','Plush Pepe','Frog Prince',1,5000,?)", (_t.time(),))
    nft_case = {"id": "nft", "price": 250, "prizes": [
        {"kind": "nft", "model_id": 1, "emoji": "🐸", "title": "Plush Pepe", "model": "Frog Prince",
         "amount": 5000, "weight": 1},
    ]}
    await fund(casino, 1, 500)
    await fund(casino, 2, 500)
    r = await casino.open_case(1, nft_case)
    assert r["kind"] == "nft" and r["nft"]["model"] == "Frog Prince" and r["nft"]["win_id"] == 1
    assert r["balance"] == 250                                   # NFT не зачисляется звёздами
    assert (await casino.db.one("SELECT stock, reserved FROM nft_models")) == {"stock": 1, "reserved": 1}
    assert (await casino.db.one("SELECT user_id, status, price FROM nft_wins")) == {
        "user_id": 1, "status": "won", "price": 5000}
    with pytest.raises(GameError, match="закончились"):          # единственный подарок модели уже зарезервирован
        await casino.open_case(2, nft_case)
    assert await balance(casino, 2) == 500                       # списание откатилось
    with pytest.raises(GameError, match="закончились"):          # и в пачке — вся пачка откатывается
        await casino.open_case(2, nft_case, 2)
    assert await balance(casino, 2) == 500


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


async def test_big_wins_feed(casino, monkeypatch):
    await fund(casino, 1, 100)
    await casino.slots(1, 2, value=64)          # 777 -> ×40
    await casino.slots(1, 2, value=48)          # две семёрки, ×1 — в ленту не попадает
    feed = await casino.big_wins()
    assert feed == [{"game": "slots", "bet": 2, "win": 80, "x": 40.0, "cur": "stars", "name": "User1"}]


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


async def test_withdraw_flow(casino):
    await fund(casino, 1, 100)
    wd = await casino.withdraw_request(1, "g1", 50, "🧸")
    assert wd["balance"] == 50
    with pytest.raises(GameError, match="уже есть заявка"):
        await casino.withdraw_request(1, "g1", 15, "🌹")
    # отправка: claim -> finish(ok)
    assert (await casino.withdraw_claim(wd["id"], 7))["status"] == "sending"
    assert await casino.withdraw_claim(wd["id"], 7) is None          # второй клик
    await casino.withdraw_finish(wd["id"], ok=True)
    assert (await casino.get_withdrawal(wd["id"]))["status"] == "sent"
    assert await casino.withdraw_reject(wd["id"], 7) is None          # отправленную не отклонить
    assert await balance(casino, 1) == 50


async def test_withdraw_failed_send_returns_to_pending_and_reject_refunds(casino):
    await fund(casino, 1, 100)
    wd = await casino.withdraw_request(1, "g1", 100, "💝")
    await casino.withdraw_claim(wd["id"], 7)
    await casino.withdraw_finish(wd["id"], ok=False, error="BALANCE_TOO_LOW")
    row = await casino.get_withdrawal(wd["id"])
    assert row["status"] == "pending" and row["error"] == "BALANCE_TOO_LOW"
    rej = await casino.withdraw_reject(wd["id"], 7)
    assert rej["balance"] == 100
    st = await casino.db.stats()
    assert st["withdrawn"] == 0 and st["withdraw_pending"] == 0


async def test_withdraw_requires_wagering_free_stars(casino):
    code = await casino.create_check(7, 100, 1)
    await casino.activate_check(1, code)
    with pytest.raises(GameError, match="отыграйте"):
        await casino.withdraw_request(1, "g1", 50, "🧸")
    assert (await casino.wager_status(1))["left"] == 100
    for _ in range(10):
        await casino.slots(1, 5) if await balance(casino, 1) >= 5 else None
    await casino.db.conn.execute("UPDATE users SET wagered=100, balance=100 WHERE id=1")
    wd = await casino.withdraw_request(1, "g1", 50, "🧸")
    assert wd["status"] == "pending"
    with pytest.raises(GameError, match="Недостаточно"):
        await casino.withdraw_request(2, "g1", 50, "🧸")


async def test_pvp_hockey_round(casino, monkeypatch):
    await fund(casino, 1, 100)
    await fund(casino, 2, 100)
    await casino.pvp_bet(1, 30, "hockey")
    await casino.pvp_bet(2, 10, "roulette")              # раунды разных игр не смешиваются
    s = await casino.pvp_bet(2, 50, "hockey")
    assert s["game"] == "hockey" and s["round"]["pot"] == 80
    assert (await casino.pvp_state(1, "roulette"))["round"]["pot"] == 10
    await casino.db.conn.execute("UPDATE pvp_rounds SET ends_at=? WHERE game='hockey'", (time.time() - 1,))
    monkeypatch.setattr(g, "pvp_pick_winner", lambda bets, rng=None: (2, 40))
    res = await casino.pvp_tick()
    assert [r["game"] for r in res] == ["hockey"]
    last = (await casino.pvp_state(1, "hockey"))["last"]
    zones = last["detail"]["zones"]
    assert zones == [[0.0, 60.0], [60.0, 160.0]]          # 30 и 50 из 80
    tx, ty = last["detail"]["points"][-1]
    assert 60 <= ty <= 160                                 # шайба в зоне победителя
    assert await balance(casino, 2) == 100 - 10 - 50 + 76
    with pytest.raises(GameError):
        await casino.pvp_bet(1, 5, "chess")


async def test_migration_adds_pvp_columns(tmp_path):
    import aiosqlite
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as c:
        await c.execute("CREATE TABLE pvp_rounds (id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL DEFAULT 'open', "
                        "created_at REAL NOT NULL, ends_at REAL, winner_id INTEGER, pot INTEGER NOT NULL DEFAULT 0, "
                        "payout INTEGER NOT NULL DEFAULT 0, ticket INTEGER, finished_at REAL)")
        await c.execute("INSERT INTO pvp_rounds(created_at, status) VALUES (1, 'done')")
        await c.commit()
    db = Database(path)
    await db.connect()
    row = await db.one("SELECT game, detail FROM pvp_rounds")
    assert row == {"game": "roulette", "detail": None}
    await db.close()


async def test_referral_bonus_and_rules(casino):
    db = casino.db
    for uid in (100, 101, 102):
        await db.touch_user(uid, None, f"U{uid}")
    assert not await db.set_referrer(100, 100)                    # себя нельзя
    assert await db.set_referrer(101, 100)
    assert not await db.set_referrer(101, 102)                    # второй раз нельзя
    assert not await db.set_referrer(100, 101)                    # по кругу нельзя
    assert await db.credit_payment("p1", 101, 250)
    assert (await db.get_user(100))["balance"] == 25              # 10% пригласившему
    assert not await db.credit_payment("p1", 101, 250)            # повтор апдейта не платит дважды
    assert (await db.get_user(100))["balance"] == 25
    await db.credit_payment("p2", 102, 500)                       # чужой игрок — бонуса нет
    assert (await db.get_user(100))["balance"] == 25
    assert not await db.set_referrer(102, 100)                    # уже покупал — привязать нельзя
    info = await casino.referrals(100)
    assert (info["count"], info["earned"], info["list"][0]["earned"]) == (1, 25, 25)
    assert (await casino.wager_status(100))["left"] == 25         # бонус нужно отыграть
    await db.conn.execute("UPDATE users SET created_at=0 WHERE id=102")
    await db.touch_user(103, None, "old")
    await db.conn.execute("UPDATE users SET created_at=0 WHERE id=103")
    assert not await db.set_referrer(103, 100)                    # старый игрок — нельзя


async def test_profile(casino):
    await casino.db.credit_payment("p", 1, 100)
    p = await casino.profile(1)
    assert p["deposited"] == 100 and p["games"] == 0 and p["best"] is None


async def test_vip_rakeback(casino, monkeypatch):
    db = casino.db
    await fund(casino, 1, 20_000)
    v = await casino.vip(1)
    assert v["level"] == 0 and v["rakeback"] == 0.002 and v["next"]["at"] == 2_000
    monkeypatch.setattr(g, "plinko_drop", lambda rows, risk, rng=None: ([0] * rows, 0, 0.0))
    for _ in range(3):
        await casino.plinko(1, 1000, 8, "low")                     # 3000 ставок: 0.2% → 0.4% после 2000
    v = await casino.vip(1)
    assert v["level"] == 1 and v["points"] == 3000
    # 1000·0.2% + 1000·0.4% (уровень поднялся после 2-й ставки) + 1000·0.4% = 2 + 4 + 4 = 10 ⭐
    assert v["rake"]["stars"] == 10
    r = await casino.claim_rakeback(1)
    assert r["stars"] == 10 and r["balance"] == 20_000 - 3000 + 10
    with pytest.raises(GameError, match="копится"):
        await casino.claim_rakeback(1)
    assert (await casino.wager_status(1))["left"] == 0             # рейкбек не нужно отыгрывать
    # рейкбек никогда не больше комиссии казино: максимум 1% при минимальной комиссии 5%
    assert max(lv[3] for lv in g.LEVELS) <= 0.05 / 5


async def test_daily_bonus(casino, monkeypatch):
    with pytest.raises(GameError, match="пополнения"):
        await casino.daily_bonus(1)                                # без пополнений — нельзя (против фарма)
    await fund(casino, 1, 10)
    with pytest.raises(GameError, match="от 50"):
        await casino.daily_bonus(1)                                # и не с копеечного пополнения
    await fund(casino, 1, 40)
    monkeypatch.setattr(g, "daily_bonus_roll", lambda rng=None: 25)
    r = await casino.daily_bonus(1)
    assert r["amount"] == 25 and r["balance"] == 75
    with pytest.raises(GameError, match="уже получен"):
        await casino.daily_bonus(1)
    assert (await casino.wager_status(1))["left"] == 25            # бонус нужно отыграть
    await casino.db.conn.execute("UPDATE users SET bonus_at=0 WHERE id=1")
    assert (await casino.daily_bonus(1))["amount"] == 25           # через сутки снова
    assert 3 < g.daily_bonus_ev() < 5


async def test_leaders(casino):
    await fund(casino, 1, 1000)
    await fund(casino, 2, 1000)
    await casino.plinko(1, 100, 8, "low")
    await casino.plinko(2, 300, 8, "low")
    await casino.db.credit_ton("t", 3, 10 ** 9 * 5)
    await casino.plinko(3, 5 * 10 ** 9, 8, "low", cur="ton")       # 5 TON = 500 очков
    lb = await casino.leaders(1)
    assert [p["id"] for p in lb["top"]] == [3, 2, 1] and lb["top"][0]["points"] == 500
    assert lb["me"]["place"] == 3


async def test_player_checks_paid_from_balance(casino):
    db = casino.db
    for uid in (1, 2, 3):
        await db.touch_user(uid, f"u{uid}", f"U{uid}")
    await db.conn.execute("UPDATE users SET balance=500 WHERE id=1")
    with pytest.raises(GameError, match="Недостаточно"):
        await casino.create_check(1, 300, 2, paid=True)
    code = await casino.create_check(1, 100, 3, paid=True)
    assert (await db.get_user(1))["balance"] == 200                  # 100 × 3 списано сразу
    with pytest.raises(GameError, match="Свой чек"):
        await casino.activate_check(1, code)
    assert (await casino.activate_check(2, code))[0] == 100
    assert (await casino.wager_status(2))["left"] == 100             # полученное по чеку нужно отыграть
    assert await casino.revoke_check(code, by_user=2) is None        # чужой чек отозвать нельзя
    assert await casino.revoke_check(code, by_user=1) == 200         # остаток 2 × 100 вернулся
    assert (await db.get_user(1))["balance"] == 400
    with pytest.raises(GameError):
        await casino.activate_check(3, code)
    assert (await db.stats())["checks_redeemed"] == 0                # чеки игроков — не расход казино


async def test_free_case_daily_bonus_stars(casino, monkeypatch):
    db = casino.db
    await db.touch_user(1, "u1", "U1")
    info = await casino.free_case_info(1)
    assert info["available"] and info["prizes"][0]["amount"] == 1
    chances = {p["amount"]: p["chance"] for p in info["prizes"]}
    assert chances[1] > 55 and chances[500] < 0.02                     # крупное — редко, но шанс честный
    assert sum(p["chance"] for p in info["prizes"]) == pytest.approx(100, abs=0.01)
    monkeypatch.setattr(g, "pick_weighted", lambda items, weights, rng=None: items[1])
    r = await casino.open_free_case(1)
    assert r["prize"] == 2 and r["balance"] == 2
    assert (await casino.wager_status(1))["left"] == 2                  # бонусные — нужно отыграть
    with pytest.raises(GameError, match="через сутки"):
        await casino.open_free_case(1)
    assert not (await casino.free_case_info(1))["available"]


async def test_free_case_nft_prize_goes_to_profile(casino, monkeypatch):
    db = casino.db
    await db.touch_user(1, "u1", "U1")
    await db.conn.execute("INSERT INTO nft_models(collection_id, collection_name, model, emoji, stock, price, price_at, "
                          "test) VALUES ('demo:Lol Pop','Lol Pop','Pink','🍭',999,900,?,1)", (time.time(),))
    prizes = (await casino.free_case_info(1))["prizes"]
    assert prizes[-1]["kind"] == "nft" and prizes[-1]["chance"] < 0.01
    monkeypatch.setattr(g, "pick_weighted", lambda items, weights, rng=None: items[-1])
    r = await casino.open_free_case(1)
    assert r["nft"]["demo"] and [x["model"] for x in await casino.gifts(1)] == ["Pink"]


async def test_leaders_reset(casino):
    db = casino.db
    await db.touch_user(1, "u1", "U1")
    await db.conn.execute("UPDATE users SET balance=100 WHERE id=1")
    await casino.plinko(1, 10)
    assert (await casino.leaders(1))["top"]
    await casino.leaders_reset()
    assert (await casino.leaders(1))["top"] == []                   # история ставок цела, таблица пустая
    assert (await db.one("SELECT COUNT(*) n FROM bets"))["n"] == 1
    await casino.plinko(1, 10)
    assert [p["points"] for p in (await casino.leaders(1))["top"]] == [10]
