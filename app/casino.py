"""Игровые операции поверх базы. Каждая операция — одна транзакция, поэтому двойных выплат не бывает."""
from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any

import aiosqlite

from .config import Config
from .db import REFERRAL_RATE, Database, InsufficientFunds
from .games import logic as g
from .nft import PRICE_MAX_AGE as NFT_PRICE_MAX_AGE

CRASH_BETTING_SECONDS = 7   # приём ставок перед стартом ракеты
CRASH_PAUSE_SECONDS = 3     # пауза после краша
PVP_ROUND_SECONDS = 30
PVP_GAMES = ("roulette", "hockey")      # сколько длится раунд после второго игрока
PVP_IDLE_REFUND = 600       # одиночную ставку возвращаем через 10 минут
CHECK_CODE_RE = re.compile(r"^[A-Za-z0-9]{6,32}$")
GIFT_SELL_RATE = 0.9        # казино выкупает NFT игрока за 90% пола маркета
MAX_GIFTS_PER_BET = 20


class GameError(Exception):
    """Ошибка, текст которой можно показать игроку."""


def display_name(user: dict | None) -> str:
    if not user:
        return "Игрок"
    return user.get("first_name") or (f"@{user['username']}" if user.get("username") else f"id{user['id']}")


class Casino:
    def __init__(self, db: Database, cfg: Config):
        self.db = db
        self.cfg = cfg

    # ---------- общее ----------

    def _check_bet(self, bet: Any) -> int:
        if not isinstance(bet, int) or isinstance(bet, bool):
            raise GameError("Ставка должна быть целым числом")
        if bet < self.cfg.min_bet:
            raise GameError(f"Минимальная ставка — {self.cfg.min_bet} ⭐")
        if bet > self.cfg.max_bet:
            raise GameError(f"Максимальная ставка — {self.cfg.max_bet} ⭐")
        return bet

    async def _take(self, c: aiosqlite.Connection, user_id: int, bet: int, game: str) -> int:
        try:
            return await self.db.change_balance(c, user_id, -bet, "bet", game)
        except InsufficientFunds:
            raise GameError("Недостаточно звёзд на балансе") from None

    async def _settle(
        self, c: aiosqlite.Connection, user_id: int, game: str, bet: int, win: int, detail: dict
    ) -> int:
        balance = None
        if win > 0:
            balance = await self.db.change_balance(c, user_id, win, "win", game)
        await self.db.log_bet(c, user_id, game, bet, win, json.dumps(detail, ensure_ascii=False))
        if balance is None:
            async with c.execute("SELECT balance FROM users WHERE id=?", (user_id,)) as q:
                balance = (await q.fetchone())["balance"]
        return balance

    async def register(self, user_id: int, username: str | None, first_name: str | None) -> dict:
        """Создаёт или обновляет игрока; новичку начисляет стартовый бонус, если он включён."""
        user, created = await self.db.touch_user(user_id, username, first_name)
        if created and self.cfg.start_bonus > 0:
            async with self.db.tx() as c:
                await self.db.change_balance(c, user_id, self.cfg.start_bonus, "bonus", "start")
            user = await self.db.get_user(user_id)
        return user

    async def profile(self, user_id: int) -> dict:
        """Статистика игрока для страницы профиля."""
        user = await self.db.get_user(user_id) or {}
        bets = await self.db.one(
            "SELECT COUNT(*) n, COALESCE(MAX(win),0) best, COALESCE(SUM(win > bet),0) wins FROM bets WHERE user_id=?",
            user_id)
        fav = await self.db.one(
            "SELECT game, COUNT(*) n FROM bets WHERE user_id=? GROUP BY game ORDER BY n DESC LIMIT 1", user_id)
        best = await self.db.one(
            "SELECT game, bet, win FROM bets WHERE user_id=? AND win > 0 ORDER BY win DESC, id LIMIT 1", user_id)
        withdrawn = await self.db.one(
            "SELECT COALESCE(SUM(amount),0) s FROM withdrawals WHERE user_id=? AND status='sent'", user_id)
        return {
            "id": user_id, "name": display_name(user or None), "username": user.get("username"),
            "joined": user.get("created_at"), "balance": user.get("balance", 0),
            "deposited": user.get("deposited", 0), "wagered": user.get("wagered", 0), "won": user.get("won", 0),
            "withdrawn": withdrawn["s"], "games": bets["n"], "wins": bets["wins"],
            "favorite": fav["game"] if fav else None,
            "best": {"game": best["game"], "bet": best["bet"], "win": best["win"]} if best else None,
            "wager": await self.wager_status(user_id),
        }

    async def referrals(self, user_id: int) -> dict:
        """Рефералы игрока и сколько звёзд он с них получил."""
        rows = await self.db.all(
            "SELECT u.id, u.username, u.first_name, u.created_at, "
            "(SELECT COALESCE(SUM(delta),0) FROM ledger l WHERE l.user_id=? AND l.kind='ref_bonus' "
            " AND l.ref=CAST(u.id AS TEXT)) earned "
            "FROM users u WHERE u.referrer_id=? ORDER BY earned DESC, u.created_at DESC LIMIT 50",
            user_id, user_id)
        total = await self.db.one(
            "SELECT COALESCE(SUM(delta),0) s FROM ledger WHERE user_id=? AND kind='ref_bonus'", user_id)
        count = await self.db.one("SELECT COUNT(*) n FROM users WHERE referrer_id=?", user_id)
        return {
            "count": count["n"], "earned": total["s"], "rate": REFERRAL_RATE,
            "list": [{"id": r["id"], "name": display_name(r), "earned": r["earned"], "joined": r["created_at"]}
                     for r in rows],
        }

    async def big_wins(self, limit: int = 20) -> list[dict]:
        """Лента крупных выигрышей всех игроков (от ×5)."""
        rows = await self.db.all(
            "SELECT b.game, b.bet, b.win, b.ts, u.id, u.first_name, u.username FROM bets b "
            "LEFT JOIN users u ON u.id = b.user_id WHERE b.win >= b.bet * 5 AND b.win > 0 "
            "ORDER BY b.id DESC LIMIT ?",
            limit,
        )
        return [
            {"game": r["game"], "bet": r["bet"], "win": r["win"], "x": round(r["win"] / r["bet"], 2),
             "name": display_name({"id": r["id"], "first_name": r["first_name"], "username": r["username"]})}
            for r in rows
        ]

    # ---------- чеки ----------

    async def create_check(self, admin_id: int, amount: int, activations: int) -> str:
        if amount < 1 or amount > 1_000_000:
            raise GameError("Сумма чека — от 1 до 1 000 000 ⭐")
        if activations < 1 or activations > 10_000:
            raise GameError("Активаций — от 1 до 10 000")
        code = secrets.token_hex(6)
        async with self.db.tx() as c:
            await c.execute(
                "INSERT INTO checks(code, amount, total, left, created_by, created_at) VALUES (?,?,?,?,?,?)",
                (code, amount, activations, activations, admin_id, time.time()),
            )
        return code

    async def activate_check(self, user_id: int, code: str) -> tuple[int, int]:
        """Возвращает (сумма, новый баланс)."""
        code = code.strip()
        if not CHECK_CODE_RE.match(code):
            raise GameError("Чек не найден")
        async with self.db.tx() as c:
            async with c.execute("SELECT * FROM checks WHERE code=?", (code,)) as q:
                check = await q.fetchone()
            if not check or not check["active"]:
                raise GameError("Чек не найден или отозван")
            if check["left"] <= 0:
                raise GameError("Чек уже полностью активирован")
            cur = await c.execute(
                "INSERT OR IGNORE INTO check_uses(code, user_id, ts) VALUES (?,?,?)", (code, user_id, time.time())
            )
            if cur.rowcount != 1:
                raise GameError("Вы уже активировали этот чек")
            await c.execute("UPDATE checks SET left = left - 1 WHERE code=?", (code,))
            balance = await self.db.change_balance(c, user_id, check["amount"], "check", code)
        return check["amount"], balance

    async def list_checks(self) -> list[dict]:
        return await self.db.all("SELECT * FROM checks WHERE active=1 AND left>0 ORDER BY created_at DESC LIMIT 50")

    async def revoke_check(self, code: str) -> bool:
        async with self.db.tx() as c:
            cur = await c.execute("UPDATE checks SET active=0 WHERE code=? AND active=1", (code.strip(),))
        return cur.rowcount == 1

    # ---------- вывод подарками ----------

    async def wager_status(self, user_id: int) -> dict:
        """Звёзды из чеков и бонусов нужно отыграть: сумма ставок ≥ полученного бесплатно."""
        free = await self.db.one(
            "SELECT COALESCE(SUM(delta),0) s FROM ledger WHERE user_id=? AND kind IN ('check','bonus','ref_bonus')",
            user_id
        )
        user = await self.db.get_user(user_id)
        wagered = user["wagered"] if user else 0
        return {"required": free["s"], "done": wagered, "left": max(0, free["s"] - wagered)}

    async def withdraw_request(self, user_id: int, gift_id: str, price: int, emoji: str | None) -> dict:
        if price < 1:
            raise GameError("Подарок недоступен")
        wager = await self.wager_status(user_id)
        if wager["left"] > 0:
            raise GameError(f"Сначала отыграйте бонусные звёзды: осталось поставить {wager['left']} ⭐")
        async with self.db.tx() as c:
            async with c.execute(
                "SELECT 1 FROM withdrawals WHERE user_id=? AND status IN ('pending','sending')", (user_id,)
            ) as q:
                if await q.fetchone():
                    raise GameError("У вас уже есть заявка на вывод — дождитесь её обработки")
            cur = await c.execute(
                "INSERT INTO withdrawals(user_id, amount, gift_id, gift_emoji, created_at) VALUES (?,?,?,?,?)",
                (user_id, price, gift_id, emoji, time.time()),
            )
            wd_id = cur.lastrowid
            try:
                balance = await self.db.change_balance(c, user_id, -price, "withdraw", str(wd_id))
            except InsufficientFunds:
                raise GameError("Недостаточно звёзд на балансе") from None
        return {"id": wd_id, "user_id": user_id, "amount": price, "emoji": emoji, "status": "pending",
                "balance": balance}

    async def get_withdrawal(self, wd_id: int) -> dict | None:
        return await self.db.one("SELECT * FROM withdrawals WHERE id=?", wd_id)

    async def withdraw_claim(self, wd_id: int, admin_id: int) -> dict | None:
        """Берёт заявку в отправку. None — если её уже обработали."""
        async with self.db.tx() as c:
            cur = await c.execute(
                "UPDATE withdrawals SET status='sending', admin_id=? WHERE id=? AND status='pending'", (admin_id, wd_id)
            )
        return await self.get_withdrawal(wd_id) if cur.rowcount == 1 else None

    async def withdraw_finish(self, wd_id: int, ok: bool, error: str | None = None) -> None:
        """После попытки отправки: успех — sent, ошибка — заявка снова ждёт решения."""
        async with self.db.tx() as c:
            await c.execute(
                "UPDATE withdrawals SET status=?, error=?, processed_at=? WHERE id=? AND status='sending'",
                ("sent" if ok else "pending", error, time.time(), wd_id),
            )

    async def withdraw_reject(self, wd_id: int, admin_id: int) -> dict | None:
        """Отклоняет заявку и возвращает звёзды игроку."""
        async with self.db.tx() as c:
            cur = await c.execute(
                "UPDATE withdrawals SET status='rejected', admin_id=?, processed_at=? WHERE id=? AND status='pending'",
                (admin_id, time.time(), wd_id),
            )
            if cur.rowcount != 1:
                return None
            async with c.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,)) as q:
                wd = dict(await q.fetchone())
            wd["balance"] = await self.db.change_balance(c, wd["user_id"], wd["amount"], "withdraw_refund", str(wd_id))
        return wd

    async def withdrawals(self, user_id: int | None = None, status: str | None = None, limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM withdrawals WHERE 1=1"
        args: list[Any] = []
        if user_id is not None:
            sql += " AND user_id=?"
            args.append(user_id)
        if status is not None:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return await self.db.all(sql, *args)

    # ---------- слоты ----------

    async def slots(self, user_id: int, bet: Any, value: int | None = None) -> dict:
        """value — исход 1–64 (например, от 🎰 Telegram в чате); без него выпадает случайно на сервере.

        7️⃣7️⃣7️⃣ — NFT у релейера с рыночной ценой ≈ ×40 от ставки; если такого нет — ×40 звёздами.
        """
        bet = self._check_bet(bet)
        if value is None:
            value, reels, mult = g.slots_spin()
        else:
            reels = g.slots_reels(value)
            mult = g.slots_multiplier(reels)
        detail: dict[str, Any] = {"value": value, "reels": reels}
        nft = None
        async with self.db.tx() as c:
            await self._take(c, user_id, bet, "slots")
            if value == 64:
                nft = await self._reserve_nft_near(c, user_id, bet * g.SLOT_777)
            if nft:
                detail["nft_win"] = nft["win_id"]
                await self.db.log_bet(c, user_id, "slots", bet, nft["price"], json.dumps(detail, ensure_ascii=False))
                async with c.execute("SELECT balance FROM users WHERE id=?", (user_id,)) as q:
                    balance = (await q.fetchone())["balance"]
                win = nft["price"]
            else:
                win = g.payout(bet, mult)
                balance = await self._settle(c, user_id, "slots", bet, win, detail)
        result = {"value": value, "reels": reels, "multiplier": mult, "win": win, "balance": balance}
        if nft:
            result["nft"] = nft
        return result

    async def _reserve_nft_near(self, c: aiosqlite.Connection, user_id: int, target: int) -> dict | None:
        """Резервирует у релейера модель с ценой ближе всего к target (±SLOT_NFT_TOLERANCE)."""
        lo, hi = target * (1 - g.SLOT_NFT_TOLERANCE), target * (1 + g.SLOT_NFT_TOLERANCE)
        async with c.execute(
            "SELECT * FROM nft_models WHERE enabled=1 AND stock > reserved AND price BETWEEN ? AND ? "
            "AND price_at > ? ORDER BY ABS(price - ?) LIMIT 1",
            (lo, hi, time.time() - NFT_PRICE_MAX_AGE, target),
        ) as q:
            row = await q.fetchone()
        if not row:
            return None
        await c.execute("UPDATE nft_models SET reserved=reserved+1 WHERE id=?", (row["id"],))
        cur = await c.execute(
            "INSERT INTO nft_wins(model_id, user_id, price, created_at) VALUES (?,?,?,?)",
            (row["id"], user_id, row["price"], time.time()),
        )
        return {"win_id": cur.lastrowid, "title": row["collection_name"], "model": row["model"],
                "emoji": row["emoji"] or "💎", "price": row["price"]}

    # ---------- кости ----------

    async def dice(self, user_id: int, bet: Any, chance: Any, over: Any = False) -> dict:
        bet = self._check_bet(bet)
        if not isinstance(chance, (int, float)) or isinstance(chance, bool) or not g.dice_valid_chance(float(chance)):
            raise GameError(f"Шанс — от {g.DICE_MIN_CHANCE} до {g.DICE_MAX_CHANCE:g}%, не больше двух знаков после точки")
        chance = float(chance)
        over = over is True
        roll, won, mult = g.dice_roll(chance, over)
        win = g.payout(bet, mult)
        async with self.db.tx() as c:
            await self._take(c, user_id, bet, "dice")
            balance = await self._settle(
                c, user_id, "dice", bet, win, {"roll": roll, "chance": chance, "over": over}
            )
        return {"roll": roll, "chance": chance, "over": over, "won": won,
                "multiplier": g.dice_multiplier(chance), "win": win, "balance": balance}

    # ---------- кейсы ----------

    async def open_case(self, user_id: int, case: dict) -> dict:
        """case — готовый кейс из CaseCatalog (с живыми ценами). NFT не зачисляется звёздами, а передаётся игроку."""
        prize = g.pick_weighted(case["prizes"], [p["weight"] for p in case["prizes"]])
        detail = {"case": case["id"], "prize": prize["amount"], "gift": prize["emoji"], "kind": prize["kind"]}
        async with self.db.tx() as c:
            await self._take(c, user_id, case["price"], "case")
            if prize["kind"] == "nft":
                # Резервируем подарок этой модели у релейера; конкретный NFT выберется при передаче
                cur = await c.execute(
                    "UPDATE nft_models SET reserved=reserved+1 WHERE id=? AND enabled=1 AND stock > reserved",
                    (prize["model_id"],),
                )
                if cur.rowcount != 1:
                    raise GameError("Подарки этой модели только что закончились — откройте кейс ещё раз")
                win = await c.execute(
                    "INSERT INTO nft_wins(model_id, user_id, price, created_at) VALUES (?,?,?,?)",
                    (prize["model_id"], user_id, prize["amount"], time.time()),
                )
                detail["nft_win"] = win.lastrowid
                detail["model"] = prize["model"]
                await self.db.log_bet(c, user_id, "case", case["price"], prize["amount"],
                                      json.dumps(detail, ensure_ascii=False))
                async with c.execute("SELECT balance FROM users WHERE id=?", (user_id,)) as q:
                    balance = (await q.fetchone())["balance"]
            else:
                balance = await self._settle(c, user_id, "case", case["price"], prize["amount"], detail)
        result = {"case": case["id"], "kind": prize["kind"], "prize": prize["amount"], "gift": prize["emoji"],
                  "balance": balance}
        if prize["kind"] == "nft":
            result["nft"] = {"win_id": detail["nft_win"], "title": prize["title"], "model": prize["model"]}
        return result

    # ---------- мины ----------

    @staticmethod
    def _mines_view(game: dict, reveal: bool = False) -> dict:
        opened = json.loads(game["opened"])
        view = {
            "active": not reveal,
            "bet": game["bet"],
            "mines": game["mines"],
            "opened": opened,
            "multiplier": g.mines_multiplier(game["mines"], len(opened)),
            "next_multiplier": g.mines_multiplier(game["mines"], len(opened) + 1)
            if len(opened) < g.MINES_CELLS - game["mines"] else None,
        }
        view["cashout"] = g.payout(game["bet"], view["multiplier"]) if opened else game["bet"]
        if reveal:
            view["layout"] = json.loads(game["layout"])
        return view

    async def mines_state(self, user_id: int) -> dict | None:
        game = await self.db.one("SELECT * FROM mines_games WHERE user_id=?", user_id)
        return self._mines_view(game) if game else None

    async def mines_start(self, user_id: int, bet: Any, mines: Any) -> dict:
        bet = self._check_bet(bet)
        if not isinstance(mines, int) or isinstance(mines, bool) or not g.MINES_MIN <= mines <= 24:
            raise GameError(f"Мин — от {g.MINES_MIN} до 24")
        layout = g.mines_place(mines)
        async with self.db.tx() as c:
            async with c.execute("SELECT 1 FROM mines_games WHERE user_id=?", (user_id,)) as q:
                if await q.fetchone():
                    raise GameError("Сначала закончите текущую игру")
            balance = await self._take(c, user_id, bet, "mines")
            await c.execute(
                "INSERT INTO mines_games(user_id, bet, mines, layout, created_at) VALUES (?,?,?,?,?)",
                (user_id, bet, mines, json.dumps(layout), time.time()),
            )
            async with c.execute("SELECT * FROM mines_games WHERE user_id=?", (user_id,)) as q:
                game = dict(await q.fetchone())
        return {**self._mines_view(game), "balance": balance}

    async def mines_open(self, user_id: int, cell: Any) -> dict:
        if not isinstance(cell, int) or not 0 <= cell < g.MINES_CELLS:
            raise GameError("Нет такой клетки")
        async with self.db.tx() as c:
            async with c.execute("SELECT * FROM mines_games WHERE user_id=?", (user_id,)) as q:
                row = await q.fetchone()
            if not row:
                raise GameError("Нет активной игры")
            game = dict(row)
            layout = json.loads(game["layout"])
            opened = json.loads(game["opened"])
            if cell in opened:
                raise GameError("Клетка уже открыта")
            if cell in layout:
                await c.execute("DELETE FROM mines_games WHERE user_id=?", (user_id,))
                game["opened"] = json.dumps(opened)
                balance = await self._settle(
                    c, user_id, "mines", game["bet"], 0, {"mines": game["mines"], "opened": len(opened), "boom": cell}
                )
                return {**self._mines_view(game, reveal=True), "boom": cell, "win": 0, "balance": balance}
            opened.append(cell)
            game["opened"] = json.dumps(opened)
            if len(opened) == g.MINES_CELLS - game["mines"]:
                return await self._mines_cashout(c, game)
            await c.execute("UPDATE mines_games SET opened=? WHERE user_id=?", (game["opened"], user_id))
        return self._mines_view(game)

    async def mines_cashout(self, user_id: int) -> dict:
        async with self.db.tx() as c:
            async with c.execute("SELECT * FROM mines_games WHERE user_id=?", (user_id,)) as q:
                row = await q.fetchone()
            if not row:
                raise GameError("Нет активной игры")
            game = dict(row)
            if not json.loads(game["opened"]):
                raise GameError("Откройте хотя бы одну клетку")
            return await self._mines_cashout(c, game)

    async def _mines_cashout(self, c: aiosqlite.Connection, game: dict) -> dict:
        view = self._mines_view(game, reveal=True)
        win = view["cashout"]
        await c.execute("DELETE FROM mines_games WHERE user_id=?", (game["user_id"],))
        balance = await self._settle(
            c, game["user_id"], "mines", game["bet"], win,
            {"mines": game["mines"], "opened": len(view["opened"]), "multiplier": view["multiplier"]},
        )
        return {**view, "win": win, "balance": balance}

    # ---------- краш (общие раунды для всех игроков) ----------

    async def _crash_current(self, c: aiosqlite.Connection) -> dict | None:
        async with c.execute("SELECT * FROM crash_rounds ORDER BY id DESC LIMIT 1") as q:
            row = await q.fetchone()
        return dict(row) if row else None

    async def crash_tick(self, now: float | None = None) -> None:
        """Двигает раунд: приём ставок -> полёт -> краш -> пауза -> новый раунд. Вызывается ~10 раз в секунду."""
        now = time.time() if now is None else now
        async with self.db.tx() as c:
            rnd = await self._crash_current(c)
            if rnd is None or (rnd["status"] == "crashed" and now >= rnd["crashed_at"] + CRASH_PAUSE_SECONDS):
                await c.execute(
                    "INSERT INTO crash_rounds(point, status, betting_until, created_at) VALUES (?,?,?,?)",
                    (g.crash_point(), "betting", now + CRASH_BETTING_SECONDS, now),
                )
                return
            if rnd["status"] == "betting" and now >= rnd["betting_until"]:
                await c.execute(
                    "UPDATE crash_rounds SET status='running', started_at=? WHERE id=?", (rnd["betting_until"], rnd["id"])
                )
                return
            if rnd["status"] != "running":
                return
            point = rnd["point"]
            current = g.crash_multiplier_at(now - rnd["started_at"])
            # Автовыводы, которые сработали до краша
            async with c.execute(
                "SELECT * FROM crash_bets WHERE round_id=? AND cashout IS NULL AND auto IS NOT NULL "
                "AND auto < ? AND auto <= ?", (rnd["id"], point, current),
            ) as q:
                autos = [dict(r) for r in await q.fetchall()]
            for bet in autos:
                await self._crash_pay(c, rnd, bet, bet["auto"])
            if current >= point:
                crashed_at = rnd["started_at"] + g.crash_time_of(point)
                await c.execute(
                    "UPDATE crash_rounds SET status='crashed', crashed_at=? WHERE id=?", (min(now, crashed_at), rnd["id"])
                )
                async with c.execute(
                    "SELECT * FROM crash_bets WHERE round_id=? AND cashout IS NULL", (rnd["id"],)
                ) as q:
                    losers = [dict(r) for r in await q.fetchall()]
                for bet in losers:
                    await c.execute(
                        "UPDATE crash_bets SET win=0 WHERE round_id=? AND user_id=?", (rnd["id"], bet["user_id"])
                    )
                    await self.db.log_bet(
                        c, bet["user_id"], "crash", bet["bet"], 0,
                        json.dumps({"round": rnd["id"], "point": point, "cashout": None}),
                    )

    async def _crash_pay(self, c: aiosqlite.Connection, rnd: dict, bet: dict, multiplier: float) -> int:
        win = g.payout(bet["bet"], multiplier)
        await c.execute(
            "UPDATE crash_bets SET cashout=?, win=? WHERE round_id=? AND user_id=?",
            (multiplier, win, rnd["id"], bet["user_id"]),
        )
        balance = await self._settle(
            c, bet["user_id"], "crash", bet["bet"], win,
            {"round": rnd["id"], "point": rnd["point"], "cashout": multiplier},
        )
        return balance

    async def crash_bet(self, user_id: int, bet: Any, auto: Any = None) -> dict:
        bet = self._check_bet(bet)
        if auto is not None:
            if not isinstance(auto, (int, float)) or isinstance(auto, bool) or not 1.01 <= auto <= g.CRASH_MAX:
                raise GameError("Автовывод — от 1.01×")
            auto = round(float(auto), 2)
        now = time.time()
        async with self.db.tx() as c:
            rnd = await self._crash_current(c)
            if not rnd or rnd["status"] != "betting" or now >= rnd["betting_until"]:
                raise GameError("Ставки принимаются перед стартом раунда — дождитесь следующего")
            async with c.execute(
                "SELECT 1 FROM crash_bets WHERE round_id=? AND user_id=?", (rnd["id"], user_id)
            ) as q:
                if await q.fetchone():
                    raise GameError("Вы уже сделали ставку в этом раунде")
            balance = await self._take(c, user_id, bet, "crash")
            await c.execute(
                "INSERT INTO crash_bets(round_id, user_id, bet, auto, placed_at) VALUES (?,?,?,?,?)",
                (rnd["id"], user_id, bet, auto, now),
            )
        return {"round": rnd["id"], "bet": bet, "auto": auto, "balance": balance}

    async def crash_cashout(self, user_id: int) -> dict:
        now = time.time()
        async with self.db.tx() as c:
            rnd = await self._crash_current(c)
            if not rnd or rnd["status"] != "running":
                raise GameError("Раунд не идёт")
            async with c.execute(
                "SELECT * FROM crash_bets WHERE round_id=? AND user_id=? AND cashout IS NULL", (rnd["id"], user_id)
            ) as q:
                row = await q.fetchone()
            if not row:
                raise GameError("Нет активной ставки")
            current = g.crash_multiplier_at(now - rnd["started_at"])
            if current >= rnd["point"]:
                raise GameError("Не успели — ракета уже взорвалась")
            balance = await self._crash_pay(c, rnd, dict(row), current)
        return {"cashout": current, "win": g.payout(row["bet"], current), "bet": row["bet"], "balance": balance}

    async def crash_state(self, user_id: int | None = None) -> dict:
        now = time.time()
        rnd = await self.db.one("SELECT * FROM crash_rounds ORDER BY id DESC LIMIT 1")
        history = [r["point"] for r in await self.db.all(
            "SELECT point FROM crash_rounds WHERE status='crashed' ORDER BY id DESC LIMIT 20")]
        if not rnd:
            return {"round": None, "history": history, "growth": g.CRASH_GROWTH}
        rows = await self.db.all(
            "SELECT b.user_id, b.bet, b.auto, b.cashout, b.win, u.first_name, u.username FROM crash_bets b "
            "LEFT JOIN users u ON u.id=b.user_id WHERE b.round_id=? ORDER BY b.bet DESC",
            rnd["id"],
        )
        players = [
            {"id": r["user_id"], "name": display_name({"id": r["user_id"], **r}), "bet": r["bet"],
             "cashout": r["cashout"], "win": r["win"]}
            for r in rows
        ]
        mine = next((dict(r) for r in rows if r["user_id"] == user_id), None)
        view = {"id": rnd["id"], "phase": rnd["status"]}
        if rnd["status"] == "betting":
            view["betting_left"] = max(0.0, rnd["betting_until"] - now)
        elif rnd["status"] == "running":
            view["elapsed"] = now - rnd["started_at"]
            view["multiplier"] = g.crash_multiplier_at(view["elapsed"])
        else:
            view["point"] = rnd["point"]
            view["next_in"] = max(0.0, rnd["crashed_at"] + CRASH_PAUSE_SECONDS - now)
        return {
            "round": view,
            "players": players,
            "my": {"bet": mine["bet"], "auto": mine["auto"], "cashout": mine["cashout"], "win": mine["win"]}
            if mine else None,
            "history": history,
            "growth": g.CRASH_GROWTH,
        }

    # ---------- подарки игроков (прислали релейеру) ----------

    @staticmethod
    def gift_title(r: dict) -> str:
        return f"{r['collection_name']} #{r['number']}" if r.get("number") else (r.get("collection_name") or "Подарок")

    @classmethod
    def _gift_view(cls, r: dict, now: float) -> dict:
        fresh = bool(r["value"]) and now - (r["priced_at"] or 0) < NFT_PRICE_MAX_AGE
        return {"id": r["id"], "kind": r["kind"], "title": cls.gift_title(r), "model": r["model"],
                "emoji": r["emoji"] or "🎁", "rarity": r["rarity"], "value": r["value"], "priced": fresh,
                "status": r["status"], "sell": int(r["value"] * GIFT_SELL_RATE) if fresh else None,
                "locked_until": r["transfer_at"] if r["transfer_at"] > now else None}

    async def gifts(self, user_id: int) -> list[dict]:
        now = time.time()
        rows = await self.db.all("SELECT * FROM user_gifts WHERE user_id=? AND kind='nft' AND status IN "
                                 "('owned','staked','withdrawing') ORDER BY value DESC, id", user_id)
        return [self._gift_view(r, now) for r in rows]

    @staticmethod
    def _gift_ids(gifts: Any) -> list[int]:
        if not gifts:
            return []
        if not isinstance(gifts, list) or len(gifts) > MAX_GIFTS_PER_BET or not all(
                isinstance(i, int) and not isinstance(i, bool) for i in gifts):
            raise GameError("Некорректный список подарков")
        return sorted(set(gifts))

    async def _take_gifts(self, c: aiosqlite.Connection, user_id: int, ids: list[int], round_id: int) -> list[dict]:
        now = time.time()
        taken = []
        for gift_id in ids:
            async with c.execute("SELECT * FROM user_gifts WHERE id=? AND user_id=? AND kind='nft'",
                                 (gift_id, user_id)) as q:
                row = await q.fetchone()
            if not row or row["status"] != "owned":
                raise GameError("Этот подарок уже поставлен или выведен")
            if not row["value"] or now - (row["priced_at"] or 0) >= NFT_PRICE_MAX_AGE:
                raise GameError(f"Цена {self.gift_title(dict(row))} ещё проверяется на маркете — попробуйте позже")
            await c.execute("UPDATE user_gifts SET status='staked', round_id=? WHERE id=?", (round_id, gift_id))
            taken.append({**dict(row), "title": self.gift_title(dict(row))})
        return taken

    async def gift_sell(self, user_id: int, gift_id: Any) -> dict:
        """Казино выкупает NFT игрока за GIFT_SELL_RATE от пола маркета — звёзды можно ставить в любые игры."""
        if not isinstance(gift_id, int) or isinstance(gift_id, bool):
            raise GameError("Подарок не найден")
        now = time.time()
        async with self.db.tx() as c:
            async with c.execute("SELECT * FROM user_gifts WHERE id=? AND user_id=? AND kind='nft'",
                                 (gift_id, user_id)) as q:
                row = await q.fetchone()
            if not row or row["status"] != "owned":
                raise GameError("Этот подарок уже поставлен или выведен")
            if not row["value"] or now - (row["priced_at"] or 0) >= NFT_PRICE_MAX_AGE:
                raise GameError("Цена подарка ещё проверяется на маркете — попробуйте позже")
            amount = int(row["value"] * GIFT_SELL_RATE)
            await c.execute("UPDATE user_gifts SET status='sold' WHERE id=?", (gift_id,))
            balance = await self.db.change_balance(c, user_id, amount, "gift_sell", str(gift_id))
        return {"amount": amount, "balance": balance}

    async def gift_withdraw_claim(self, user_id: int, gift_id: Any) -> dict:
        if not isinstance(gift_id, int) or isinstance(gift_id, bool):
            raise GameError("Подарок не найден")
        async with self.db.tx() as c:
            async with c.execute("SELECT * FROM user_gifts WHERE id=? AND user_id=? AND kind='nft'",
                                 (gift_id, user_id)) as q:
                row = await q.fetchone()
            if not row or row["status"] != "owned":
                raise GameError("Этот подарок уже поставлен или выведен")
            if row["transfer_at"] > time.time():
                raise GameError("Telegram пока не даёт передать этот подарок — попробуйте позже")
            await c.execute("UPDATE user_gifts SET status='withdrawing' WHERE id=?", (gift_id,))
        return dict(row)

    async def gift_withdraw_finish(self, gift_id: int, ok: bool) -> None:
        async with self.db.tx() as c:
            await c.execute("UPDATE user_gifts SET status=? WHERE id=? AND status='withdrawing'",
                            ("withdrawn" if ok else "owned", gift_id))

    # ---------- PvP-рулетка ----------

    @staticmethod
    def _pvp_game(game: Any) -> str:
        if game not in PVP_GAMES:
            raise GameError("Неизвестная игра")
        return game

    async def _open_round(self, c: aiosqlite.Connection, game: str) -> dict:
        async with c.execute(
            "SELECT * FROM pvp_rounds WHERE status='open' AND game=? ORDER BY id DESC LIMIT 1", (game,)
        ) as q:
            row = await q.fetchone()
        if row:
            return dict(row)
        cur = await c.execute("INSERT INTO pvp_rounds(game, created_at) VALUES (?, ?)", (game, time.time()))
        async with c.execute("SELECT * FROM pvp_rounds WHERE id=?", (cur.lastrowid,)) as q:
            return dict(await q.fetchone())

    async def pvp_bet(self, user_id: int, amount: Any, game: Any = "roulette", gifts: Any = None) -> dict:
        """Ставка звёздами и/или NFT-подарками (подарок идёт в банк по цене пола маркета)."""
        game = self._pvp_game(game)
        gift_ids = self._gift_ids(gifts)
        if gift_ids and amount in (None, 0):
            amount = 0
        else:
            amount = self._check_bet(amount)
        now = time.time()
        async with self.db.tx() as c:
            rnd = await self._open_round(c, game)
            if rnd["ends_at"] and now >= rnd["ends_at"] - 1:
                raise GameError("Раунд уже крутится, подождите следующий")
            stake = amount
            staked = []
            if gift_ids:
                for gift in await self._take_gifts(c, user_id, gift_ids, rnd["id"]):
                    stake += gift["value"]
                    staked.append({"emoji": gift["emoji"], "title": gift["title"], "value": gift["value"]})
            if amount:
                await self._take(c, user_id, amount, game)
            async with c.execute("SELECT gifts FROM pvp_bets WHERE round_id=? AND user_id=?",
                                 (rnd["id"], user_id)) as q:
                prev = await q.fetchone()
            all_gifts = (json.loads(prev["gifts"]) if prev and prev["gifts"] else []) + staked
            await c.execute(
                "INSERT INTO pvp_bets(round_id, user_id, amount, joined, gifts) VALUES (?,?,?,?,?) "
                "ON CONFLICT(round_id, user_id) DO UPDATE SET amount = amount + excluded.amount, gifts=excluded.gifts",
                (rnd["id"], user_id, stake, now, json.dumps(all_gifts, ensure_ascii=False) if all_gifts else None),
            )
            await c.execute("UPDATE pvp_rounds SET pot = pot + ? WHERE id=?", (stake, rnd["id"]))
            async with c.execute("SELECT COUNT(*) n FROM pvp_bets WHERE round_id=?", (rnd["id"],)) as q:
                players = (await q.fetchone())["n"]
            if players >= 2 and not rnd["ends_at"]:
                await c.execute("UPDATE pvp_rounds SET ends_at=? WHERE id=?", (now + PVP_ROUND_SECONDS, rnd["id"]))
        return await self.pvp_state(user_id, game)

    async def pvp_tick(self) -> list[dict]:
        """Завершает раунды, у которых вышло время. Возвращает итоги для уведомлений."""
        now = time.time()
        results = []
        async with self.db.tx() as c:
            async with c.execute("SELECT * FROM pvp_rounds WHERE status='open'") as q:
                rounds = [dict(r) for r in await q.fetchall()]
            for rnd in rounds:
                async with c.execute(
                    "SELECT user_id, amount, joined FROM pvp_bets WHERE round_id=? ORDER BY joined", (rnd["id"],)
                ) as q:
                    bets = [dict(r) for r in await q.fetchall()]
                if rnd["ends_at"] and now >= rnd["ends_at"] and len(bets) >= 2:
                    winner, ticket = g.pvp_pick_winner([(b["user_id"], b["amount"]) for b in bets])
                    pot = sum(b["amount"] for b in bets)
                    prize = g.pvp_payout(pot)
                    # Подарки из банка целиком уходят победителю, звёзды — остаток выигрыша
                    async with c.execute("SELECT COALESCE(SUM(value),0) v FROM user_gifts WHERE round_id=? "
                                         "AND status='staked'", (rnd["id"],)) as q:
                        gifts_value = (await q.fetchone())["v"]
                    await c.execute("UPDATE user_gifts SET user_id=?, status='owned', round_id=NULL "
                                    "WHERE round_id=? AND status='staked'", (winner, rnd["id"]))
                    stars_prize = max(0, prize - gifts_value)
                    detail = None
                    if rnd["game"] == "hockey":
                        zones = g.hockey_zones([b["amount"] for b in bets])
                        winner_idx = next(i for i, b in enumerate(bets) if b["user_id"] == winner)
                        detail = {"zones": [list(z) for z in zones], **g.hockey_shot(zones[winner_idx])}
                    if stars_prize:
                        await self.db.change_balance(c, winner, stars_prize, "pvp_win", str(rnd["id"]))
                    prize = stars_prize + gifts_value
                    for b in bets:
                        win = prize if b["user_id"] == winner else 0
                        game_name = "hockey" if rnd["game"] == "hockey" else "pvp"
                        await self.db.log_bet(c, b["user_id"], game_name, b["amount"], win, json.dumps({"round": rnd["id"]}))
                    await c.execute(
                        "UPDATE pvp_rounds SET status='done', winner_id=?, pot=?, payout=?, ticket=?, finished_at=?, "
                        "detail=? WHERE id=?",
                        (winner, pot, prize, ticket, now, json.dumps(detail) if detail else None, rnd["id"]),
                    )
                    results.append({"round": rnd["id"], "game": rnd["game"], "winner": winner, "pot": pot,
                                    "payout": prize, "stars": stars_prize, "gifts_value": gifts_value,
                                    "players": [b["user_id"] for b in bets]})
                elif len(bets) == 1 and not rnd["ends_at"] and now - bets[0]["joined"] > PVP_IDLE_REFUND:
                    async with c.execute("SELECT COALESCE(SUM(value),0) v FROM user_gifts WHERE round_id=? "
                                         "AND status='staked'", (rnd["id"],)) as q:
                        gifts_value = (await q.fetchone())["v"]
                    await c.execute("UPDATE user_gifts SET status='owned', round_id=NULL "
                                    "WHERE round_id=? AND status='staked'", (rnd["id"],))
                    if bets[0]["amount"] > gifts_value:
                        await self.db.change_balance(c, bets[0]["user_id"], bets[0]["amount"] - gifts_value,
                                                     "pvp_refund", str(rnd["id"]))
                    await c.execute(
                        "UPDATE pvp_rounds SET status='refunded', finished_at=? WHERE id=?", (now, rnd["id"])
                    )
        return results

    async def _round_players(self, round_id: int) -> list[dict]:
        rows = await self.db.all(
            "SELECT b.user_id, b.amount, b.gifts, u.first_name, u.username FROM pvp_bets b "
            "LEFT JOIN users u ON u.id=b.user_id WHERE b.round_id=? ORDER BY b.joined",
            round_id,
        )
        total = sum(r["amount"] for r in rows) or 1
        return [
            {"id": r["user_id"], "name": display_name({"id": r["user_id"], **r}), "amount": r["amount"],
             "chance": round(r["amount"] / total * 100, 2), "gifts": json.loads(r["gifts"]) if r["gifts"] else []}
            for r in rows
        ]

    async def pvp_state(self, user_id: int | None = None, game: Any = "roulette") -> dict:
        game = self._pvp_game(game)
        now = time.time()
        rnd = await self.db.one(
            "SELECT * FROM pvp_rounds WHERE status='open' AND game=? ORDER BY id DESC LIMIT 1", game
        )
        current = None
        if rnd:
            players = await self._round_players(rnd["id"])
            current = {
                "id": rnd["id"],
                "pot": rnd["pot"],
                "ends_in": max(0.0, rnd["ends_at"] - now) if rnd["ends_at"] else None,
                "players": players,
                "my_bet": next((p["amount"] for p in players if p["id"] == user_id), 0),
            }
        last = await self.db.one(
            "SELECT * FROM pvp_rounds WHERE status='done' AND game=? ORDER BY id DESC LIMIT 1", game
        )
        last_view = None
        if last:
            players = await self._round_players(last["id"])
            winner = next((p for p in players if p["id"] == last["winner_id"]), None)
            last_view = {
                "id": last["id"], "pot": last["pot"], "payout": last["payout"], "ticket": last["ticket"],
                "winner": winner, "players": players, "finished_ago": now - (last["finished_at"] or now),
                "detail": json.loads(last["detail"]) if last["detail"] else None,
            }
        return {"game": game, "round": current, "last": last_view, "commission": g.PVP_COMMISSION,
                "round_seconds": PVP_ROUND_SECONDS}
