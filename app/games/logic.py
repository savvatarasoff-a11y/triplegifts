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

HOUSE_EDGE = 0.05  # комиссия казино в костях, минах и краше


# ---------------- Слоты ----------------

SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "🔔", "⭐", "7️⃣", "💎"]
SLOT_WEIGHTS = [22, 22, 19, 15, 11, 7, 4]
# Три одинаковых символа -> множитель. 💎💎💎 — джекпот ×1000 (примерно 1 раз на 15 600 спинов)
SLOT_TRIPLE = {"🍒": 5, "🍋": 10, "🍇": 20, "🔔": 30, "⭐": 60, "7️⃣": 250, "💎": 1000}
SLOT_TWO_DIAMONDS = 10   # два 💎 в любом месте
SLOT_TWO_CHERRIES = 2    # две 🍒 в любом месте
# RTP ≈ 90%, выигрышных спинов ≈ 15%


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
DICE_MAX_CHANCE = 90.0


def dice_valid_chance(chance: float) -> bool:
    return DICE_MIN_CHANCE <= chance <= DICE_MAX_CHANCE and round(chance, 2) == chance


def dice_multiplier(chance: float) -> float:
    """Шанс 50% -> ×1.9, 1% -> ×95, 0.01% -> ×9500."""
    return math.floor((1 - HOUSE_EDGE) * 100 / chance * 10000) / 10000


def dice_roll(chance: float, over: bool = False, rng: random.Random = RNG) -> tuple[float, bool, float]:
    """Выпадает число 0.00–99.99. «Меньше»: выигрыш при roll < шанс; «больше»: при roll ≥ 100 − шанс."""
    if not dice_valid_chance(chance):
        raise ValueError("chance")
    roll = rng.randrange(10000) / 100
    win = roll >= round(100 - chance, 2) if over else roll < chance
    return roll, win, dice_multiplier(chance) if win else 0.0


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


MINES_MIN = 2   # с одной миной первый клик давал бы множитель меньше ×1


def mines_place(mines: int, rng: random.Random = RNG) -> list[int]:
    if not MINES_MIN <= mines <= MINES_CELLS - 1:
        raise ValueError("mines")
    return sorted(rng.sample(range(MINES_CELLS), mines))


# ---------------- Краш ----------------

CRASH_GROWTH = 0.07   # множитель растёт как e^(0.07·t), t в секундах
CRASH_MAX = 10000.0


def crash_point(rng: random.Random = RNG) -> float:
    """Точка краша: P(краш ≥ x) = 0.95 / x. Около 6% раундов (1 − 0.95/1.01) падают сразу на 1.00."""
    u = rng.random()
    point = (1 - HOUSE_EDGE) / (1 - u)
    point = math.floor(point * 100) / 100
    return max(1.0, min(point, CRASH_MAX))


def crash_multiplier_at(seconds: float) -> float:
    return math.floor(math.exp(CRASH_GROWTH * max(0.0, seconds)) * 100) / 100


def crash_time_of(multiplier: float) -> float:
    return math.log(max(multiplier, 1.0)) / CRASH_GROWTH


# ---------------- Кейсы ----------------

# Подарки в кейсах: (множитель от цены кейса, вес, подарок). RTP ≈ 87%, джекпот ×100
CASE_TIERS = (
    (0.2, 1000, "🌹"),
    (0.5, 560, "🧸"),
    (1, 270, "🎁"),
    (2, 120, "💝"),
    (5, 52, "🍾"),
    (20, 10, "🏆"),
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


# ---------------- PvP-хоккей ----------------
# Поле 100×160 делится на горизонтальные зоны игроков пропорционально ставкам.
# Победитель выбирается так же, как в PvP-рулетке (шанс = доля ставки), затем строится
# честная траектория удара: шайба летит из центра по прямой, отражаясь от бортов,
# и останавливается в случайной точке зоны победителя. Длина пути — не меньше 1.5 длины поля.

HOCKEY_W = 100.0
HOCKEY_H = 160.0
HOCKEY_START = (50.0, 80.0)
HOCKEY_MIN_PATH = 1.5 * HOCKEY_H
HOCKEY_MAX_PATH = 3.5 * HOCKEY_H


def hockey_zones(amounts: Sequence[int]) -> list[tuple[float, float]]:
    """Границы зон по вертикали (y0, y1) в порядке ставок."""
    total = sum(amounts)
    zones, y = [], 0.0
    for i, amount in enumerate(amounts):
        y1 = HOCKEY_H if i == len(amounts) - 1 else y + HOCKEY_H * amount / total
        zones.append((y, y1))
        y = y1
    return zones


def _fold(v: float, size: float) -> float:
    m = math.floor(v / size)
    r = v - m * size
    return r if m % 2 == 0 else size - r


def _image(t: float, k: int, size: float) -> float:
    """Координата k-го зеркального отражения точки t (метод развёртки)."""
    return k * size + (t if k % 2 == 0 else size - t)


def hockey_shot(zone: tuple[float, float], rng: random.Random = RNG) -> dict:
    y0, y1 = zone
    margin = min(3.0, (y1 - y0) / 4)
    tx = rng.uniform(6.0, HOCKEY_W - 6.0)
    ty = rng.uniform(y0 + margin, y1 - margin)
    sx, sy = HOCKEY_START
    candidates = []
    for i in range(-6, 7):
        for j in range(-5, 6):
            ix, iy = _image(tx, i, HOCKEY_W), _image(ty, j, HOCKEY_H)
            dist = math.hypot(ix - sx, iy - sy)
            if HOCKEY_MIN_PATH <= dist <= HOCKEY_MAX_PATH:
                candidates.append((ix, iy, dist))
    ix, iy, dist = rng.choice(candidates)
    # Точки отскока: пересечения прямой с линиями бортов развёрнутого поля
    ts = []
    for size, a, b, axis in ((HOCKEY_W, sx, ix, 0), (HOCKEY_H, sy, iy, 1)):
        lo, hi = sorted((a, b))
        k = math.floor(lo / size) + 1
        while k * size < hi:
            ts.append(((k * size) - a) / (b - a))
            k += 1
    points = [[sx, sy]]
    for t in sorted(ts):
        px, py = sx + (ix - sx) * t, sy + (iy - sy) * t
        points.append([round(_fold(px, HOCKEY_W), 3), round(_fold(py, HOCKEY_H), 3)])
    points.append([round(tx, 3), round(ty, 3)])
    return {"field": [HOCKEY_W, HOCKEY_H], "points": points, "length": round(dist, 3),
            "angle": round(math.degrees(math.atan2(iy - sy, ix - sx)), 1)}
