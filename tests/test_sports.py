import time

import pytest

from app import sports as sp
from app.casino import GameError
from tests.test_casino import balance, casino, fund  # noqa: F401  (фикстура casino)


def event(eid, home="Arsenal", away="Chelsea", kickoff=None, prices=((2.1, 3.4, 3.5), (2.0, 3.5, 3.6))):
    return {"id": eid, "home_team": home, "away_team": away, "commence_time": kickoff or time.time() + 86400,
            "bookmakers": [{"key": f"b{i}", "markets": [{"key": "h2h", "outcomes": [
                {"name": home, "price": h}, {"name": "Draw", "price": d}, {"name": away, "price": a}]}]}
                for i, (h, d, a) in enumerate(prices)]}


class FakeClient(sp.OddsClient):
    def __init__(self):
        super().__init__("key")
        self.events, self.games, self.calls = {}, {}, []

    async def odds(self, sport):
        self.calls.append(("odds", sport))
        return self.events.get(sport, [])

    async def scores(self, sport):
        self.calls.append(("scores", sport))
        return self.games.get(sport, [])


@pytest.fixture
async def book(casino):  # noqa: F811
    client = FakeClient()
    b = sp.Sportsbook(casino, client)
    await b.init()
    return b


def test_price_removes_bookmaker_margin_and_adds_ours():
    h, d, a = sp.price(event("x"))
    assert sum(1 / o for o in (h, d, a)) == pytest.approx(1 + sp.MARGIN, abs=0.01)
    assert h < a and sp.MIN_ODDS <= min(h, d, a) and max(h, d, a) <= sp.MAX_ODDS
    assert sp.price({"home_team": "A", "away_team": "B", "bookmakers": []}) is None
    assert sp.result_of(2, 1) == "home" and sp.result_of(0, 0) == "draw" and sp.result_of(1, 3) == "away"


async def test_bet_win_lose_and_void(book):
    c = book.casino
    await fund(c, 1, 1000)
    await fund(c, 2, 1000)
    book.client.events["soccer_epl"] = [event("m1"), event("m2", "Liverpool", "Everton")]
    assert await book.refresh_odds(force=True) == 2
    evs = await book.events("epl")
    assert len(evs) == 2 and evs[0]["odds"]["home"] > 1
    odds = evs[0]["odds"]
    r = await book.place(1, "m1", "home", 100)
    assert r["balance"] == 900 and r["bet"]["odds"] == odds["home"]
    await book.place(2, "m1", "draw", 50)
    with pytest.raises(GameError):
        await book.place(1, "m1", "win", 10)
    with pytest.raises(GameError):                                      # больше лимита на один матч
        await book.place(1, "m1", "away", c.cfg.max_bet)
    # коэффициенты обновились — уже сделанная ставка остаётся по старому
    book.client.events["soccer_epl"] = [event("m1", prices=((1.5, 4.0, 6.0),))]
    await book.refresh_odds(force=True)
    assert (await book.my_bets(1))[0]["odds"] == odds["home"]
    # матч сыгран 2:1 — П1 выигрывает
    sent = []

    async def notify(uid, text):
        sent.append((uid, text))
    await book.db.conn.execute("UPDATE sport_bets SET kickoff=? WHERE event_id='m1'", (time.time() - 3 * 3600,))
    book.client.games["soccer_epl"] = [{"id": "m1", "completed": True, "home_team": "Arsenal", "away_team": "Chelsea",
                                        "scores": [{"name": "Arsenal", "score": "2"}, {"name": "Chelsea", "score": "1"}]}]
    assert await book.settle_pending(notify) == 2
    win = int(100 * odds["home"])
    assert await balance(c, 1) == 900 + win and await balance(c, 2) == 950
    assert {b["status"] for b in await book.my_bets(1)} == {"won"} and (await book.my_bets(2))[0]["status"] == "lost"
    assert len(sent) == 2 and "сыграла" in sent[0][1]
    hist = await c.db.recent_bets(1, 5)
    assert hist[0]["game"] == "football" and hist[0]["win"] == win
    # повторный расчёт ничего не меняет
    assert await book.settle_pending(notify) == 0
    # матч без результата 3 дня — возврат
    await book.place(1, "m2", "away", 40)
    await book.db.conn.execute("UPDATE sport_bets SET kickoff=? WHERE event_id='m2'", (time.time() - 4 * 86400,))
    before = await balance(c, 1)
    await book.settle_pending(notify)
    assert await balance(c, 1) == before + 40 and (await book.my_bets(1))[0]["status"] == "void"


async def test_closed_before_kickoff_and_disabled(book, casino):  # noqa: F811
    await fund(casino, 1, 500)
    book.client.events["soccer_spain_la_liga"] = [event("s1", "Barcelona", "Real Madrid", kickoff=time.time() + 30)]
    await book.refresh_odds(force=True)
    assert await book.events("laliga") == []                            # до начала меньше минуты — не показываем
    with pytest.raises(GameError):
        await book.place(1, "s1", "home", 10)
    off = sp.Sportsbook(casino, sp.OddsClient(None))
    await off.init()
    assert not off.client.enabled and await off.refresh_odds() == 0
