import time
from datetime import datetime, timezone

import pytest

from app import sports as sp
from app.casino import GameError
from tests.test_casino import balance, casino, fund  # noqa: F401  (фикстура casino)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def espn_event(eid, home="Arsenal", away="Chelsea", hid="359", aid="363", kickoff=None, state="pre",
               status="STATUS_SCHEDULED", score=(None, None), ml=(-150, 280, 420), neutral=False):
    comp = {"date": iso(kickoff or time.time() + 86400), "neutralSite": neutral,
            "status": {"type": {"state": state, "completed": state == "post", "name": status}},
            "competitors": [
                {"homeAway": "home", "score": score[0], "team": {"id": hid, "displayName": home, "logo": "h.png"}},
                {"homeAway": "away", "score": score[1], "team": {"id": aid, "displayName": away, "logo": "a.png"}}]}
    if ml:
        comp["odds"] = [{"provider": {"name": "DraftKings"}, "homeTeamOdds": {"moneyLine": ml[0]},
                         "drawOdds": {"moneyLine": ml[1]}, "awayTeamOdds": {"moneyLine": ml[2]}}]
    return {"id": eid, "date": comp["date"], "competitions": [comp]}


def table(*teams):
    """Таблица ESPN: (id, сыграно, забито, пропущено)."""
    return {"children": [{"standings": {"entries": [
        {"team": {"id": tid}, "stats": [{"name": "gamesPlayed", "value": gp}, {"name": "pointsFor", "value": gf},
                                        {"name": "pointsAgainst", "value": ga}]} for tid, gp, gf, ga in teams]}}]}


class FakeEspn:
    enabled = True

    def __init__(self):
        self.boards, self.tables, self.calls = {}, {}, []

    async def scoreboard(self, code, start, end):
        self.calls.append(("scoreboard", code))
        return self.boards.get(code, [])

    async def standings(self, code):
        self.calls.append(("standings", code))
        return self.tables.get(code, {})


@pytest.fixture
async def book(casino):  # noqa: F811
    b = sp.Sportsbook(casino, FakeEspn())
    await b.init()
    return b


def test_pricing():
    assert sp.american(-200) == pytest.approx(1.5) and sp.american("+150") == pytest.approx(2.5)
    assert sp.american(50) is None and sp.american("x") is None
    h, d, a = sp.book_odds(espn_event("x")["competitions"][0])
    assert 1 + sp.MARGIN_BOOK - 0.01 <= sum(1 / o for o in (h, d, a)) <= 1.15 and h < a
    # аутсайдер: цену режем сильнее и ограничиваем сверху
    h, d, a = sp.book_odds(espn_event("z", ml=(-2000, 1200, 4000))["competitions"][0])
    assert h < 1.1 and a == d == sp.MAX_ODDS
    # своя модель: сильная команда дома — фаворит, маржа больше, цены в пределах
    strong, weak = {"att": 1.6, "def": 0.6}, {"att": 0.7, "def": 1.4}
    h, d, a = sp.model_odds(strong, weak)
    assert h < 1.5 < a <= sp.MAX_ODDS_MODEL
    assert sum(1 / o for o in (h, d, a)) >= 1 + sp.MARGIN_MODEL - 0.01
    assert sp.model_odds(strong, None) is None
    # ESPN кладёт в список линий null и линии без moneyline — разбор не падает
    comp = espn_event("y")["competitions"][0]
    comp["odds"] = [None, {"overUnder": 2.5}] + comp["odds"]
    assert sp.book_odds(comp) is not None
    assert sp.book_odds({"odds": [None, {"moneyline": None}]}) is None
    assert sum(sp.poisson_probs(1.4, 1.1)) == pytest.approx(1, abs=1e-6)
    s = sp.strengths(table(("1", 10, 25, 8), ("2", 10, 8, 22), ("3", 2, 9, 0)), 1.0)
    assert s["1"]["att"] > 1 > s["2"]["att"] and s["1"]["def"] < 1 < s["2"]["def"] and "3" not in s   # мало матчей


def test_main_time_result():
    done = lambda st, sc: espn_event("x", state="post", status=st, score=sc)["competitions"][0]
    assert sp.main_time_result(done("STATUS_FULL_TIME", ("2", "1"))) == "home"
    assert sp.main_time_result(done("STATUS_FULL_TIME", ("0", "0"))) == "draw"
    assert sp.main_time_result(done("STATUS_FINAL_AET", ("2", "1"))) == "draw"      # победа в доп. время — в основное ничья
    assert sp.main_time_result(done("STATUS_FINAL_PEN", ("1", "1"))) == "draw"
    assert sp.main_time_result(espn_event("x", status="STATUS_POSTPONED")["competitions"][0]) == "void"
    assert sp.main_time_result(espn_event("x")["competitions"][0]) is None


async def test_bet_win_lose_void(book):
    c, espn = book.casino, book.client
    await fund(c, 1, 1000)
    await fund(c, 2, 1000)
    espn.boards["eng.1"] = [espn_event("1"), espn_event("2", "Liverpool", "Everton", "364", "368", ml=None)]
    espn.tables["eng.1"] = table(("359", 8, 18, 6), ("363", 8, 12, 10), ("364", 8, 20, 5), ("368", 8, 6, 15))
    assert await book.refresh_odds(force=True) == 2                    # второй матч — по своей модели
    evs = {e["id"]: e for e in await book.events("epl")}
    assert set(evs) == {"espn:1", "espn:2"} and evs["espn:2"]["odds"]["home"] < evs["espn:2"]["odds"]["away"]
    odds = evs["espn:1"]["odds"]
    r = await book.place(1, "espn:1", "home", 100)
    assert r["balance"] == 900 and r["bet"]["odds"] == odds["home"]
    await book.place(2, "espn:1", "draw", 50)
    with pytest.raises(GameError):
        await book.place(1, "espn:1", "win", 10)
    with pytest.raises(GameError):                                     # по модели лимит на матч меньше
        await book.place(1, "espn:2", "home", int(c.cfg.max_bet * sp.MODEL_STAKE_SHARE) + 1)
    # матч сыгран 2:1 — П1
    sent = []

    async def notify(uid, text):
        sent.append((uid, text))
    await book.db.conn.execute("UPDATE sport_bets SET kickoff=? WHERE event_id='espn:1'", (time.time() - 3 * 3600,))
    espn.boards["eng.1"] = [espn_event("1", state="post", status="STATUS_FULL_TIME", score=("2", "1"))]
    assert await book.settle_pending(notify) == 2
    win = int(100 * odds["home"])
    assert await balance(c, 1) == 900 + win and await balance(c, 2) == 950
    assert (await book.my_bets(1))[0]["status"] == "won" and (await book.my_bets(2))[0]["status"] == "lost"
    assert len(sent) == 2 and "сыграла" in sent[0][1] and "2:1" in sent[0][1]
    assert (await c.db.recent_bets(1, 5))[0]["game"] == "football"
    assert await book.settle_pending(notify) == 0                      # повторно ничего не меняется
    # перенос — возврат
    await book.place(1, "espn:2", "away", 40)
    before = await balance(c, 1)
    await book.db.conn.execute("UPDATE sport_bets SET kickoff=? WHERE event_id='espn:2'", (time.time() - 3 * 3600,))
    await book.db.kv_set("sports:scores_at:epl", "0")
    espn.boards["eng.1"] = [espn_event("2", status="STATUS_POSTPONED", ml=None)]
    await book.settle_pending(notify)
    assert await balance(c, 1) == before + 40 and (await book.my_bets(1))[0]["status"] == "void"


async def test_closed_unpriced_and_stale(book, casino):  # noqa: F811
    await fund(casino, 1, 500)
    espn = book.client
    espn.boards["esp.1"] = [espn_event("s1", "Barcelona", "Sevilla", kickoff=time.time() + 30),        # почти началось
                            espn_event("s2", "Unknown", "Nobody", "1", "2", ml=None)]                # нет ни линии, ни данных
    espn.boards["uefa.nations"] = [espn_event("n1", "Spain", "Malta", "s", "m", ml=None, neutral=True)]
    await book.refresh_odds(force=True)
    assert await book.events() == []
    with pytest.raises(GameError):
        await book.place(1, "espn:s1", "home", 10)
    # устаревшие коэффициенты — ставки не принимаем
    espn.boards["esp.1"] = [espn_event("s3", "Barcelona", "Sevilla")]
    await book.refresh_odds(force=True)
    await book.db.conn.execute("UPDATE sport_events SET odds_at=? WHERE id='espn:s3'", (time.time() - sp.ODDS_MAX_AGE - 1,))
    with pytest.raises(GameError):
        await book.place(1, "espn:s3", "home", 10)


async def test_league_icons(casino):  # noqa: F811
    from io import BytesIO
    from types import SimpleNamespace

    from PIL import Image

    def png():
        buf = BytesIO()
        Image.new("RGBA", (64, 64), (200, 30, 30, 255)).save(buf, "PNG")
        return buf.getvalue()

    class Bot:
        async def get_sticker_set(self, name):
            assert name == sp.ICON_SET
            return SimpleNamespace(stickers=[SimpleNamespace(custom_emoji_id=f"e{i}", emoji=em, file_id=f"f{i}",
                                                             is_video=False, thumbnail=None)
                                             for i, em in enumerate(["⚽", "🇪🇸", "🇫🇷", "⭐", "🇬🇧"])])

        async def download(self, file_id):
            return BytesIO(png())

    icons = sp.LeagueIcons(casino.db)
    bot = Bot()
    m = await icons.mapping(bot)
    assert m == {"epl": "e4", "laliga": "e1", "ligue1": "e2", "ucl": "e3"}         # Лига наций — логотип турнира
    assert await icons.assign(bot, "unl", 1) == "e0" and (await icons.mapping(bot))["unl"] == "e0"
    with pytest.raises(ValueError):
        await icons.assign(bot, "unl", 99)
    sheet = await icons.sheet(bot)
    assert sheet[:8] == b"\x89PNG\r\n\x1a\n"


def test_league_logo_prefers_dark():
    c = sp.EspnClient()
    c._take_logo("uefa.nations", {"leagues": [{"logos": [None, {"href": "https://x/l.png", "rel": ["full", "default"]},
                                                         {"href": "https://x/d.png", "rel": ["full", "dark"]}]}]})
    assert c.logos["uefa.nations"] == "https://x/d.png"


def test_favourite_never_priced_above_model():
    book = sp.Sportsbook.__new__(sp.Sportsbook)
    book.power = {"359": {"att": 1.8, "def": 0.5}, "363": {"att": 0.6, "def": 1.6}}
    comp = espn_event("f", ml=(110, 250, 240))["competitions"][0]          # линия считает матч равным
    odds, source = book.price(comp)
    model = sp.model_odds(book.power["359"], book.power["363"])
    assert source == "book" and odds[0] == model[0] < sp.book_odds(comp)[0]
    assert sp.book_odds(espn_event("g", ml=(-400, 450, 900))["competitions"][0])[0] < 1.2   # рынок 1.25
