"""Математика игр. Всё случайное — только на сервере через криптостойкий генератор.

Функции чистые: генератор случайных чисел передаётся параметром, чтобы их можно было тестировать.
"""
from __future__ import annotations

import math
import random
import secrets
from dataclasses import dataclass
from typing import Sequence

RNG = secrets.SystemRandom()

HOUSE_EDGE = 0.03  # 3% — для кубика, мин и краша


# ---------------- Слоты ----------------

SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "🔔", "⭐", "7️⃣"]
SLOT_WEIGHTS = [15, 25, 22, 18, 12, 8]
# Три одинаковых символа -> множитель
SLOT_TRIPLE = {"🍒": 4, "🍋": 7, "🍇": 15, "🔔": 30, "⭐": 55, "7️⃣": 180}
# Ровно две вишни -> ×3, одна вишня -> возврат 40% ставки. RTP ≈ 94.7%
SLOT_TWO_CHERRIES = 3
SLOT_ONE_CHERRY = 0.4


def slots_multiplier(reels: Sequence[str]) -> float:
    if reels[0] == reels[1] == reels[2]:
        return SLOT_TRIPLE[reels[0]]
    cherries = sum(1 for s in reels if s == "🍒")
    if cherries == 2:
        return SLOT_TWO_CHERRIES
    if cherries == 1:
        return SLOT_ONE_CHERRY
    return 0.0


def slots_spin(rng: random.Random = RNG) -> tuple[list[str], float]:
    reels = rng.choices(SLOT_SYMBOLS, weights=SLOT_WEIGHTS, k=3)
    return reels, slots_multiplier(reels)


def slots_rtp() -> float:
    """Точный возврат игроку (RTP) полным перебором."""
    total = sum(SLOT_WEIGHTS)
    probs = {s: w / total for s, w in zip(SLOT_SYMBOLS, SLOT_WEIGHTS)}
    rtp = 0.0
    for a in SLOT_SYMBOLS:
        for b in SLOT_SYMBOLS:
            for c in SLOT_SYMBOLS:
                rtp += probs[a] * probs[b] * probs[c] * slots_multiplier([a, b, c])
    return rtp


# ---------------- Кости (Dice) ----------------

DICE_MIN_CHANCE = 1
DICE_MAX_CHANCE = 95


def dice_multiplier(chance: int) -> float:
    return round((1 - HOUSE_EDGE) * 100 / chance, 4)


def dice_roll(chance: int, rng: random.Random = RNG) -> tuple[float, bool, float]:
    """Выпадает число 0.00–99.99; выигрыш, если оно меньше шанса."""
    if not DICE_MIN_CHANCE <= chance <= DICE_MAX_CHANCE:
        raise ValueError("chance")
    roll = rng.randrange(10000) / 100
    win = roll < chance
    return roll, win, dice_multiplier(chance) if win else 0.0


# ---------------- Рулетка (европейская, один ноль) ----------------

RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
ROULETTE_BETS = {"red", "black", "even", "odd", "low", "high", "dozen1", "dozen2", "dozen3", "number"}


def roulette_color(n: int) -> str:
    if n == 0:
        return "green"
    return "red" if n in RED_NUMBERS else "black"


def roulette_multiplier(bet_type: str, value: int | None, n: int) -> float:
    """Множитель выплаты (включая ставку) или 0."""
    if bet_type == "number":
        return 36.0 if value == n else 0.0
    if n == 0:
        return 0.0
    wins = {
        "red": n in RED_NUMBERS,
        "black": n not in RED_NUMBERS,
        "even": n % 2 == 0,
        "odd": n % 2 == 1,
        "low": n <= 18,
        "high": n >= 19,
        "dozen1": n <= 12,
        "dozen2": 13 <= n <= 24,
        "dozen3": n >= 25,
    }
    if bet_type not in wins:
        raise ValueError("bet_type")
    if not wins[bet_type]:
        return 0.0
    return 3.0 if bet_type.startswith("dozen") else 2.0


def roulette_spin(rng: random.Random = RNG) -> int:
    return rng.randrange(37)


# ---------------- Мины ----------------

MINES_CELLS = 25


def mines_multiplier(mines: int, opened: int) -> float:
    """Множитель после `opened` безопасных клеток при `mines` минах на поле 5×5."""
    if opened == 0:
        return 1.0
    fair = 1.0
    for i in range(opened):
        fair *= (MINES_CELLS - i) / (MINES_CELLS - mines - i)
    return round((1 - HOUSE_EDGE) * fair, 4)


def mines_place(mines: int, rng: random.Random = RNG) -> list[int]:
    if not 1 <= mines <= MINES_CELLS - 1:
        raise ValueError("mines")
    return sorted(rng.sample(range(MINES_CELLS), mines))


# ---------------- Краш ----------------

CRASH_GROWTH = 0.07   # множитель растёт как e^(0.07·t), t в секундах
CRASH_MAX = 1000.0


def crash_point(rng: random.Random = RNG) -> float:
    """Точка краша: P(краш ≥ x) = 0.97 / x. Около 3% раундов падают сразу на 1.00."""
    u = rng.random()
    point = (1 - HOUSE_EDGE) / (1 - u)
    point = math.floor(point * 100) / 100
    return max(1.0, min(point, CRASH_MAX))


def crash_multiplier_at(seconds: float) -> float:
    return math.floor(math.exp(CRASH_GROWTH * max(0.0, seconds)) * 100) / 100


def crash_time_of(multiplier: float) -> float:
    return math.log(max(multiplier, 1.0)) / CRASH_GROWTH


# ---------------- Кейсы ----------------

@dataclass(frozen=True)
class Case:
    id: str
    name: str
    emoji: str
    price: int
    prizes: tuple[tuple[int, int], ...]  # (звёзды, вес)

    def expected_value(self) -> float:
        total = sum(w for _, w in self.prizes)
        return sum(p * w for p, w in self.prizes) / total


CASES = [
    Case("bronze", "Бронзовый", "🥉", 10, ((1, 40), (5, 30), (10, 15), (20, 10), (50, 4), (100, 1))),
    Case("silver", "Серебряный", "🥈", 50, ((10, 40), (25, 28), (50, 17), (100, 10), (250, 4), (500, 1))),
    Case("gold", "Золотой", "🥇", 200, ((50, 40), (100, 28), (200, 17), (400, 10), (1000, 4), (2500, 1))),
    Case("diamond", "Алмазный", "💎", 1000, ((250, 42), (500, 28), (1000, 16), (2000, 9), (5000, 4), (15000, 1))),
]
CASES_BY_ID = {c.id: c for c in CASES}


def case_open(case: Case, rng: random.Random = RNG) -> int:
    values = [p for p, _ in case.prizes]
    weights = [w for _, w in case.prizes]
    return rng.choices(values, weights=weights, k=1)[0]


# ---------------- PvP-рулетка ----------------

PVP_COMMISSION = 0.05


def pvp_pick_winner(bets: Sequence[tuple[int, int]], rng: random.Random = RNG) -> tuple[int, int]:
    """bets: (user_id, сумма). Шанс пропорционален ставке. Возвращает (победитель, билет)."""
    total = sum(a for _, a in bets)
    if total <= 0:
        raise ValueError("empty pot")
    ticket = rng.randrange(total)
    acc = 0
    for user_id, amount in bets:
        acc += amount
        if ticket < acc:
            return user_id, ticket
    raise AssertionError("unreachable")


def pvp_payout(pot: int) -> int:
    return pot - math.ceil(pot * PVP_COMMISSION)


def payout(bet: int, multiplier: float) -> int:
    """Выплата в целых звёздах, всегда округляется вниз."""
    return int(math.floor(bet * multiplier + 1e-9))
