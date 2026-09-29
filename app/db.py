"""SQLite: пользователи, баланс с журналом операций, платежи, чеки, игры."""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import aiosqlite

from . import money
from .games import logic as g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    balance    INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    deposited  INTEGER NOT NULL DEFAULT 0,
    wagered    INTEGER NOT NULL DEFAULT 0,
    won        INTEGER NOT NULL DEFAULT 0,
    referrer_id INTEGER,                      -- кто пригласил (реферальная программа)
    created_at REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    delta         INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    ref           TEXT,
    balance_after INTEGER NOT NULL,
    ts            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id, id);
CREATE TABLE IF NOT EXISTS payments (
    charge_id TEXT PRIMARY KEY,
    user_id   INTEGER NOT NULL,
    amount    INTEGER NOT NULL,
    ts        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
    code       TEXT PRIMARY KEY,
    amount     INTEGER NOT NULL,
    total      INTEGER NOT NULL,
    left       INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS check_uses (
    code    TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    ts      REAL NOT NULL,
    PRIMARY KEY (code, user_id)
);
CREATE TABLE IF NOT EXISTS bets (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    game    TEXT NOT NULL,
    bet     INTEGER NOT NULL,
    win     INTEGER NOT NULL,
    detail  TEXT,
    ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bets_user ON bets(user_id, id);
CREATE TABLE IF NOT EXISTS mines_games (
    user_id    INTEGER PRIMARY KEY,
    bet        INTEGER NOT NULL,
    mines      INTEGER NOT NULL,
    layout     TEXT NOT NULL,
    opened     TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS crash_games (
    user_id    INTEGER PRIMARY KEY,
    bet        INTEGER NOT NULL,
    point      REAL NOT NULL,
    auto       REAL,
    started_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS withdrawals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    amount       INTEGER NOT NULL,
    gift_id      TEXT NOT NULL,
    gift_emoji   TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | sending | sent | rejected
    error        TEXT,
    admin_id     INTEGER,
    created_at   REAL NOT NULL,
    processed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(user_id, id);
CREATE TABLE IF NOT EXISTS business_connections (
    id         TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    can_gifts  INTEGER NOT NULL,          -- может ли бот видеть и передавать подарки
    is_enabled INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nft_models (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id   TEXT NOT NULL,          -- id коллекции (исходного подарка)
    collection_name TEXT NOT NULL,          -- «Plush Pepe»
    model           TEXT NOT NULL,          -- «Frog Prince»
    rarity          REAL,                   -- редкость модели, %
    emoji           TEXT,
    stock           INTEGER NOT NULL DEFAULT 0,  -- сколько подарков этой модели у релейера
    reserved        INTEGER NOT NULL DEFAULT 0,  -- выиграно, но ещё не передано
    price           INTEGER,                -- флор MRKT в звёздах
    price_at        REAL,                   -- когда цена проверена
    enabled         INTEGER NOT NULL DEFAULT 1,
    UNIQUE (collection_id, model)
);
CREATE TABLE IF NOT EXISTS nft_wins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    price         INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'won',   -- won | sending | sent | failed
    owned_gift_id TEXT,                          -- какой конкретно подарок ушёл
    error         TEXT,
    created_at    REAL NOT NULL,
    sent_at       REAL
);
CREATE TABLE IF NOT EXISTS crash_rounds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    point         REAL NOT NULL,
    status        TEXT NOT NULL,            -- betting | running | crashed
    betting_until REAL NOT NULL,
    started_at    REAL,
    crashed_at    REAL,
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS crash_bets (
    round_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    bet       INTEGER NOT NULL,
    auto      REAL,
    cashout   REAL,
    win       INTEGER,
    placed_at REAL NOT NULL,
    PRIMARY KEY (round_id, user_id)
);
CREATE TABLE IF NOT EXISTS pvp_rounds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game        TEXT NOT NULL DEFAULT 'roulette', -- roulette | hockey
    detail      TEXT,                             -- для хоккея: траектория шайбы и зоны
    status      TEXT NOT NULL DEFAULT 'open',   -- open | done | refunded
    created_at  REAL NOT NULL,
    ends_at     REAL,
    winner_id   INTEGER,
    pot         INTEGER NOT NULL DEFAULT 0,
    payout      INTEGER NOT NULL DEFAULT 0,
    ticket      INTEGER,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS user_gifts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    ref             TEXT NOT NULL UNIQUE,        -- msg_id подарка на аккаунте релейера
    kind            TEXT NOT NULL,               -- nft | gift
    collection_id   TEXT,
    collection_name TEXT,
    number          INTEGER,
    model           TEXT,
    emoji           TEXT,
    rarity          REAL,
    value           INTEGER,                     -- цена в звёздах (NFT — пол маркета модели)
    priced_at       REAL,
    transfer_at     REAL NOT NULL DEFAULT 0,     -- раньше этого времени Telegram не даёт передать
    status          TEXT NOT NULL,               -- owned | staked | withdrawing | withdrawn | sold | credited | stock (прислал админ) | lost (проиграл в апгрейде)
    round_id        INTEGER,
    from_user       INTEGER,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS user_gifts_user ON user_gifts(user_id, status);
CREATE TABLE IF NOT EXISTS pvp_bets (
    round_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    amount   INTEGER NOT NULL,             -- звёзды + стоимость поставленных подарков
    joined   REAL NOT NULL,
    gifts    TEXT,                         -- JSON: поставленные подарки (для показа)
    PRIMARY KEY (round_id, user_id)
);
"""


TON_SCHEMA = """
CREATE TABLE IF NOT EXISTS ton_withdrawals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    amount       INTEGER NOT NULL,             -- nanoTON
    address      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | rejected
    admin_id     INTEGER,
    created_at   REAL NOT NULL,
    processed_at REAL
);
"""

REFERRAL_RATE = 0.10   # пригласивший получает 10% от каждой покупки звёзд рефералом
REFERRAL_WINDOW = 24 * 3600   # привязать можно только новичка: не позже суток после первого входа


class InsufficientFunds(Exception):
    pass


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA)
        await self._migrate()

    async def _migrate(self) -> None:
        """Добавляет новые столбцы в базу, созданную старой версией бота."""
        async with self.conn.execute("PRAGMA table_info(pvp_rounds)") as cur:
            cols = {r["name"] for r in await cur.fetchall()}
        if "game" not in cols:
            await self.conn.execute("ALTER TABLE pvp_rounds ADD COLUMN game TEXT NOT NULL DEFAULT 'roulette'")
        if "detail" not in cols:
            await self.conn.execute("ALTER TABLE pvp_rounds ADD COLUMN detail TEXT")
        async with self.conn.execute("PRAGMA table_info(users)") as cur:
            if "referrer_id" not in {r["name"] for r in await cur.fetchall()}:
                await self.conn.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS users_referrer ON users(referrer_id)")
        for table in ("user_gifts", "nft_models"):   # test=1 — демо-NFT (нет на релейере, выигрыш платится звёздами)
            async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
                if "test" not in {r["name"] for r in await cur.fetchall()}:
                    await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN test INTEGER NOT NULL DEFAULT 0")
        async with self.conn.execute("PRAGMA table_info(pvp_bets)") as cur:
            if "gifts" not in {r["name"] for r in await cur.fetchall()}:
                await self.conn.execute("ALTER TABLE pvp_bets ADD COLUMN gifts TEXT")
        # TON — вторая валюта: баланс в nanoTON и валюта у всех денежных записей
        await self._add_columns("users", {"ton": "INTEGER NOT NULL DEFAULT 0",
                                          "ton_deposited": "INTEGER NOT NULL DEFAULT 0",
                                          "ton_wagered": "INTEGER NOT NULL DEFAULT 0",
                                          "ton_won": "INTEGER NOT NULL DEFAULT 0"})
        for table in ("ledger", "bets", "payments", "mines_games", "crash_bets", "pvp_rounds"):
            await self._add_columns(table, {"cur": "TEXT NOT NULL DEFAULT 'stars'"})
        await self._add_columns("crash_bets", {"gifts": "TEXT"})   # JSON: NFT, поставленные в краш
        # рейкбек копится с каждой ставки: звёзды — в тысячных долях звезды, TON — в nanoTON
        await self._add_columns("users", {"rake_milli": "INTEGER NOT NULL DEFAULT 0",
                                          "rake_ton": "INTEGER NOT NULL DEFAULT 0",
                                          "bonus_at": "REAL NOT NULL DEFAULT 0"})
        await self.conn.executescript(TON_SCHEMA)
        # чеки игроков оплачены из их баланса (paid=1), чеки админа — бесплатные
        await self._add_columns("checks", {"paid": "INTEGER NOT NULL DEFAULT 0"})
        await self._add_columns("users", {"free_case_at": "REAL NOT NULL DEFAULT 0"})

    async def _add_columns(self, table: str, columns: dict[str, str]) -> None:
        async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
            have = {r["name"] for r in await cur.fetchall()}
        for name, ddl in columns.items():
            if name not in have:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "База не подключена"
        return self._conn

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[aiosqlite.Connection]:
        """Транзакция записи. Все денежные операции идут только внутри неё."""
        async with self._lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")

    async def kv_get(self, key: str) -> str | None:
        row = await self.one("SELECT value FROM kv WHERE key=?", key)
        return row["value"] if row else None

    async def kv_set(self, key: str, value: str | None) -> None:
        async with self.tx() as c:
            if value is None:
                await c.execute("DELETE FROM kv WHERE key=?", (key,))
            else:
                await c.execute(
                    "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

    async def one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        async with self.conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def all(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        async with self.conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ---------- пользователи ----------

    async def touch_user(self, user_id: int, username: str | None, first_name: str | None) -> tuple[dict, bool]:
        """Создаёт или обновляет пользователя. Возвращает (пользователь, создан_ли)."""
        now = time.time()
        async with self.tx() as c:
            cur = await c.execute(
                "INSERT OR IGNORE INTO users(id, username, first_name, created_at, last_seen) VALUES (?,?,?,?,?)",
                (user_id, username, first_name, now, now),
            )
            created = cur.rowcount == 1
            if not created:
                await c.execute(
                    "UPDATE users SET username=?, first_name=?, last_seen=? WHERE id=?",
                    (username, first_name, now, user_id),
                )
        user = await self.get_user(user_id)
        assert user is not None
        return user, created

    async def get_user(self, user_id: int) -> dict | None:
        return await self.one("SELECT * FROM users WHERE id=?", user_id)

    async def find_user(self, query: str) -> dict | None:
        q = query.strip().lstrip("@")
        if q.isdigit():
            return await self.get_user(int(q))
        return await self.one("SELECT * FROM users WHERE lower(username)=lower(?)", q)

    # ---------- деньги (вызывать внутри tx) ----------

    @staticmethod
    async def change_balance(
        c: aiosqlite.Connection, user_id: int, delta: int, kind: str, ref: str | None = None, cur: str = money.STARS
    ) -> int:
        col = money.column(cur)
        res = await c.execute(
            f"UPDATE users SET {col} = {col} + ? WHERE id=? AND {col} + ? >= 0",
            (delta, user_id, delta),
        )
        if res.rowcount != 1:
            raise InsufficientFunds()
        async with c.execute(f"SELECT {col} b FROM users WHERE id=?", (user_id,)) as q:
            balance = (await q.fetchone())["b"]
        await c.execute(
            "INSERT INTO ledger(user_id, delta, kind, ref, balance_after, ts, cur) VALUES (?,?,?,?,?,?,?)",
            (user_id, delta, kind, ref, balance, time.time(), cur),
        )
        return balance

    @staticmethod
    async def log_bet(c: aiosqlite.Connection, user_id: int, game: str, bet: int, win: int, detail: str = "",
                      cur: str = money.STARS) -> None:
        await c.execute(
            "INSERT INTO bets(user_id, game, bet, win, detail, ts, cur) VALUES (?,?,?,?,?,?,?)",
            (user_id, game, bet, win, detail, time.time(), cur),
        )
        wagered, won = ("wagered", "won") if cur == money.STARS else ("ton_wagered", "ton_won")
        await c.execute(
            f"UPDATE users SET {wagered} = {wagered} + ?, {won} = {won} + ? WHERE id=?", (bet, win, user_id)
        )
        # рейкбек по VIP-уровню: доля ставки копится и забирается в профиле
        async with c.execute("SELECT wagered, ton_wagered FROM users WHERE id=?", (user_id,)) as q:
            row = await q.fetchone()
        if row:
            pct = g.LEVELS[g.level_for(g.level_points(row["wagered"], row["ton_wagered"]))][3]
            if cur == money.STARS:
                await c.execute("UPDATE users SET rake_milli = rake_milli + ? WHERE id=?",
                                (int(bet * pct * 1000), user_id))
            else:
                await c.execute("UPDATE users SET rake_ton = rake_ton + ? WHERE id=?", (int(bet * pct), user_id))

    # ---------- платежи ----------

    async def set_referrer(self, user_id: int, referrer_id: int) -> bool:
        """Привязывает реферала. Только новичку (первые сутки) без покупок и один раз; себя и «по кругу» пригласить нельзя."""
        if user_id == referrer_id:
            return False
        async with self.tx() as c:
            cur = await c.execute(
                "UPDATE users SET referrer_id=? WHERE id=? AND referrer_id IS NULL AND deposited=0 AND created_at > ? "
                "AND EXISTS (SELECT 1 FROM users r WHERE r.id=? AND (r.referrer_id IS NULL OR r.referrer_id != ?))",
                (referrer_id, user_id, time.time() - REFERRAL_WINDOW, referrer_id, user_id),
            )
        return cur.rowcount == 1

    async def credit_payment(self, charge_id: str, user_id: int, amount: int) -> bool:
        """Зачисляет оплату Stars (и 10% пригласившему). Повторный апдейт с тем же charge_id ничего не делает."""
        async with self.tx() as c:
            cur = await c.execute(
                "INSERT OR IGNORE INTO payments(charge_id, user_id, amount, ts) VALUES (?,?,?,?)",
                (charge_id, user_id, amount, time.time()),
            )
            if cur.rowcount != 1:
                return False
            await c.execute(
                "INSERT OR IGNORE INTO users(id, created_at, last_seen) VALUES (?,?,?)",
                (user_id, time.time(), time.time()),
            )
            await self.change_balance(c, user_id, amount, "deposit", charge_id)
            await c.execute("UPDATE users SET deposited = deposited + ? WHERE id=?", (amount, user_id))
            async with c.execute("SELECT referrer_id FROM users WHERE id=?", (user_id,)) as q:
                referrer = (await q.fetchone())["referrer_id"]
            bonus = int(amount * REFERRAL_RATE)
            if referrer and bonus > 0:
                await self.change_balance(c, referrer, bonus, "ref_bonus", str(user_id))
        return True

    # ---------- история ----------

    async def credit_ton(self, tx_hash: str, user_id: int, amount: int) -> bool:
        """Зачисляет перевод TON (nanoTON). Повторная обработка той же транзакции ничего не делает."""
        async with self.tx() as c:
            res = await c.execute(
                "INSERT OR IGNORE INTO payments(charge_id, user_id, amount, ts, cur) VALUES (?,?,?,?,?)",
                (f"ton:{tx_hash}", user_id, amount, time.time(), money.TON),
            )
            if res.rowcount != 1:
                return False
            await c.execute("INSERT OR IGNORE INTO users(id, created_at, last_seen) VALUES (?,?,?)",
                            (user_id, time.time(), time.time()))
            await self.change_balance(c, user_id, amount, "deposit", tx_hash, money.TON)
            await c.execute("UPDATE users SET ton_deposited = ton_deposited + ? WHERE id=?", (amount, user_id))
            async with c.execute("SELECT referrer_id FROM users WHERE id=?", (user_id,)) as q:
                referrer = (await q.fetchone())["referrer_id"]
            bonus = int(amount * REFERRAL_RATE)
            if referrer and bonus > 0:
                await self.change_balance(c, referrer, bonus, "ref_bonus", str(user_id), money.TON)
        return True

    async def recent_bets(self, user_id: int, limit: int = 20) -> list[dict]:
        return await self.all(
            "SELECT game, bet, win, ts, cur FROM bets WHERE user_id=? ORDER BY id DESC LIMIT ?", user_id, limit
        )

    async def stats(self) -> dict[str, Any]:
        users = await self.one("SELECT COUNT(*) n, COALESCE(SUM(balance),0) bal, COALESCE(SUM(ton),0) ton FROM users")
        pay = await self.one("SELECT COUNT(*) n, COALESCE(SUM(amount),0) s FROM payments WHERE cur='stars'")
        bets = await self.one("SELECT COUNT(*) n, COALESCE(SUM(bet),0) b, COALESCE(SUM(win),0) w FROM bets "
                              "WHERE cur='stars'")
        ton_pay = await self.one("SELECT COUNT(*) n, COALESCE(SUM(amount),0) s FROM payments WHERE cur='ton'")
        ton_bets = await self.one("SELECT COALESCE(SUM(bet),0) b, COALESCE(SUM(win),0) w FROM bets WHERE cur='ton'")
        ton_wd = await self.one(
            "SELECT COALESCE(SUM(CASE WHEN status='sent' THEN amount END),0) sent, "
            "COALESCE(SUM(CASE WHEN status='pending' THEN amount END),0) pending FROM ton_withdrawals")
        checks = await self.one(
            "SELECT COALESCE(SUM(amount*left),0) liab, COUNT(*) n FROM checks WHERE active=1 AND left>0 AND paid=0"
        )
        issued = await self.one("SELECT COALESCE(SUM(l.delta),0) s FROM ledger l JOIN checks c ON c.code=l.ref "
                                "WHERE l.kind='check' AND c.paid=0")
        wd = await self.one(
            "SELECT COALESCE(SUM(CASE WHEN status='sent' THEN amount END),0) sent, "
            "COALESCE(SUM(CASE WHEN status IN ('pending','sending') THEN amount END),0) pending FROM withdrawals"
        )
        return {
            "users": users["n"],
            "balances": users["bal"],
            "payments": pay["n"],
            "deposited": pay["s"],
            "bets": bets["n"],
            "wagered": bets["b"],
            "won": bets["w"],
            "active_checks": checks["n"],
            "checks_liability": checks["liab"],
            "checks_redeemed": issued["s"],
            "withdrawn": wd["sent"],
            "withdraw_pending": wd["pending"],
            "ton_balances": users["ton"],
            "ton_payments": ton_pay["n"],
            "ton_deposited": ton_pay["s"],
            "ton_wagered": ton_bets["b"],
            "ton_won": ton_bets["w"],
            "ton_withdrawn": ton_wd["sent"],
            "ton_withdraw_pending": ton_wd["pending"],
        }
