"""Игровые операции поверх базы. Каждая операция — одна транзакция, поэтому двойных выплат не бывает."""
from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any

import aiosqlite

from .config import Config
from .db import Database, InsufficientFunds
from .games import logic as g

PVP_ROUND_SECONDS = 30      # сколько длится раунд после второго игрока
PVP_IDLE_REFUND = 600       # одиночную ставку возвращаем через 10 минут
CHECK_CODE_RE = re.compile(r"^[A-Za-z0-9]{6,32}$")


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

    # ---------- слоты ----------

    async def slots(self, user_id: int, bet: Any) -> dict:
        bet = self._check_bet(bet)
        reels, mult = g.slots_spin()
        win = g.payout(bet, mult)
        async with self.db.tx() as c:
            await self._take(c, user_id, bet, "slots")
            balance = await self._settle(c, user_id, "slots", bet, win, {"reels": reels})
        return {"reels": reels, "multiplier": mult, "win": win, "balance": balance}

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

    # ---------- рулетка ----------

    async def roulette(self, user_id: int, bet: Any, bet_type: Any, value: Any = None) -> dict:
        bet = self._check_bet(bet)
        if bet_type not in g.ROULETTE_BETS:
            raise GameError("Неизвестный тип ставки")
        if bet_type == "number":
            if not isinstance(value, int) or not 0 <= value <= 36:
                raise GameError("Число — от 0 до 36")
        else:
            value = None
        number = g.roulette_spin()
        mult = g.roulette_multiplier(bet_type, value, number)
        win = g.payout(bet, mult)
        async with self.db.tx() as c:
            await self._take(c, user_id, bet, "roulette")
            balance = await self._settle(
                c, user_id, "roulette", bet, win, {"number": number, "type": bet_type, "value": value}
            )
        return {"number": number, "color": g.roulette_color(number), "multiplier": mult,
                "win": win, "balance": balance}

    # ---------- кейсы ----------

    async def open_case(self, user_id: int, case_id: Any) -> dict:
        case = g.CASES_BY_ID.get(case_id) if isinstance(case_id, str) else None
        if not case:
            raise GameError("Кейс не найден")
        prize, gift = g.case_open(case)
        async with self.db.tx() as c:
            await self._take(c, user_id, case.price, "case")
            balance = await self._settle(
                c, user_id, "case", case.price, prize, {"case": case.id, "prize": prize, "gift": gift}
            )
        return {"case": case.id, "prize": prize, "gift": gift, "balance": balance}

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
        if not isinstance(mines, int) or not 1 <= mines <= 24:
            raise GameError("Мин — от 1 до 24")
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

    # ---------- краш ----------

    async def crash_start(self, user_id: int, bet: Any, auto: Any = None) -> dict:
        bet = self._check_bet(bet)
        if auto is not None:
            if not isinstance(auto, (int, float)) or isinstance(auto, bool) or not 1.01 <= auto <= g.CRASH_MAX:
                raise GameError("Автовывод — от 1.01×")
            auto = round(float(auto), 2)
        point = g.crash_point()
        now = time.time()
        async with self.db.tx() as c:
            async with c.execute("SELECT * FROM crash_games WHERE user_id=?", (user_id,)) as q:
                existing = await q.fetchone()
            if existing:
                result = await self._crash_resolve(c, dict(existing), now)
                if result["status"] == "running":
                    raise GameError("Сначала закончите текущую игру")
            balance = await self._take(c, user_id, bet, "crash")
            await c.execute(
                "INSERT INTO crash_games(user_id, bet, point, auto, started_at) VALUES (?,?,?,?,?)",
                (user_id, bet, point, auto, now),
            )
        return {"status": "running", "bet": bet, "auto": auto, "multiplier": 1.0, "elapsed": 0.0,
                "growth": g.CRASH_GROWTH, "balance": balance}

    async def _crash_resolve(self, c: aiosqlite.Connection, game: dict, now: float, cashout: bool = False) -> dict:
        """Определяет исход игры на момент now; завершённую игру удаляет и рассчитывает."""
        elapsed = now - game["started_at"]
        current = g.crash_multiplier_at(elapsed)
        point = game["point"]
        auto = game["auto"]
        base = {"bet": game["bet"], "auto": auto, "elapsed": elapsed, "growth": g.CRASH_GROWTH}

        win_at = None
        if auto is not None and auto < point and current >= auto:
            win_at = auto
        elif current >= point:
            win_at = None
            status = "crashed"
        elif cashout:
            win_at = current
        else:
            return {**base, "status": "running", "multiplier": current}

        if win_at is not None:
            win = g.payout(game["bet"], win_at)
            status = "cashed"
        else:
            win = 0
        await c.execute("DELETE FROM crash_games WHERE user_id=?", (game["user_id"],))
        balance = await self._settle(
            c, game["user_id"], "crash", game["bet"], win, {"point": point, "cashout": win_at}
        )
        return {**base, "status": status, "multiplier": win_at or point, "point": point,
                "win": win, "balance": balance}

    async def crash_state(self, user_id: int) -> dict:
        async with self.db.tx() as c:
            async with c.execute("SELECT * FROM crash_games WHERE user_id=?", (user_id,)) as q:
                row = await q.fetchone()
            if not row:
                return {"status": "idle"}
            return await self._crash_resolve(c, dict(row), time.time())

    async def crash_cashout(self, user_id: int) -> dict:
        async with self.db.tx() as c:
            async with c.execute("SELECT * FROM crash_games WHERE user_id=?", (user_id,)) as q:
                row = await q.fetchone()
            if not row:
                raise GameError("Нет активной игры")
            return await self._crash_resolve(c, dict(row), time.time(), cashout=True)

    async def crash_sweep(self) -> int:
        """Рассчитывает брошенные игры (игрок закрыл приложение)."""
        now = time.time()
        rows = await self.db.all("SELECT * FROM crash_games")
        done = 0
        for game in rows:
            target = game["point"]
            if game["auto"] is not None and game["auto"] < game["point"]:
                target = game["auto"]
            if now - game["started_at"] < g.crash_time_of(target) + 2:
                continue
            async with self.db.tx() as c:
                async with c.execute("SELECT * FROM crash_games WHERE user_id=?", (game["user_id"],)) as q:
                    row = await q.fetchone()
                if row:
                    await self._crash_resolve(c, dict(row), now)
                    done += 1
        return done

    # ---------- PvP-рулетка ----------

    async def _open_round(self, c: aiosqlite.Connection) -> dict:
        async with c.execute("SELECT * FROM pvp_rounds WHERE status='open' ORDER BY id DESC LIMIT 1") as q:
            row = await q.fetchone()
        if row:
            return dict(row)
        cur = await c.execute("INSERT INTO pvp_rounds(created_at) VALUES (?)", (time.time(),))
        async with c.execute("SELECT * FROM pvp_rounds WHERE id=?", (cur.lastrowid,)) as q:
            return dict(await q.fetchone())

    async def pvp_bet(self, user_id: int, amount: Any) -> dict:
        amount = self._check_bet(amount)
        now = time.time()
        async with self.db.tx() as c:
            rnd = await self._open_round(c)
            if rnd["ends_at"] and now >= rnd["ends_at"] - 1:
                raise GameError("Раунд уже крутится, подождите следующий")
            await self._take(c, user_id, amount, "pvp")
            await c.execute(
                "INSERT INTO pvp_bets(round_id, user_id, amount, joined) VALUES (?,?,?,?) "
                "ON CONFLICT(round_id, user_id) DO UPDATE SET amount = amount + excluded.amount",
                (rnd["id"], user_id, amount, now),
            )
            await c.execute("UPDATE pvp_rounds SET pot = pot + ? WHERE id=?", (amount, rnd["id"]))
            async with c.execute("SELECT COUNT(*) n FROM pvp_bets WHERE round_id=?", (rnd["id"],)) as q:
                players = (await q.fetchone())["n"]
            if players >= 2 and not rnd["ends_at"]:
                await c.execute("UPDATE pvp_rounds SET ends_at=? WHERE id=?", (now + PVP_ROUND_SECONDS, rnd["id"]))
        return await self.pvp_state(user_id)

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
                    await self.db.change_balance(c, winner, prize, "pvp_win", str(rnd["id"]))
                    for b in bets:
                        win = prize if b["user_id"] == winner else 0
                        await self.db.log_bet(c, b["user_id"], "pvp", b["amount"], win, json.dumps({"round": rnd["id"]}))
                    await c.execute(
                        "UPDATE pvp_rounds SET status='done', winner_id=?, pot=?, payout=?, ticket=?, finished_at=? WHERE id=?",
                        (winner, pot, prize, ticket, now, rnd["id"]),
                    )
                    results.append({"round": rnd["id"], "winner": winner, "pot": pot, "payout": prize,
                                    "players": [b["user_id"] for b in bets]})
                elif len(bets) == 1 and not rnd["ends_at"] and now - bets[0]["joined"] > PVP_IDLE_REFUND:
                    await self.db.change_balance(c, bets[0]["user_id"], bets[0]["amount"], "pvp_refund", str(rnd["id"]))
                    await c.execute(
                        "UPDATE pvp_rounds SET status='refunded', finished_at=? WHERE id=?", (now, rnd["id"])
                    )
        return results

    async def _round_players(self, round_id: int) -> list[dict]:
        rows = await self.db.all(
            "SELECT b.user_id, b.amount, u.first_name, u.username FROM pvp_bets b "
            "LEFT JOIN users u ON u.id=b.user_id WHERE b.round_id=? ORDER BY b.joined",
            round_id,
        )
        total = sum(r["amount"] for r in rows) or 1
        return [
            {"id": r["user_id"], "name": display_name({"id": r["user_id"], **r}), "amount": r["amount"],
             "chance": round(r["amount"] / total * 100, 2)}
            for r in rows
        ]

    async def pvp_state(self, user_id: int | None = None) -> dict:
        now = time.time()
        rnd = await self.db.one("SELECT * FROM pvp_rounds WHERE status='open' ORDER BY id DESC LIMIT 1")
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
        last = await self.db.one("SELECT * FROM pvp_rounds WHERE status='done' ORDER BY id DESC LIMIT 1")
        last_view = None
        if last:
            players = await self._round_players(last["id"])
            winner = next((p for p in players if p["id"] == last["winner_id"]), None)
            last_view = {
                "id": last["id"], "pot": last["pot"], "payout": last["payout"], "ticket": last["ticket"],
                "winner": winner, "players": players, "finished_ago": now - (last["finished_at"] or now),
            }
        return {"round": current, "last": last_view, "commission": g.PVP_COMMISSION,
                "round_seconds": PVP_ROUND_SECONDS}
