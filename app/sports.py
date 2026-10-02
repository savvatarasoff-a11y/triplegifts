"""Ставки на футбол: матчи и коэффициенты букмекеров — The Odds API, наша маржа поверх, расчёт по итоговому счёту.

Игрок ставит на исход основного времени (П1 / Х / П2) до начала матча; коэффициент фиксируется в момент ставки.
После матча фоновый цикл берёт счёт и рассчитывает ставки. Матч без результата через 3 дня после начала — возврат.
"""
from __future__ import annotations

import json
import logging
import math
import os
import statistics
import time
from typing import Any, Awaitable, Callable

import aiohttp

from . import money
from .casino import Casino, GameError

log = logging.getLogger(__name__)

API = "https://api.the-odds-api.com/v4"
# ключ лиги: (ключ The Odds API, название, значок)
LEAGUES: dict[str, tuple[str, str, str]] = {
    "epl": ("soccer_epl", "АПЛ", "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
    "laliga": ("soccer_spain_la_liga", "Ла Лига", "🇪🇸"),
    "ligue1": ("soccer_france_ligue_one", "Лига 1", "🇫🇷"),
    "ucl": ("soccer_uefa_champs_league", "Лига чемпионов", "🏆"),
    "unl": ("soccer_uefa_nations_league", "Лига наций", "🌍"),
}
PICKS = ("home", "draw", "away")
MARGIN = 0.08               # наша маржа: сумма обратных коэффициентов = 1.08
MIN_ODDS, MAX_ODDS = 1.03, 25.0
CLOSE_BEFORE = 60           # приём ставок закрывается за минуту до начала
ODDS_MAX_AGE = 24 * 3600    # по устаревшим коэффициентам не принимаем
ODDS_EVERY = float(os.getenv("ODDS_REFRESH_HOURS", "8")) * 3600   # бесплатный тариф — 500 запросов в месяц
SCORES_EVERY = 3 * 3600     # счёт по лиге запрашиваем не чаще, пока есть нерассчитанные ставки
SETTLE_AFTER = 110 * 60     # матч идёт ~2 часа — раньше счёт не спрашиваем
VOID_AFTER = 3 * 24 * 3600  # результата нет через 3 дня после начала — ставки возвращаем

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


def price(event: dict[str, Any]) -> tuple[float, float, float] | None:
    """Наши коэффициенты П1/Х/П2: медиана по букмекерам → вероятности без их маржи → наша маржа."""
    home, away = event.get("home_team"), event.get("away_team")
    seen: dict[str, list[float]] = {"home": [], "draw": [], "away": []}
    for bm in event.get("bookmakers") or []:
        for market in bm.get("markets") or []:
            if market.get("key") != "h2h":
                continue
            for o in market.get("outcomes") or []:
                key = "home" if o.get("name") == home else "away" if o.get("name") == away else \
                    "draw" if o.get("name") == "Draw" else None
                p = o.get("price")
                if key and isinstance(p, (int, float)) and p > 1:
                    seen[key].append(float(p))
    if not all(seen.values()):
        return None
    implied = {k: 1 / statistics.median(v) for k, v in seen.items()}
    total = sum(implied.values())
    out = []
    for k in PICKS:
        fair = implied[k] / total
        odds = math.floor(100 / (fair * (1 + MARGIN))) / 100       # вниз — маржа не меньше заявленной
        out.append(min(MAX_ODDS, max(MIN_ODDS, odds)))
    return out[0], out[1], out[2]


def result_of(home_score: int, away_score: int) -> str:
    return "home" if home_score > away_score else "away" if away_score > home_score else "draw"


class OddsClient:
    """The Odds API. Без ключа (ODDS_API_KEY) раздел футбола выключен."""

    def __init__(self, api_key: str | None):
        self.api_key = (api_key or "").strip()
        self.remaining: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _get(self, path: str, **params: Any) -> list[dict[str, Any]]:
        params = {"apiKey": self.api_key, "dateFormat": "unix", **params}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(f"{API}{path}", params=params) as r:
                self.remaining = r.headers.get("x-requests-remaining", self.remaining)
                if r.status != 200:
                    raise RuntimeError(f"The Odds API: HTTP {r.status}")
                return await r.json()

    async def odds(self, sport: str) -> list[dict[str, Any]]:
        return await self._get(f"/sports/{sport}/odds", regions="eu", markets="h2h", oddsFormat="decimal")

    async def scores(self, sport: str) -> list[dict[str, Any]]:
        return await self._get(f"/sports/{sport}/scores", daysFrom=3)


Notify = Callable[[int, str], Awaitable[None]]


class Sportsbook:
    def __init__(self, casino: Casino, client: OddsClient):
        self.casino, self.db, self.client = casino, casino.db, client

    async def init(self) -> None:
        await self.db.conn.executescript(SCHEMA)

    # ---------- данные с The Odds API ----------

    async def _due(self, key: str, every: float) -> bool:
        last = float(await self.db.kv_get(key) or 0)
        return time.time() - last >= every

    async def refresh_odds(self, force: bool = False) -> int:
        """Матчи и коэффициенты по всем лигам (не чаще ODDS_EVERY на лигу). Возвращает число обновлённых матчей."""
        if not self.client.enabled:
            return 0
        n = 0
        for league, (sport, _, _) in LEAGUES.items():
            key = f"sports:odds_at:{league}"
            if not force and not await self._due(key, ODDS_EVERY):
                continue
            try:
                events = await self.client.odds(sport)
            except Exception as e:
                log.warning("Коэффициенты %s не получены: %s", league, e)
                continue
            await self.db.kv_set(key, str(time.time()))
            now = time.time()
            async with self.db.tx() as c:
                for ev in events:
                    kickoff = float(ev.get("commence_time") or 0)
                    odds = price(ev)
                    if not ev.get("id") or kickoff <= now or odds is None:
                        continue
                    await c.execute(
                        "INSERT INTO sport_events(id, league, home, away, kickoff, odds_home, odds_draw, odds_away, odds_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kickoff=excluded.kickoff,"
                        " odds_home=excluded.odds_home, odds_draw=excluded.odds_draw, odds_away=excluded.odds_away,"
                        " odds_at=excluded.odds_at WHERE status='open'",
                        (ev["id"], league, ev["home_team"], ev["away_team"], kickoff, *odds, now),
                    )
                    n += 1
            log.info("Футбол %s: %s матчей, запросов к API осталось %s", league, len(events), self.client.remaining)
        return n

    async def settle_pending(self, notify: Notify | None = None) -> int:
        """Рассчитывает ставки на сыгранные матчи и возвращает ставки на матчи без результата. Число рассчитанных."""
        now = time.time()
        pending = await self.db.all(
            "SELECT DISTINCT e.league FROM sport_bets b JOIN sport_events e ON e.id=b.event_id "
            "WHERE b.status='open' AND b.kickoff < ?", now - SETTLE_AFTER)
        done = 0
        if self.client.enabled:
            for row in pending:
                league = row["league"]
                key = f"sports:scores_at:{league}"
                if league not in LEAGUES or not await self._due(key, SCORES_EVERY):
                    continue
                try:
                    games = await self.client.scores(LEAGUES[league][0])
                except Exception as e:
                    log.warning("Счёт %s не получен: %s", league, e)
                    continue
                await self.db.kv_set(key, str(now))
                for game in games:
                    if not game.get("completed") or not game.get("scores"):
                        continue
                    sc = {s.get("name"): s.get("score") for s in game["scores"]}
                    try:
                        hs, as_ = int(sc[game["home_team"]]), int(sc[game["away_team"]])
                    except (KeyError, TypeError, ValueError):
                        continue
                    done += await self.settle_event(game["id"], hs, as_, notify)
        # без результата слишком долго (перенос, отмена) — возврат
        for bet in await self.db.all("SELECT * FROM sport_bets WHERE status='open' AND kickoff < ?", now - VOID_AFTER):
            await self._void(bet, notify)
            done += 1
        return done

    async def settle_event(self, event_id: str, home_score: int, away_score: int, notify: Notify | None = None) -> int:
        ev = await self.db.one("SELECT * FROM sport_events WHERE id=?", event_id)
        if not ev:
            return 0
        res = result_of(home_score, away_score)
        bets = await self.db.all("SELECT * FROM sport_bets WHERE event_id=? AND status='open'", event_id)
        now = time.time()
        msgs = []
        async with self.db.tx() as c:
            await c.execute("UPDATE sport_events SET status='done', home_score=?, away_score=?, settled_at=? WHERE id=?",
                            (home_score, away_score, now, event_id))
            for b in bets:
                won = b["pick"] == res
                payout = math.floor(b["amount"] * b["odds"]) if won else 0
                cur = b["cur"]
                detail = {"match": f"{ev['home']} — {ev['away']}", "pick": b["pick"], "odds": b["odds"],
                          "score": f"{home_score}:{away_score}", "league": ev["league"]}
                await self.casino._settle(c, b["user_id"], "football", b["amount"], payout, detail, cur)
                await c.execute("UPDATE sport_bets SET status=?, payout=?, settled_at=? WHERE id=?",
                                ("won" if won else "lost", payout, now, b["id"]))
                score = f"{ev['home']} {home_score}:{away_score} {ev['away']}"
                msgs.append((b["user_id"], f"✅ Ставка сыграла: {score}\nВыигрыш {money.fmt(payout, cur)}" if won
                             else f"❌ Ставка не сыграла: {score}"))
        if notify:
            for uid, text in msgs:
                try:
                    await notify(uid, text)
                except Exception:
                    pass
        return len(bets)

    async def _void(self, bet: dict[str, Any], notify: Notify | None) -> None:
        async with self.db.tx() as c:
            await self.db.change_balance(c, bet["user_id"], bet["amount"], "refund", "football", bet["cur"])
            await c.execute("UPDATE sport_bets SET status='void', payout=?, settled_at=? WHERE id=?",
                            (bet["amount"], time.time(), bet["id"]))
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
        # на один матч — не больше максимальной ставки суммарно (чтобы не обходили лимит дроблением)
        hi = self.casino.cfg.max_bet if cur == money.STARS else money.TON_MAX_BET
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

    def summary(self) -> str:
        return json.dumps({"enabled": self.client.enabled, "remaining": self.client.remaining})
