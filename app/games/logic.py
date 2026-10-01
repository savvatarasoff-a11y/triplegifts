"""Математика игр. Всё случайное — только на сервере через криптостойкий генератор.

Функции чистые: генератор случайных чисел передаётся параметром, чтобы их можно было тестировать.
"""
from __future__ import annotations

import math
import random
import secrets
from dataclasses import dataclass
from typing import Any, Sequence

RNG = secrets.SystemRandom()

HOUSE_EDGE = 0.05  # комиссия казино в костях и краше
MINES_EDGE = 0.12  # в минах жёстче: игроки слишком часто выигрывали


# ---------------- Слоты ----------------

# Слоты как 🎰 в Telegram: 3 барабана по 4 символа, 64 равновероятных исхода (значения 1–64).
# Значение v раскладывается так же, как у Telegram: v-1 = r1 + 4·r2 + 16·r3.
SLOT_SYMBOLS = ["bar", "grape", "lemon", "seven"]
SLOT_777 = 40         # 7️⃣7️⃣7️⃣ — NFT стоимостью ≈ ×40 от ставки (или ×40 звёздами, если NFT нет)
SLOT_TRIPLE = 2       # BAR BAR BAR, 🍇🍇🍇, 🍋🍋🍋
SLOT_TWO_SEVENS = 1   # ровно две семёрки — возврат ставки
SLOT_PAIR = 0         # другие пары ничего не дают
SLOT_NFT_TOLERANCE = 0.25   # NFT подходит, если его цена от 75% до 100% от 40 ставок (дороже — нельзя)
# RTP = (40 + 3·2 + 9·1) / 64 ≈ 85.9%; в плюс уходят 4 из 64 исходов


def slots_reels(value: int) -> list[str]:
    if not 1 <= value <= 64:
        raise ValueError("value")
    v = value - 1
    return [SLOT_SYMBOLS[v % 4], SLOT_SYMBOLS[(v // 4) % 4], SLOT_SYMBOLS[(v // 16) % 4]]


def slots_multiplier(reels: Sequence[str]) -> float:
    if reels[0] == reels[1] == reels[2]:
        return SLOT_777 if reels[0] == "seven" else SLOT_TRIPLE
    counts = {s: reels.count(s) for s in set(reels)}
    if counts.get("seven") == 2:
        return SLOT_TWO_SEVENS
    if 2 in counts.values():
        return SLOT_PAIR
    return 0


def slots_spin(rng: random.Random = RNG) -> tuple[int, list[str], float]:
    value = rng.randrange(64) + 1
    reels = slots_reels(value)
    return value, reels, slots_multiplier(reels)


def slots_rtp() -> float:
    """Точный возврат игроку полным перебором 64 исходов."""
    return sum(slots_multiplier(slots_reels(v)) for v in range(1, 65)) / 64


# ---------------- Plinko ----------------

PLINKO_RTP = 0.94          # возврат игроку в Plinko (комиссия 6%)
PLINKO_ROWS = (8, 12, 16)
PLINKO_RISKS = ("low", "medium", "high")
# Классические таблицы Plinko (левая половина + центр, симметричны); ниже масштабируются до PLINKO_RTP
_PLINKO_BASE = {
    (8, "low"): (5.6, 2.1, 1.1, 1, 0.5),
    (8, "medium"): (13, 3, 1.3, 0.7, 0.4),
    (8, "high"): (29, 4, 1.5, 0.3, 0.2),
    (12, "low"): (10, 3, 1.6, 1.4, 1.1, 1, 0.5),
    (12, "medium"): (33, 11, 4, 2, 1.1, 0.6, 0.3),
    (12, "high"): (170, 24, 8.1, 2, 0.7, 0.2, 0.2),
    (16, "low"): (16, 9, 2, 1.4, 1.4, 1.2, 1.1, 1, 0.5),
    (16, "medium"): (110, 41, 10, 5, 3, 1.5, 1, 0.5, 0.3),
    (16, "high"): (1000, 130, 26, 9, 4, 2, 0.2, 0.2, 0.2),
}


def plinko_probs(rows: int) -> list[float]:
    """Вероятность попасть в лунку k: C(n, k) / 2ⁿ (шарик n раз отскакивает влево или вправо)."""
    return [math.comb(rows, k) / 2 ** rows for k in range(rows + 1)]


def _plinko_scale(rows: int, half: tuple[float, ...]) -> list[float]:
    full = list(half) + list(reversed(half[:-1]))
    rtp = sum(p * m for p, m in zip(plinko_probs(rows), full))
    f = PLINKO_RTP / rtp
    # округляем вниз: 2 знака до ×10, 1 знак до ×100, дальше целые — RTP не превышает PLINKO_RTP
    def rnd(x: float) -> float:
        step = 0.01 if x < 10 else 0.1 if x < 100 else 1
        return round(math.floor(x * f / step + 1e-9) * step, 2)
    return [rnd(m) for m in full]


PLINKO_TABLES = {key: _plinko_scale(key[0], half) for key, half in _PLINKO_BASE.items()}


def plinko_rtp(rows: int, risk: str) -> float:
    return sum(p * m for p, m in zip(plinko_probs(rows), PLINKO_TABLES[(rows, risk)]))


def plinko_drop(rows: int, risk: str, rng: random.Random = RNG) -> tuple[list[int], int, float]:
    """(путь 0=влево/1=вправо, лунка, множитель)."""
    path = [rng.randrange(2) for _ in range(rows)]
    bucket = sum(path)
    return path, bucket, PLINKO_TABLES[(rows, risk)][bucket]


# ---------------- Кирка ----------------
# Кирка летает по шахте под действием гравитации и отскакивает от каждого блока, которого коснулась.
# Каждое касание снимает 1 прочности кирки и 1 HP блока; HP блока зависит от его ценности. Сломанная руда платит,
# TNT взрывает всё вокруг, блок починки возвращает кирке прочность. Игра кончается, когда прочность на нуле.
# Всю физику считает сервер; приложение получает мир и точки отскоков и рисует те же параболы.

PICKAXE_RTP = 0.93
PICKAXE_HP = 60
PICKAXE_MAX_X = 5000          # потолок выигрыша за игру, в ставках
PICKAXE_REPAIR = 10           # сколько прочности возвращает блок починки
PICK_COLS = 7
PICK_G = 22.0                 # гравитация, клеток/с²
PICK_DT = 1 / 240             # шаг физики, с
PICK_R = 0.3                  # радиус кирки, клеток
PICK_REST = 0.78              # упругость отскока
PICK_VMIN, PICK_VMAX = 6.0, 12.0
PICK_MAX_T = 240.0            # страховка: дольше 4 минут игра не идёт
# HP блоков по ценности; руды одной прочности в физике неотличимы — на этом держится точный расчёт RTP ниже
PICK_HARD = {"grass": 1, "dirt": 1, "tnt": 1, "repair": 1, "stone": 2, "coal": 2, "copper": 2,
             "iron": 3, "gold": 3, "redstone": 3, "lapis": 3, "diamond": 5, "emerald": 5}
_PAY_CLASSES = (2, 3, 5)

# по кирке: веса блоков и относительная ценность руд (дороже кирка — реже, но крупнее руды)
_PICK_LEVELS = {
    "iron":    {"w": {"dirt": 26, "stone": 34, "coal": 10, "copper": 7, "iron": 6, "gold": 5, "redstone": 4, "lapis": 3,
                      "diamond": 1.0, "emerald": 0.25, "tnt": 2.2, "repair": 1.6},
                "v": {"coal": 0.5, "copper": 0.8, "iron": 1, "gold": 2, "redstone": 3, "lapis": 4, "diamond": 15,
                      "emerald": 60}},
    "gold":    {"w": {"dirt": 28, "stone": 38, "coal": 8, "copper": 5, "iron": 4, "gold": 4, "redstone": 3, "lapis": 2,
                      "diamond": 0.6, "emerald": 0.06, "tnt": 2.2, "repair": 1.6},
                "v": {"coal": 0.5, "copper": 0.8, "iron": 1, "gold": 3, "redstone": 5, "lapis": 6, "diamond": 40,
                      "emerald": 1000}},
    "diamond": {"w": {"dirt": 30, "stone": 42, "coal": 6, "copper": 4, "iron": 3, "gold": 3, "redstone": 2, "lapis": 1.5,
                      "diamond": 0.3, "emerald": 0.012, "tnt": 2.2, "repair": 1.6},
                "v": {"coal": 0.5, "copper": 0.8, "iron": 1, "gold": 3, "redstone": 8, "lapis": 10, "diamond": 100,
                      "emerald": 8000}},
}
PICKAXES = tuple(_PICK_LEVELS)
ORES = ("coal", "copper", "iron", "gold", "redstone", "lapis", "diamond", "emerald")
# Среднее число сломанных блоков каждого класса прочности за игру (Монте-Карло: 60 000 игр, у алмазной — 120 000;
# пересчитать: pickaxe_class_counts). Тип блока внутри класса на физику не влияет, поэтому
# E[выигрыш] = Σ по классам E[N_класса] × средняя ценность руды класса — без шума от редких изумрудов.
PICKAXE_CLASS_COUNTS = {
    "iron": {2: 16.6596, 3: 4.9127, 5: 0.2287},
    "gold": {2: 18.2105, 3: 3.8484, 5: 0.1294},
    "diamond": {2: 19.3552, 3: 2.9146, 5: 0.0623},
}


def pickaxe_run(level: str, pays: dict[str, float] | None = None, rng: random.Random = RNG, record: bool = True
                ) -> dict[str, Any]:
    """Одна партия: мир, точки отскоков (t, x, y, vx, vy, клетка, прочность, сломанные блоки), итог."""
    weights = _PICK_LEVELS[level]["w"]
    pays = PICKAXE_TABLES[level] if pays is None else pays
    types, ws = list(weights), list(weights.values())
    world: dict[tuple[int, int], list] = {}

    def get(x: int, y: int):
        if x < 0 or x >= PICK_COLS:
            return "wall"
        if y < 0:
            return None
        k = (x, y)
        if k not in world:
            t = "grass" if y == 0 else rng.choices(types, ws)[0]
            world[k] = [t, PICK_HARD[t]]
        c = world[k]
        return c if c[1] > 0 else None

    def smash(cx: int, cy: int, out: list) -> None:
        c = get(cx, cy)
        if c is None or c == "wall":
            return
        c[1] = 0
        out.append((cx, cy, c[0]))
        if c[0] == "tnt":                       # взрыв: всё вокруг ломается сразу
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx or dy:
                        smash(cx + dx, cy + dy, out)

    x, y = 3.5, -1.6
    vx, vy = round(rng.uniform(-3, 3), 4), 0.0
    start = {"t": 0, "x": x, "y": y, "vx": round(vx, 4), "vy": vy}
    hp, t, total = PICKAXE_HP, 0.0, 0.0
    events: list[dict[str, Any]] = []
    counts = {k: 0 for k in _PAY_CLASSES}
    dt, g, r = PICK_DT, PICK_G, PICK_R
    while hp > 0 and t < PICK_MAX_T:
        nx = x + vx * dt
        ny = y + vy * dt + 0.5 * g * dt * dt
        nvy = vy + g * dt
        best = None
        for cx in range(math.floor(nx - r), math.floor(nx + r) + 1):
            for cy in range(math.floor(ny - r), math.floor(ny + r) + 1):
                c = get(cx, cy)
                if c is None:
                    continue
                px, py = min(max(nx, cx), cx + 1), min(max(ny, cy), cy + 1)
                dx, dy = nx - px, ny - py
                d = math.hypot(dx, dy)
                if d >= r:
                    continue
                if d < 1e-9:
                    dx, dy = -vx, -nvy
                    d = math.hypot(dx, dy) or 1.0
                if best is None or r - d > best[0]:
                    best = (r - d, cx, cy, dx / d, dy / d, c)
        t += dt
        if best is None:
            x, y, vy = nx, ny, nvy
            continue
        pen, cx, cy, nxn, nyn, c = best
        x, y = nx + nxn * pen, ny + nyn * pen
        dot = vx * nxn + nvy * nyn
        rvx, rvy = vx - (1 + PICK_REST) * dot * nxn, nvy - (1 + PICK_REST) * dot * nyn
        ang = math.atan2(rvy, rvx) + rng.uniform(-0.45, 0.45)
        sp = min(PICK_VMAX, max(PICK_VMIN, math.hypot(rvx, rvy)))
        vx, vy = math.cos(ang) * sp, math.sin(ang) * sp
        if vx * nxn + vy * nyn < 0.35 * sp:     # отскок всегда от блока, а не вдоль него
            ang = math.atan2(nyn, nxn) + rng.uniform(-0.8, 0.8)
            vx, vy = math.cos(ang) * sp, math.sin(ang) * sp
        broken: list = []
        if c != "wall":
            hp -= 1
            c[1] -= 1
            if c[1] <= 0:
                c[1] = 1
                smash(cx, cy, broken)
                for _, _, bt in broken:
                    if bt == "repair":
                        hp = min(PICKAXE_HP, hp + PICKAXE_REPAIR)
                    if PICK_HARD[bt] in counts:
                        counts[PICK_HARD[bt]] += 1
                    total += pays.get(bt, 0)
        if record:
            events.append({"t": round(t, 4), "x": round(x, 4), "y": round(y, 4), "vx": round(vx, 4), "vy": round(vy, 4),
                           "c": None if c == "wall" else [cx, cy], "hp": max(0, hp),
                           "br": [[bx, by, round(pays.get(bt, 0), 4)] for bx, by, bt in broken]})
    out: dict[str, Any] = {"mult": round(min(total, PICKAXE_MAX_X), 2), "counts": counts, "t": round(t, 4)}
    if record:
        rows = max(y for _, y in world) + 10
        for xx in range(PICK_COLS):             # мир с запасом ниже, чтобы камере было что показать
            for yy in range(rows):
                get(xx, yy)
        out["world"] = ["".join(PICK_CODES[world[(xx, yy)][0]] for xx in range(PICK_COLS)) for yy in range(rows)]
        out["events"] = events
        out["start"] = start
    return out


PICK_CODES = {"grass": "g", "dirt": "d", "stone": "s", "coal": "c", "copper": "u", "iron": "i", "gold": "o",
              "redstone": "r", "lapis": "l", "diamond": "a", "emerald": "e", "tnt": "t", "repair": "h"}


def pickaxe_class_counts(level: str, n: int, rng: random.Random) -> dict[int, float]:
    acc = {k: 0 for k in _PAY_CLASSES}
    for _ in range(n):
        for k, v in pickaxe_run(level, {}, rng, record=False)["counts"].items():
            acc[k] += v
    return {k: v / n for k, v in acc.items()}


def _class_value(level: str, pays: dict[str, float], counts: dict) -> float:
    w = _PICK_LEVELS[level]["w"]
    ev = 0.0
    for k in _PAY_CLASSES:
        members = [t for t in w if PICK_HARD.get(t) == k]
        wsum = sum(w[t] for t in members)
        ev += counts[k] * sum(w[t] * pays.get(t, 0) for t in members) / wsum
    return ev


def _pick_table(level: str) -> dict[str, float]:
    counts = PICKAXE_CLASS_COUNTS[level]
    lv = _PICK_LEVELS[level]
    if not counts:
        return dict(lv["v"])
    f = PICKAXE_RTP / _class_value(level, lv["v"], counts)
    return {t: math.floor(v * f * 1000) / 1000 for t, v in lv["v"].items()}   # вниз — RTP не выше PICKAXE_RTP


PICKAXE_TABLES = {lv: _pick_table(lv) for lv in PICKAXES}


def pickaxe_rtp(level: str) -> float:
    return _class_value(level, PICKAXE_TABLES[level], PICKAXE_CLASS_COUNTS[level])


# ---------------- Мины ----------------

MINES_CELLS = 25


def mines_multiplier(mines: int, opened: int) -> float:
    """Множитель после `opened` безопасных клеток при `mines` минах на поле 5×5."""
    if opened == 0:
        return 1.0
    fair = 1.0
    for i in range(opened):
        fair *= (MINES_CELLS - i) / (MINES_CELLS - mines - i)
    return round((1 - MINES_EDGE) * fair, 4)


MINES_MIN = 5   # меньше пяти мин — слишком частые мелкие выигрыши (с 3 минами первая клетка безопасна в 88%)


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

# Кейсы состоят из настоящих подарков Telegram. Цена каждого подарка берётся из живого каталога
# (getAvailableGifts) в момент открытия — здесь только состав и веса. Если какого-то подарка
# нет в каталоге или с реальными ценами кейс стал выгоден игроку сильнее CASE_MAX_RTP, кейс отключается.
CASE_MAX_RTP = 0.92
CASE_TARGET_RTP = 0.87


@dataclass(frozen=True)
class CaseDef:
    id: str
    name: str
    emoji: str
    price: int
    items: tuple[tuple[str, float], ...]   # (эмодзи подарка, вес)


CASE_DEFS = [
    CaseDef("bear", "Мишка", "🧸", 25, (
        ("🧸", 55), ("💝", 14), ("🌹", 14), ("🎁", 8), ("🚀", 4), ("🍾", 2), ("💎", 3),
    )),
    CaseDef("rocket", "Ракета", "🚀", 50, (
        ("🧸", 12), ("💝", 10), ("🌹", 16), ("🎁", 12), ("🎂", 12), ("🚀", 12), ("🍾", 10),
        ("🏆", 7), ("💍", 5), ("💎", 4),
    )),
    CaseDef("heart", "Сердечко", "💝", 20, (
        ("💝", 47), ("🧸", 40), ("🌹", 7), ("🎁", 3), ("🎂", 1), ("🚀", 1), ("💎", 1),
    )),
    CaseDef("party", "Праздник", "🎂", 60, (
        ("🧸", 10), ("💝", 8), ("🌹", 8), ("🎁", 6), ("🎂", 14), ("💐", 12), ("🍾", 10), ("🚀", 10),
        ("🏆", 8), ("💍", 8), ("💎", 7),
    )),
    CaseDef("lux", "Люкс", "💍", 105, (
        ("🎂", 5), ("💐", 5), ("🍾", 4), ("🚀", 4), ("🏆", 28), ("💍", 27), ("💎", 27),
    )),
]
NFT_MAX_TOTAL_CHANCE = 0.9      # суммарный шанс NFT в кейсе не выше 90%
NFT_CASE_MIN_PRICE_SHARE = 0.5   # NFT в кейсе — не дешевле 50% цены кейса
CASE_MIN_RTP = 0.8       # кейс, который с текущими ценами отдаёт игрокам меньше 80%, не показываем
NFT_CASE_ID = "nft"
NFT_SHARE = 0.5          # доля цены NFT-кейса, которая в среднем уходит на NFT


@dataclass(frozen=True)
class NftCaseDef:
    id: str
    name: str
    emoji: str
    price: int | None     # None — цена из настройки NFT_CASE_PRICE
    share: float          # доля цены, которая в среднем уходит на NFT
    rtp: float = 0.87     # сколько кейс в среднем возвращает: чем дороже NFT внутри, тем меньше


NFT_CASE_DEFS = [
    NftCaseDef("nft_starter", "NFT Starter", "🍀", 50, 0.5, 0.87),
    NftCaseDef("nft_mini", "NFT Mini", "🎲", 100, 0.6, 0.86),
    NftCaseDef(NFT_CASE_ID, "NFT-кейс", "💎", None, 0.7, 0.83),
    NftCaseDef("nft_gold", "NFT Gold", "🏆", 500, 0.73, 0.79),
    NftCaseDef("nft_premium", "NFT Premium", "👑", 1000, 0.72, 0.76),
    NftCaseDef("nft_legend", "NFT Legend", "🐉", 2500, 0.69, 0.72),
]
CASE_RTP_TOLERANCE = 0.07   # кейс, который с текущими ценами отдаёт меньше (rtp − 7%), не показываем


def pick_weighted(items: Sequence[Any], weights: Sequence[float], rng: random.Random = RNG) -> Any:
    return rng.choices(items, weights=weights, k=1)[0]


def expected_value(prizes: Sequence[tuple[float, float]]) -> float:
    """prizes: (стоимость, вес)."""
    total = sum(w for _, w in prizes)
    return sum(v * w for v, w in prizes) / total


# NFT-джекпот в кейсах с подарками: модели от 3 до 100 цен кейса, в среднем 15% цены кейса уходит на них
GIFT_CASE_JACKPOT_SHARE = 0.22
GIFT_CASE_JACKPOT_RANGE = (3, 100)
GIFT_CASE_JACKPOT_MAX_MODELS = 8


def tilt_weights(weights: Sequence[float], amounts: Sequence[float], target_mean: float) -> list[float] | None:
    """Сдвигает веса к дешёвым/дорогим призам (w·e^(−t·a)), сохраняя их разнообразие, так чтобы среднее
    стало target_mean (не выше). None — если так не получить."""
    lo_v, hi_v = min(amounts), max(amounts)
    if not lo_v <= target_mean <= hi_v:
        return None
    scale = hi_v or 1

    def tilted(t: float) -> list[float]:
        return [w * math.exp(-t * a / scale) for w, a in zip(weights, amounts)]

    def mean(t: float) -> float:
        w = tilted(t)
        return sum(x * a for x, a in zip(w, amounts)) / sum(w)

    lo_t, hi_t = -60.0, 60.0                     # mean(t) убывает по t
    for _ in range(100):
        mid = (lo_t + hi_t) / 2
        lo_t, hi_t = (mid, hi_t) if mean(mid) > target_mean else (lo_t, mid)
    return tilted(hi_t)                          # hi_t: среднее не выше нужного


def gift_case_with_jackpot(price: int, gifts: Sequence[tuple[float, int]], nft_prices: Sequence[int],
                           target_rtp: float = CASE_TARGET_RTP, share: float = GIFT_CASE_JACKPOT_SHARE
                           ) -> tuple[list[float], list[float]] | None:
    """Кейс с подарками + NFT-джекпот: (вероятности NFT, новые веса подарков). None — если не сходится."""
    if not nft_prices or not gifts:
        return None
    n = len(nft_prices)
    weights = [w for w, _ in gifts]
    amounts = [a for _, a in gifts]
    # дешёвый кейс может не вытянуть полную долю джекпота — уменьшаем её, пока не сойдётся
    for k in (1, 0.66, 0.33):
        p_nft = [share * k * price / (n * pn) for pn in nft_prices]
        q = 1 - sum(p_nft)
        nft_ev = sum(p * pn for p, pn in zip(p_nft, nft_prices))
        tilted = tilt_weights(weights, amounts, (target_rtp * price - nft_ev) / q)
        if tilted is not None:
            total = sum(tilted)
            return p_nft, [q * w / total for w in tilted]
    return None


# Ежедневный бесплатный кейс (для подписчиков канала): почти всегда 1–3 ★, крупные призы — с крошечным,
# но настоящим шансом (показывается игроку как есть). Звёзды — бонусные: их нужно один раз отыграть.
FREE_CASE_EVERY = 24 * 3600
FREE_CASE_STARS = ((1, 6000), (2, 2500), (3, 900), (5, 400), (10, 150), (25, 40), (100, 8), (500, 1.5))
FREE_CASE_NFT_WEIGHT = 0.5            # 0.005%
FREE_CASE_NFT_RANGE = (300, 3000)     # NFT-приз — модель в этом диапазоне цен (ближе к 1000 ★)


def nft_case_weights(price: int, nft_prices: Sequence[int], gift_prices: Sequence[int],
                     target_rtp: float = CASE_TARGET_RTP, share: float = NFT_SHARE
                     ) -> tuple[list[float], list[float]] | None:
    """Вероятности для NFT-кейса: (вероятности NFT, вероятности обычных подарков).

    Каждый NFT в среднем «съедает» равную часть NFT_SHARE·цены: p_i = NFT_SHARE·C / (n·P_i).
    Остальная вероятность делится между двумя соседними по цене обычными подарками так,
    чтобы итоговый RTP был равен target_rtp (или ниже, если точнее не получается).
    """
    if not nft_prices or not gift_prices:
        return None
    levels = sorted(set(gift_prices))
    n = len(nft_prices)
    p_nft = [share * price / (n * pn) for pn in nft_prices]
    total_nft = sum(p_nft)
    if total_nft > NFT_MAX_TOTAL_CHANCE:       # NFT слишком дешёвые относительно кейса
        p_nft = [p * NFT_MAX_TOTAL_CHANCE / total_nft for p in p_nft]
        total_nft = NFT_MAX_TOTAL_CHANCE
    q = 1 - total_nft
    nft_ev = sum(p * pn for p, pn in zip(p_nft, nft_prices))
    need_mean = (target_rtp * price - nft_ev) / q
    p_gift = [0.0] * len(gift_prices)
    if need_mean <= levels[0]:
        lo = hi = levels[0]
        share_hi = 0.0
    elif need_mean >= levels[-1]:
        lo = hi = levels[-1]
        share_hi = 1.0
    else:
        hi = next(v for v in levels if v >= need_mean)
        lo = max(v for v in levels if v <= need_mean)
        share_hi = 0.0 if hi == lo else (need_mean - lo) / (hi - lo)
    lo_idx = [i for i, v in enumerate(gift_prices) if v == lo]
    hi_idx = [i for i, v in enumerate(gift_prices) if v == hi]
    for i in lo_idx:
        p_gift[i] += q * (1 - share_hi) / len(lo_idx)
    for i in hi_idx:
        p_gift[i] += q * share_hi / len(hi_idx)
    return p_nft, p_gift


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


# ---------- апгрейд NFT ----------

UPGRADE_EDGE = 0.10        # комиссия казино в апгрейде
UPGRADE_MAX_CHANCE = 0.75  # выше шанс не бывает — иначе это обмен, а не игра
UPGRADE_MIN_CHANCE = 0.01


def upgrade_chance(stake: int, target: int) -> float:
    """Шанс апгрейда: ставка / цена цели с комиссией, т.е. в среднем игрок получает 90% ставки."""
    if stake <= 0 or target <= 0:
        return 0.0
    return min(UPGRADE_MAX_CHANCE, (1 - UPGRADE_EDGE) * stake / target)


def upgrade_roll(rng: random.Random = RNG) -> float:
    return rng.random()


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


# ---------------- VIP-уровни, рейкбек, ежедневный бонус ----------------

# Уровень — по сумме ставок (TON считаются как LEVEL_TON_STARS звёзд за 1 TON).
# Рейкбек — доля каждой ставки, которая возвращается игроку. Максимум 1% — это 1/5 самой
# маленькой комиссии казино (5% в PvP и краше), так что казино в плюсе в любой игре.
LEVEL_TON_STARS = 100
LEVELS = (
    # (название, эмодзи, ставок от, рейкбек)
    ("Новичок", "🥉", 0, 0.002),
    ("Серебро", "🥈", 2_000, 0.004),
    ("Золото", "🥇", 10_000, 0.006),
    ("Платина", "💠", 50_000, 0.008),
    ("Бриллиант", "💎", 200_000, 0.01),
)

DAILY_BONUS_EVERY = 24 * 3600
DAILY_BONUS_MIN_STARS = 50                 # бонус — только после пополнения от 50 ⭐ …
DAILY_BONUS_MIN_TON = 500_000_000          # … или от 0.5 TON (против фарма с пустых аккаунтов)
# ежедневный бонус: (звёзд, вес) — в среднем ≈ 4.2 ⭐, 100 ⭐ выпадает в 0.5% случаев
DAILY_BONUS = ((1, 380), (2, 250), (3, 150), (5, 110), (10, 70), (25, 25), (50, 10), (100, 5))


def level_points(wagered_stars: int, wagered_ton_nano: int) -> int:
    return wagered_stars + wagered_ton_nano * LEVEL_TON_STARS // 1_000_000_000


def level_for(points: int) -> int:
    return max(i for i, lv in enumerate(LEVELS) if points >= lv[2])


def daily_bonus_roll(rng: random.Random = RNG) -> int:
    return pick_weighted([a for a, _ in DAILY_BONUS], [w for _, w in DAILY_BONUS], rng)


def daily_bonus_ev() -> float:
    return expected_value([(a, w) for a, w in DAILY_BONUS])
