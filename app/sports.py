"""Ставки на футбол без платных API: матчи, счета и линии — из открытого JSON ESPN.

Коэффициенты:
  1) если ESPN отдаёт линию букмекера (американские moneyline) — переводим в вероятности, убираем маржу
     букмекера и добавляем нашу (MARGIN_BOOK);
  2) иначе — своя модель Пуассона по турнирным таблицам (атака/оборона команд, преимущество поля),
     с маржой выше (MARGIN_MODEL) и меньшим лимитом на матч;
  3) нет ни линии, ни данных о командах — матч не показываем (лучше пропустить, чем ошибиться в цене).

Ставка — на исход основного времени (П1 / Х / П2), коэффициент фиксируется в момент ставки. Если матч дошёл
до дополнительного времени или пенальти, основное время закончилось вничью — ставки рассчитываются как «Х».
Перенос/отмена или нет результата 3 дня — возврат.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import aiohttp

from . import money
from .casino import Casino, GameError

log = logging.getLogger(__name__)

ESPN = "https://site.api.espn.com/apis"
# ключ лиги: (код ESPN, название, значок)
LEAGUES: dict[str, tuple[str, str, str]] = {
    "epl": ("eng.1", "АПЛ", "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
    "laliga": ("esp.1", "Ла Лига", "🇪🇸"),
    "ligue1": ("fra.1", "Лига 1", "🇫🇷"),
    "ucl": ("uefa.champions", "Лига чемпионов", "🏆"),
    "unl": ("uefa.nations", "Лига наций", "🌍"),
}
# таблицы, из которых модель берёт силу клубов (и для еврокубков), и поправка на уровень лиги
STRENGTH_LEAGUES = {"eng.1": 1.0, "esp.1": 0.97, "ger.1": 0.95, "ita.1": 0.95, "fra.1": 0.9, "por.1": 0.82,
                    "ned.1": 0.8}
PICKS = ("home", "draw", "away")
MARGIN_BOOK = 0.08          # маржа поверх линии букмекера
MARGIN_MODEL = 0.12         # маржа поверх своей модели (она грубее — запас больше)
MIN_ODDS, MAX_ODDS, MAX_ODDS_MODEL = 1.03, 25.0, 12.0
MODEL_STAKE_SHARE = 0.2     # по модели на один матч — не больше 20% максимальной ставки
CLOSE_BEFORE = 60           # приём ставок закрывается за минуту до начала
ODDS_MAX_AGE = 3 * 3600     # коэффициенты старше — ставки не принимаем
FIXTURES_EVERY = 30 * 60    # расписание и линии
STANDINGS_EVERY = 6 * 3600  # таблицы для модели
SCORES_EVERY = 15 * 60      # счёт по лиге, пока есть нерассчитанные ставки
SETTLE_AFTER = 110 * 60     # матч идёт ~2 часа — раньше счёт не спрашиваем
VOID_AFTER = 3 * 24 * 3600  # результата нет через 3 дня после начала — ставки возвращаем
AHEAD_DAYS = 8              # сколько дней вперёд показываем матчи
HOME_GOALS, AWAY_GOALS = 1.5, 1.15     # средние голы хозяев и гостей в топ-лигах
SHRINK = 6                  # «виртуальных» средних матчей в оценке силы — меньше шума в начале сезона
MIN_GAMES = 3               # меньше сыгранных матчей — модель не верит таблице
EXTRA_TIME = ("AET", "PEN", "EXTRA", "SHOOTOUT")   # статусы ESPN: доп. время / пенальти
VOID_STATUS = ("POSTPONED", "CANCELED", "CANCELLED", "ABANDONED", "SUSPENDED", "FORFEIT")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sport_events (
    id         TEXT PRIMARY KEY,
    league     TEXT NOT NULL,
    home       TEXT NOT NULL,
    away       TEXT NOT NULL,
    kickoff    REAL NOT NULL,
    odds_home  REAL, odds_draw REAL, odds_away REAL,
    odds_at    REAL,
    status     TEXT NOT NULL DEFAULT 'open',      -- open | done | void
    home_score INTEGER, away_score INTEGER,
    settled_at REAL
);
CREATE TABLE IF NOT EXISTS sport_bets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    event_id   TEXT NOT NULL,
    pick       TEXT NOT NULL,                      -- home | draw | away
    odds       REAL NOT NULL,
    amount     INTEGER NOT NULL,
    cur        TEXT NOT NULL DEFAULT 'stars',
    kickoff    REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',      -- open | won | lost | void
    payout     INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    settled_at REAL
);
CREATE INDEX IF NOT EXISTS sport_bets_open ON sport_bets(status, event_id);
CREATE INDEX IF NOT EXISTS sport_events_league ON sport_events(league, kickoff);
"""
EXTRA_COLUMNS = {"source": "TEXT NOT NULL DEFAULT 'book'", "home_logo": "TEXT", "away_logo": "TEXT"}


# ---------- коэффициенты ----------

def american(ml: Any) -> float | None:
    """Американская линия (+150 / -200 / "+150") → десятичный коэффициент."""
    try:
        v = float(str(ml).replace("+", "").strip())
    except (TypeError, ValueError):
        return None
    if v >= 100:
        return 1 + v / 100
    if v <= -100:
        return 1 + 100 / -v
    return None


def with_margin(probs: tuple[float, float, float], margin: float, max_odds: float) -> tuple[float, float, float]:
    total = sum(probs)
    out = []
    for p in probs:
        odds = math.floor(100 / (p / total * (1 + margin))) / 100     # вниз — маржа не меньше заявленной
        out.append(min(max_odds, max(MIN_ODDS, odds)))
    return out[0], out[1], out[2]


def book_odds(comp: dict[str, Any]) -> tuple[float, float, float] | None:
    """Линия букмекера из события ESPN (разные форматы ответа), с нашей маржой."""
    for o in comp.get("odds") or []:
        if not isinstance(o, dict):          # ESPN иногда кладёт в список линий null
            continue
        h = american((o.get("homeTeamOdds") or {}).get("moneyLine"))
        a = american((o.get("awayTeamOdds") or {}).get("moneyLine"))
        d = american((o.get("drawOdds") or {}).get("moneyLine"))
        if not (h and a and d):
            ml = o.get("moneyline") if isinstance(o.get("moneyline"), dict) else {}

            def pick(side: str) -> float | None:
                line = ml.get(side) if isinstance(ml.get(side), dict) else {}
                val = line.get("close") or line.get("open")
                return american(val.get("odds")) if isinstance(val, dict) else None
            h, a, d = pick("home"), pick("away"), pick("draw")
        if h and a and d:
            return with_margin((1 / h, 1 / d, 1 / a), MARGIN_BOOK, MAX_ODDS)
    return None


def poisson_probs(lh: float, la: float, up_to: int = 10) -> tuple[float, float, float]:
    ph = [math.exp(-lh) * lh ** k / math.factorial(k) for k in range(up_to + 1)]
    pa = [math.exp(-la) * la ** k / math.factorial(k) for k in range(up_to + 1)]
    home = sum(ph[i] * pa[j] for i in range(up_to + 1) for j in range(up_to + 1) if i > j)
    draw = sum(ph[i] * pa[i] for i in range(up_to + 1))
    return home, draw, max(0.0, 1 - home - draw)


def model_odds(home: dict[str, float] | None, away: dict[str, float] | None,
               neutral: bool = False) -> tuple[float, float, float] | None:
    """Своя цена по силе команд (атака/оборона относительно лиги, с поправкой на уровень лиги)."""
    if not home or not away:
        return None
    hg, ag = (HOME_GOALS, AWAY_GOALS) if not neutral else ((HOME_GOALS + AWAY_GOALS) / 2,) * 2
    lh = hg * home["att"] * away["def"]
    la = ag * away["att"] * home["def"]
    return with_margin(poisson_probs(lh, la), MARGIN_MODEL, MAX_ODDS_MODEL)


def strengths(standings: dict[str, Any], level: float) -> dict[str, dict[str, float]]:
    """Сила клубов из таблицы ESPN: атака и оборона относительно среднего по лиге, со сжатием к среднему."""
    rows = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for e in (node.get("standings") or {}).get("entries") or []:
                rows.append(e)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(standings)
    teams = {}
    for e in rows:
        st = {s.get("name"): s.get("value") for s in e.get("stats") or []}
        gp = st.get("gamesPlayed")
        gf = st.get("pointsFor", st.get("goalsFor"))
        ga = st.get("pointsAgainst", st.get("goalsAgainst"))
        tid = str((e.get("team") or {}).get("id") or "")
        if tid and gp and gf is not None and ga is not None:
            teams[tid] = (float(gp), float(gf), float(ga))
    total_gp = sum(v[0] for v in teams.values())
    if not total_gp:
        return {}
    avg = sum(v[1] for v in teams.values()) / total_gp            # голов за матч на команду
    out = {}
    for tid, (gp, gf, ga) in teams.items():
        if gp < MIN_GAMES or avg <= 0:
            continue
        att = (gf + SHRINK * avg) / (gp + SHRINK) / avg
        dfn = (ga + SHRINK * avg) / (gp + SHRINK) / avg
        out[tid] = {"att": att * level, "def": dfn / level}
    return out


def main_time_result(comp: dict[str, Any]) -> str | None:
    """Исход основного времени завершённого матча ESPN; None — ещё не сыгран; 'void' — перенос/отмена."""
    st = ((comp.get("status") or {}).get("type") or {})
    name = str(st.get("name") or "").upper()
    if any(v in name for v in VOID_STATUS):
        return "void"
    if not st.get("completed") or st.get("state") != "post":
        return None
    if any(v in name for v in EXTRA_TIME):
        return "draw"                  # дошли до доп. времени — значит основное закончилось вничью
    sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
    try:
        hs, as_ = int(sides["home"]["score"]), int(sides["away"]["score"])
    except (KeyError, TypeError, ValueError):
        return None
    return result_of(hs, as_)


def result_of(home_score: int, away_score: int) -> str:
    return "home" if home_score > away_score else "away" if away_score > home_score else "draw"


def scores_of(comp: dict[str, Any]) -> tuple[int | None, int | None]:
    sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
    try:
        return int(sides["home"]["score"]), int(sides["away"]["score"])
    except (KeyError, TypeError, ValueError):
        return None, None


class EspnClient:
    """Открытый JSON ESPN — без ключа и лимитов."""

    enabled = True

    async def _get(self, url: str, **params: Any) -> dict[str, Any]:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                                         headers={"User-Agent": "Mozilla/5.0"}) as s:
            async with s.get(url, params=params) as r:
                if r.status != 200:
                    raise RuntimeError(f"ESPN: HTTP {r.status}")
                return await r.json(content_type=None)

    async def scoreboard(self, code: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Матчи за период: ESPN отвечает 400 на диапазон с limit, поэтому — по одному дню (dates=YYYYMMDD)."""
        events: dict[str, dict[str, Any]] = {}
        day, errors = start, []
        while day.date() <= end.date():
            try:
                data = await self._get(f"{ESPN}/site/v2/sports/soccer/{code}/scoreboard", dates=f"{day:%Y%m%d}")
                for ev in data.get("events") or []:
                    events[str(ev.get("id"))] = ev
            except Exception as e:
                errors.append(e)
            day += timedelta(days=1)
        if errors and not events:
            raise errors[0]
        return list(events.values())

    async def standings(self, code: str) -> dict[str, Any]:
        return await self._get(f"{ESPN}/v2/sports/soccer/{code}/standings")


Notify = Callable[[int, str], Awaitable[None]]


class Sportsbook:
    def __init__(self, casino: Casino, client: Any = None):
        self.casino, self.db = casino, casino.db
        self.client = client or EspnClient()
        self.power: dict[str, dict[str, float]] = {}     # сила клубов: id команды ESPN → att/def
        self.last_counts: dict[str, Any] = {}             # итог последнего обновления по лигам (для /sports_refresh)

    async def init(self) -> None:
        await self.db.conn.executescript(SCHEMA)
        await self.db._add_columns("sport_events", EXTRA_COLUMNS)

    async def _due(self, key: str, every: float) -> bool:
        return time.time() - float(await self.db.kv_get(key) or 0) >= every

    # ---------- данные ESPN ----------

    async def refresh_strength(self, force: bool = False) -> int:
        if not force and self.power and not await self._due("sports:standings_at", STANDINGS_EVERY):
            return len(self.power)
        power: dict[str, dict[str, float]] = {}
        for code, level in STRENGTH_LEAGUES.items():
            try:
                power.update(strengths(await self.client.standings(code), level))
            except Exception as e:
                log.warning("Таблица %s не получена: %s", code, e)
        if power:
            self.power = power
            await self.db.kv_set("sports:standings_at", str(time.time()))
        return len(self.power)

    def price(self, comp: dict[str, Any], neutral: bool = False) -> tuple[tuple[float, float, float] | None, str]:
        odds = book_odds(comp)
        if odds:
            return odds, "book"
        sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        tid = lambda side: str(((sides.get(side) or {}).get("team") or {}).get("id") or "")
        return model_odds(self.power.get(tid("home")), self.power.get(tid("away")), neutral), "model"

    async def refresh_odds(self, force: bool = False) -> int:
        """Расписание и коэффициенты на AHEAD_DAYS вперёд по всем лигам. Возвращает число матчей с ценой."""
        if not force and not await self._due("sports:fixtures_at", FIXTURES_EVERY):
            return 0
        await self.refresh_strength()
        now = time.time()
        today = datetime.now(timezone.utc)
        n = 0
        per_league: dict[str, Any] = {}
        for league, (code, _, _) in LEAGUES.items():
            try:
                events = await self.client.scoreboard(code, today, today + timedelta(days=AHEAD_DAYS))
            except Exception as e:
                log.warning("Матчи %s не получены: %s", league, e)
                per_league[league] = f"ошибка: {e}"
                continue
            per_league[league] = 0
            async with self.db.tx() as c:
                for ev in events:
                    comp = (ev.get("competitions") or [{}])[0]
                    state = ((comp.get("status") or {}).get("type") or {}).get("state")
                    kickoff = _ts(comp.get("date") or ev.get("date"))
                    sides = {x.get("homeAway"): x for x in comp.get("competitors") or []}
                    if state != "pre" or not kickoff or kickoff <= now or "home" not in sides or "away" not in sides:
                        continue
                    try:
                        odds, source = self.price(comp, bool(comp.get("neutralSite")))
                    except Exception as e:               # неожиданный формат одного матча — пропускаем только его
                        log.warning("Матч %s %s не разобран: %s", league, ev.get("id"), e)
                        continue
                    if odds is None:
                        continue
                    team = lambda s: sides[s].get("team") or {}
                    await c.execute(
                        "INSERT INTO sport_events(id, league, home, away, kickoff, odds_home, odds_draw, odds_away, "
                        "odds_at, source, home_logo, away_logo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET kickoff=excluded.kickoff, odds_home=excluded.odds_home, "
                        "odds_draw=excluded.odds_draw, odds_away=excluded.odds_away, odds_at=excluded.odds_at, "
                        "source=excluded.source WHERE status='open'",
                        (f"espn:{ev['id']}", league, team("home").get("displayName") or "?",
                         team("away").get("displayName") or "?", kickoff, *odds, now, source,
                         team("home").get("logo"), team("away").get("logo")))
                    n += 1
                    per_league[league] += 1
        if n:                                   # пусто (ESPN недоступен) — попробуем снова в следующем цикле
            await self.db.kv_set("sports:fixtures_at", str(now))
        self.last_counts = per_league
        log.info("Футбол: %s матчей с коэффициентами %s", n, per_league)
        return n

    async def settle_pending(self, notify: Notify | None = None) -> int:
        """Рассчитывает ставки на сыгранные матчи, возвращает ставки на перенесённые/отменённые. Число рассчитанных."""
        now = time.time()
        rows = await self.db.all(
            "SELECT e.league, MIN(b.kickoff) k0, MAX(b.kickoff) k1 FROM sport_bets b JOIN sport_events e "
            "ON e.id=b.event_id WHERE b.status='open' AND b.kickoff < ? GROUP BY e.league", now - SETTLE_AFTER)
        done = 0
        for row in rows:
            league = row["league"]
            key = f"sports:scores_at:{league}"
            if league not in LEAGUES or not await self._due(key, SCORES_EVERY):
                continue
            start = datetime.fromtimestamp(row["k0"], timezone.utc) - timedelta(days=1)
            end = datetime.fromtimestamp(row["k1"], timezone.utc) + timedelta(days=1)
            try:
                events = await self.client.scoreboard(LEAGUES[league][0], start, end)
            except Exception as e:
                log.warning("Счёт %s не получен: %s", league, e)
                continue
            await self.db.kv_set(key, str(now))
            for ev in events:
                comp = (ev.get("competitions") or [{}])[0]
                res = main_time_result(comp)
                if res is None:
                    continue
                eid = f"espn:{ev['id']}"
                if res == "void":
                    for bet in await self.db.all("SELECT * FROM sport_bets WHERE event_id=? AND status='open'", eid):
                        await self._void(bet, notify)
                        done += 1
                    continue
                hs, as_ = scores_of(comp)
                done += await self.settle_event(eid, hs, as_, notify, result=res)
        # без результата слишком долго — возврат
        for bet in await self.db.all("SELECT * FROM sport_bets WHERE status='open' AND kickoff < ?", now - VOID_AFTER):
            await self._void(bet, notify)
            done += 1
        return done

    async def settle_event(self, event_id: str, home_score: int | None, away_score: int | None,
                           notify: Notify | None = None, result: str | None = None) -> int:
        ev = await self.db.one("SELECT * FROM sport_events WHERE id=?", event_id)
        if not ev:
            return 0
        res = result or result_of(home_score or 0, away_score or 0)
        bets = await self.db.all("SELECT * FROM sport_bets WHERE event_id=? AND status='open'", event_id)
        now = time.time()
        msgs = []
        score = f"{home_score}:{away_score}" if home_score is not None else ""
        async with self.db.tx() as c:
            await c.execute("UPDATE sport_events SET status='done', home_score=?, away_score=?, settled_at=? WHERE id=?",
                            (home_score, away_score, now, event_id))
            for b in bets:
                won = b["pick"] == res
                payout = math.floor(b["amount"] * b["odds"]) if won else 0
                detail = {"match": f"{ev['home']} — {ev['away']}", "pick": b["pick"], "odds": b["odds"],
                          "score": score, "result": res, "league": ev["league"]}
                await self.casino._settle(c, b["user_id"], "football", b["amount"], payout, detail, b["cur"])
                await c.execute("UPDATE sport_bets SET status=?, payout=?, settled_at=? WHERE id=?",
                                ("won" if won else "lost", payout, now, b["id"]))
                line = f"{ev['home']} {score} {ev['away']}".replace("  ", " — ")
                msgs.append((b["user_id"], f"✅ Ставка сыграла: {line}\nВыигрыш {money.fmt(payout, b['cur'])}" if won
                             else f"❌ Ставка не сыграла: {line}"))
        if notify:
            for uid, text in msgs:
                try:
                    await notify(uid, text)
                except Exception:
                    pass
        return len(bets)

    async def _void(self, bet: dict[str, Any], notify: Notify | None) -> None:
        async with self.db.tx() as c:
            res = await c.execute("UPDATE sport_bets SET status='void', payout=?, settled_at=? WHERE id=? AND status='open'",
                                  (bet["amount"], time.time(), bet["id"]))
            if res.rowcount != 1:
                return
            await self.db.change_balance(c, bet["user_id"], bet["amount"], "refund", "football", bet["cur"])
            await c.execute("UPDATE sport_events SET status='void' WHERE id=? AND status='open'", (bet["event_id"],))
        if notify:
            try:
                await notify(bet["user_id"], f"↩️ Матч не состоялся — ставка {money.fmt(bet['amount'], bet['cur'])} возвращена")
            except Exception:
                pass

    # ---------- для приложения ----------

    async def events(self, league: str | None = None) -> list[dict[str, Any]]:
        now = time.time()
        sql = ("SELECT * FROM sport_events WHERE status='open' AND kickoff > ? AND odds_at > ? "
               + ("AND league=? " if league else "") + "ORDER BY kickoff LIMIT 80")
        args = (now + CLOSE_BEFORE, now - ODDS_MAX_AGE) + ((league,) if league else ())
        return [{"id": e["id"], "league": e["league"], "home": e["home"], "away": e["away"], "kickoff": e["kickoff"],
                 "home_logo": e["home_logo"], "away_logo": e["away_logo"],
                 "odds": {"home": e["odds_home"], "draw": e["odds_draw"], "away": e["odds_away"]}}
                for e in await self.db.all(sql, *args)]

    async def my_bets(self, user_id: int) -> list[dict[str, Any]]:
        rows = await self.db.all(
            "SELECT b.*, e.home, e.away, e.league, e.home_score, e.away_score FROM sport_bets b "
            "JOIN sport_events e ON e.id=b.event_id WHERE b.user_id=? ORDER BY b.id DESC LIMIT 30", user_id)
        return [{k: r[k] for k in ("id", "event_id", "pick", "odds", "amount", "cur", "kickoff", "status", "payout",
                                   "home", "away", "league", "home_score", "away_score", "created_at")} for r in rows]

    async def place(self, user_id: int, event_id: Any, pick: Any, amount: Any, cur: Any = money.STARS) -> dict:
        if pick not in PICKS:
            raise GameError("Выберите исход: П1, Х или П2")
        cur = self.casino._cur(cur)
        amount = self.casino._check_bet(amount, cur)
        ev = await self.db.one("SELECT * FROM sport_events WHERE id=?", str(event_id))
        now = time.time()
        if not ev or ev["status"] != "open" or ev["kickoff"] <= now + CLOSE_BEFORE:
            raise GameError("Приём ставок на этот матч закрыт")
        if not ev["odds_at"] or now - ev["odds_at"] > ODDS_MAX_AGE:
            raise GameError("Коэффициенты обновляются — попробуйте чуть позже")
        odds = ev[f"odds_{pick}"]
        # лимит на один матч суммарно (чтобы не обходили дроблением); по своей модели — меньше
        hi = self.casino.cfg.max_bet if cur == money.STARS else money.TON_MAX_BET
        if ev["source"] == "model":
            hi = max(self.casino.cfg.min_bet if cur == money.STARS else money.TON_MIN_BET, int(hi * MODEL_STAKE_SHARE))
        staked = (await self.db.one("SELECT COALESCE(SUM(amount), 0) s FROM sport_bets WHERE user_id=? AND event_id=? "
                                    "AND cur=? AND status='open'", user_id, ev["id"], cur))["s"]
        if staked + amount > hi:
            raise GameError(f"На один матч можно поставить не больше {money.fmt(hi, cur)}")
        async with self.db.tx() as c:
            balance = await self.casino._take(c, user_id, amount, "football", cur)
            res = await c.execute(
                "INSERT INTO sport_bets(user_id, event_id, pick, odds, amount, cur, kickoff, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)", (user_id, ev["id"], pick, odds, amount, cur, ev["kickoff"], now))
            bet_id = res.lastrowid
        return {"bet": {"id": bet_id, "event_id": ev["id"], "pick": pick, "odds": odds, "amount": amount,
                        "home": ev["home"], "away": ev["away"], "possible": math.floor(amount * odds)},
                "balance": balance, "cur": cur}


def _ts(iso: Any) -> float | None:
    """Время ESPN «2026-10-03T14:00Z» → unix."""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---------- значки лиг: премиум-эмодзи из набора Telegram ----------

ICON_SET = "europeHDSofascout"
ICON_KEY = "sports:icons"            # {лига: custom_emoji_id}, назначенные вручную (/league_icons epl 5)
# подсказки для автоподбора по «базовому» эмодзи стикера, если вручную не назначено
ICON_HINTS = {"epl": ["🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f", "🇬🇧", "🦁"],
              "laliga": ["🇪🇸"], "ligue1": ["🇫🇷"], "ucl": ["⭐", "🌟", "🏆", "✨"], "unl": ["🌍", "🇪🇺", "🌐"]}


class LeagueIcons:
    def __init__(self, db: Any):
        self.db = db
        self._set: list[Any] | None = None

    async def stickers(self, bot: Any) -> list[Any]:
        if self._set is None:
            self._set = list((await bot.get_sticker_set(ICON_SET)).stickers)
        return self._set

    async def mapping(self, bot: Any) -> dict[str, str]:
        """Лига → custom_emoji_id: назначенные вручную, остальные — автоподбор по эмодзи-подсказке."""
        manual = json.loads(await self.db.kv_get(ICON_KEY) or "{}")
        out = dict(manual)
        try:
            stickers = await self.stickers(bot)
        except Exception as e:
            log.warning("Набор эмодзи %s не получен: %s", ICON_SET, e)
            return out
        used = set(out.values())
        for league, hints in ICON_HINTS.items():
            if league in out:
                continue
            for st in stickers:
                if st.custom_emoji_id not in used and (st.emoji or "") in hints:
                    out[league] = st.custom_emoji_id
                    used.add(st.custom_emoji_id)
                    break
        return out

    async def assign(self, bot: Any, league: str, number: int) -> str:
        stickers = await self.stickers(bot)
        if league not in LEAGUES:
            raise ValueError("лиги: " + ", ".join(LEAGUES))
        if not 1 <= number <= len(stickers):
            raise ValueError(f"номер от 1 до {len(stickers)}")
        manual = json.loads(await self.db.kv_get(ICON_KEY) or "{}")
        manual[league] = stickers[number - 1].custom_emoji_id
        await self.db.kv_set(ICON_KEY, json.dumps(manual))
        return manual[league]

    @staticmethod
    async def image(bot: Any, sticker: Any) -> bytes | None:
        """Картинка стикера: TGS — первый кадр, видео — превью, WebP — как есть."""
        from .nftimg import render_tgs
        file_id = sticker.file_id
        if getattr(sticker, "is_video", False) and getattr(sticker, "thumbnail", None):
            file_id = sticker.thumbnail.file_id
        buf = await bot.download(file_id)
        data = buf.read() if buf else None
        if data and data[:2] == b"\x1f\x8b":
            return await asyncio.to_thread(render_tgs, data)
        return data

    async def sheet(self, bot: Any) -> bytes:
        """Весь набор сеткой с номерами — чтобы админ выбрал значки (/league_icons)."""
        from io import BytesIO

        from PIL import Image, ImageDraw
        stickers = await self.stickers(bot)
        cols, cell = 8, 128
        rows = (len(stickers) + cols - 1) // cols
        out = Image.new("RGB", (cols * cell, rows * cell), (24, 26, 33))
        draw = ImageDraw.Draw(out)
        for i, st in enumerate(stickers):
            x, y = (i % cols) * cell, (i // cols) * cell
            try:
                data = await self.image(bot, st)
                im = Image.open(BytesIO(data)).convert("RGBA")
                im.thumbnail((cell - 24, cell - 24))
                out.paste(im, (x + (cell - im.width) // 2, y + 18 + (cell - 24 - im.height) // 2), im)
            except Exception:
                pass
            draw.rectangle((x, y, x + 30, y + 18), fill=(245, 185, 60))
            draw.text((x + 4, y + 3), str(i + 1), fill=(0, 0, 0))
        buf = BytesIO()
        out.save(buf, "PNG")
        return buf.getvalue()
