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

HOUSE_EDGE = 0.01  # 1% — как на крупных площадках: кости, мины, краш


# ---------------- Слоты ----------------

SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "🔔", "⭐", "7️⃣", "💎"]
SLOT_WEIGHTS = [28, 22, 18, 13, 9, 6, 4]
# Три одинаковых символа -> множитель. 💎💎💎 — джекпот ×1000 (примерно 1 раз на 15 600 спинов)
SLOT_TRIPLE = {"🍒": 4, "🍋": 8, "🍇": 15, "🔔": 30, "⭐": 60, "7️⃣": 250, "💎": 1000}
SLOT_TWO_DIAMONDS = 10   # два 💎 в любом месте
SLOT_TWO_CHERRIES = 2.5  # две 🍒 в любом месте
# RTP ≈ 95.8%, выигрышных спинов ≈ 21.6%


def slots_multiplier(reels: Sequence[str]) -> float:
    if reels[0] == reels[1] == reels[2]:
        return SLOT_TRIPLE[reels[0]]
    if sum(1 for s in reels if s == "💎") == 2:
        return SLOT_TWO_DIAMONDS
    if sum(1 for s in reels if s == "🍒") == 2:
        return SLOT_TWO_CHERRIES
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

DICE_MIN_CHANCE = 0.01
DICE_MAX_CHANCE = 98.0


def dice_valid_chance(chance: float) -> bool:
    return DICE_MIN_CHANCE <= chance <= DICE_MAX_CHANCE and round(chance, 2) == chance


def dice_multiplier(chance: float) -> float:
    """Шанс 50% -> ×1.98, 1% -> ×99, 0.01% -> ×9900."""
    return math.floor((1 - HOUSE_EDGE) * 100 / chance * 10000) / 10000


def dice_roll(chance: float, over: bool = False, rng: random.Random = RNG) -> tuple[float, bool, float]:
    """Выпадает число 0.00–99.99. «Меньше»: выигрыш при roll < шанс; «больше»: при roll ≥ 100 − шанс."""
    if not dice_valid_chance(chance):
        raise ValueError("chance")
    roll = rng.randrange(10000) / 100
    win = roll >= round(100 - chance, 2) if over else roll < chance
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
CRASH_MAX = 10000.0


def crash_point(rng: random.Random = RNG) -> float:
    """Точка краша: P(краш ≥ x) = 0.99 / x. Около 1% раундов падают сразу на 1.00."""
    u = rng.random()
    point = (1 - HOUSE_EDGE) / (1 - u)
    point = math.floor(point * 100) / 100
    return max(1.0, min(point, CRASH_MAX))


def crash_multiplier_at(seconds: float) -> float:
    return math.floor(math.exp(CRASH_GROWTH * max(0.0, seconds)) * 100) / 100


def crash_time_of(multiplier: float) -> float:
    return math.log(max(multiplier, 1.0)) / CRASH_GROWTH


# ---------------- Кейсы ----------------

# Подарки в кейсах: (множитель от цены кейса, вес, подарок). RTP ≈ 90.6%, джекпот ×100
CASE_TIERS = (
    (0.2, 960, "🌹"),
    (0.5, 560, "🧸"),
    (1, 280, "🎁"),
    (2, 130, "💝"),
    (5, 56, "🍾"),
    (20, 11, "🏆"),
    (100, 3, "💍"),
)


@dataclass(frozen=True)
class Case:
    id: str
    name: str
    emoji: str
    price: int

    @property
    def prizes(self) -> tuple[tuple[int, int, str], ...]:
        """(звёзды, вес, подарок)."""
        return tuple((max(1, round(self.price * m)), w, gift) for m, w, gift in CASE_TIERS)

    def expected_value(self) -> float:
        total = sum(w for _, w, _ in self.prizes)
        return sum(p * w for p, w, _ in self.prizes) / total


CASES = [
    Case("bear", "Мишка", "🧸", 25),
    Case("heart", "Сердечко", "💝", 100),
    Case("champagne", "Шампанское", "🍾", 500),
    Case("ring", "Кольцо", "💍", 2500),
]
CASES_BY_ID = {c.id: c for c in CASES}


def case_open(case: Case, rng: random.Random = RNG) -> tuple[int, str]:
    """Возвращает (звёзды, подарок)."""
    prizes = case.prizes
    prize = rng.choices(prizes, weights=[w for _, w, _ in prizes], k=1)[0]
    return prize[0], prize[2]


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
