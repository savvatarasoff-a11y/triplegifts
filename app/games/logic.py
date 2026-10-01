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
# В начале колесо решает, какая кирка выпадет (или никакая). Кирка падает в шахту и летает под гравитацией,
# отскакивая от блоков при любом касании любой своей частью (столкновения — по пиксельному контуру спрайта
# с учётом поворота). Каждое касание блока: −1 прочности кирки и −1 HP блока; HP зависит от ценности блока.
# Сломанная руда платит, TNT взрывает всё вокруг, верстак чинит кирку. Всю физику считает сервер;
# приложение получает мир и точки столкновений и рисует те же траектории.

PICKAXE_RTP = 0.87
PICKAXE_MAX_X = 5000          # потолок выигрыша за игру, в ставках
PICK_TOUCH = 4                # сколько прочности отнимает одно касание блока
PICKAXE_HEAL = 48             # сколько прочности возвращает верстак (12 касаний)
# колесо: (кирка, шанс); прочность кирок
PICK_WHEEL = (("none", 0.25), ("wood", 0.35), ("iron", 0.22), ("gold", 0.12), ("diamond", 0.06))
PICK_TIERS = {"wood": 100, "iron": 200, "gold": 300, "diamond": 400}
PICKAXES = tuple(PICK_TIERS)
PICK_COLS = 7
PICK_TOP = -8                 # выше кирка не улетает (невидимый потолок в небе)
PICK_START = (3.5, -3.5)      # отсюда — из барабана с кирками — кирка падает в шахту
PICK_DIRT_ROWS = 3            # у поверхности больше земли (доля руд среди остального не меняется)
PICK_G = 22.0                 # гравитация, клеток/с²
PICK_DT = 1 / 180             # шаг физики, с
PICK_REST = 0.78              # упругость отскока
PICK_VMIN, PICK_VMAX = 6.0, 12.0
PICK_MAX_T = 300.0            # страховка по времени
PICK_SIZE = 1.1               # размер спрайта кирки в клетках
# форма кирки — тот же спрайт 16×16, что рисует приложение; любой непрозрачный пиксель сталкивается с блоками
PICK_ART = ("................", "................", ".....OOOOO......", "....OHMQMLOei...", ".....OOOOMMkb...",
            ".........eLMO...", "........eibMLO..", ".......ekb.OMO..", "......eib..OQO..", ".....ekb...OLO..",
            "....eib....OHO..", "...ekb......O...", "..eib...........", "..kb............", "................",
            "................")
_PICK_PTS = tuple((((i + 0.5) / 16 - 0.5) * PICK_SIZE, ((j + 0.5) / 16 - 0.5) * PICK_SIZE)
                  for j in range(16) for i in range(16) if PICK_ART[j][i] != ".")
_PICK_BR = max(math.hypot(x, y) for x, y in _PICK_PTS) + 0.02
# HP блоков по ценности; руды одной прочности в физике неотличимы — на этом держится точный расчёт RTP ниже
PICK_HARD = {"grass": 1, "dirt": 1, "tnt": 1, "bench": 1, "stone": 2, "coal": 2, "copper": 2,
             "iron": 3, "gold": 3, "redstone": 3, "lapis": 3, "chest": 3, "diamond": 5, "emerald": 5}
PICK_CODES = {"grass": "g", "dirt": "d", "stone": "s", "coal": "c", "copper": "u", "iron": "i", "gold": "o",
              "redstone": "r", "lapis": "l", "chest": "h", "diamond": "a", "emerald": "e", "tnt": "t", "bench": "b"}
_PAY_CLASSES = (2, 3, 5)
PICK_WEIGHTS = {"dirt": 28, "stone": 38, "coal": 8, "copper": 5, "iron": 4, "gold": 4, "redstone": 3, "lapis": 2,
                "chest": 1.2, "diamond": 0.6, "emerald": 0.05, "tnt": 2, "bench": 1.6}
_PICK_VALUES = {"coal": 0.5, "copper": 0.8, "iron": 1, "gold": 3, "redstone": 5, "lapis": 6, "chest": 8,
                "diamond": 40, "emerald": 400}
ORES = tuple(_PICK_VALUES)
# Среднее число сломанных блоков каждого класса прочности за игру каждой киркой (Монте-Карло, 50000 игр;
# пересчитать: pickaxe_class_counts). Тип блока внутри класса на физику не влияет, поэтому
# E[выигрыш] = Σ по классам E[N_класса] × средняя ценность руды класса — без шума от редких изумрудов.
PICKAXE_CLASS_COUNTS: dict[str, dict[int, float]] = {
    "wood": {2: 4.3971, 3: 0.9146, 5: 0.0225},
    "iron": {2: 12.5309, 3: 2.8865, 5: 0.089},
    "gold": {2: 20.9976, 3: 4.9832, 5: 0.1611},
    "diamond": {2: 29.4733, 3: 7.0739, 5: 0.2331},
}


def pickaxe_spin(rng: random.Random = RNG) -> str:
    return rng.choices([k for k, _ in PICK_WHEEL], [p for _, p in PICK_WHEEL])[0]


def pickaxe_run(hp0: int, pays: dict[str, float] | None = None, rng: random.Random = RNG, record: bool = True,
                checkpoints: tuple[int, ...] = ()) -> dict[str, Any]:
    """Одна партия с киркой прочностью hp0: мир, столкновения, итог.

    checkpoints — прочности, для которых нужно запомнить счётчики сломанных блоков в момент, когда кирка с такой
    прочностью сломалась бы (траектория от прочности не зависит — она лишь решает, когда остановиться)."""
    pays = PICKAXE_TABLE if pays is None else pays
    types, ws = list(PICK_WEIGHTS), list(PICK_WEIGHTS.values())
    ws_top = [w * 3 if t == "dirt" else w for t, w in PICK_WEIGHTS.items()]
    world: dict[tuple[int, int], list] = {}

    def get(x: int, y: int):
        if x < 0 or x >= PICK_COLS or y < PICK_TOP:
            return "wall"
        if y < 0:
            return None
        k = (x, y)
        if k not in world:
            t = "grass" if y == 0 else rng.choices(types, ws_top if y <= PICK_DIRT_ROWS else ws)[0]
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

    x, y = PICK_START
    vx, vy = round(rng.uniform(-3, 3), 4), 0.0
    th, om = 0.0, round(rng.choice((-1, 1)) * rng.uniform(5, 10), 4)     # стартует ровно как стоит в барабане
    start = {"t": 0, "x": x, "y": y, "vx": vx, "vy": vy, "a": th, "w": om}
    touches = heals = 0
    t, total = 0.0, 0.0
    events: list[dict[str, Any]] = []
    counts = {k: 0 for k in _PAY_CLASSES}
    snaps: dict[int, dict[int, int]] = {}
    pending = sorted(set(checkpoints))
    dt, g = PICK_DT, PICK_G
    def hp_left(h: int) -> int:
        return h - touches * PICK_TOUCH + heals * PICKAXE_HEAL

    while hp_left(hp0) > 0 and t < PICK_MAX_T:
        nx, ny = x + vx * dt, y + vy * dt + 0.5 * g * dt * dt
        nvy, nth = vy + g * dt, th + om * dt
        t += dt
        solid = set()
        for cx in range(math.floor(nx - _PICK_BR), math.floor(nx + _PICK_BR) + 1):
            for cy in range(math.floor(ny - _PICK_BR), math.floor(ny + _PICK_BR) + 1):
                if get(cx, cy) is not None:
                    solid.add((cx, cy))
        best = None
        if solid:
            co, si = math.cos(nth), math.sin(nth)
            for px, py in _PICK_PTS:
                wx, wy = nx + px * co - py * si, ny + px * si + py * co
                k = (math.floor(wx), math.floor(wy))
                if k not in solid:
                    continue
                cx, cy = k
                # выталкиваем через ближайшую грань, за которой пусто
                d, n = min(((wx - cx) + 5 * ((cx - 1, cy) in solid), (-1, 0)),
                           ((cx + 1 - wx) + 5 * ((cx + 1, cy) in solid), (1, 0)),
                           ((wy - cy) + 5 * ((cx, cy - 1) in solid), (0, -1)),
                           ((cy + 1 - wy) + 5 * ((cx, cy + 1) in solid), (0, 1)))
                if best is None or d > best[0]:
                    best = (d, n, cx, cy)
        if best is None:
            x, y, vy, th = nx, ny, nvy, nth
            continue
        d, (nxn, nyn), cx, cy = best
        d = d % 5 + 0.002
        x, y, th = nx + nxn * d, ny + nyn * d, nth
        dot = vx * nxn + nvy * nyn
        c = get(cx, cy)
        broken: list = []
        if dot >= 0:                            # уже отлетает (задело при повороте) — только выталкиваем
            vy = nvy
            kind = 0
        else:
            rvx, rvy = vx - (1 + PICK_REST) * dot * nxn, nvy - (1 + PICK_REST) * dot * nyn
            ang = math.atan2(rvy, rvx) + rng.uniform(-0.45, 0.45)
            sp = min(PICK_VMAX, max(PICK_VMIN, math.hypot(rvx, rvy)))
            vx, vy = math.cos(ang) * sp, math.sin(ang) * sp
            if vx * nxn + vy * nyn < 0.35 * sp:     # отскок всегда от блока, а не вдоль него
                ang = math.atan2(nyn, nxn) + rng.uniform(-0.8, 0.8)
                vx, vy = math.cos(ang) * sp, math.sin(ang) * sp
            om = -om * 0.7 + rng.uniform(-6, 6)
            om = math.copysign(min(16.0, max(4.0, abs(om))), om)
            kind = 2
            if c != "wall":
                kind = 1
                touches += 1
                c[1] -= 1
                if c[1] <= 0:
                    c[1] = 1
                    smash(cx, cy, broken)
                    for _, _, bt in broken:
                        if bt == "bench":
                            heals += 1
                        if PICK_HARD[bt] in counts:
                            counts[PICK_HARD[bt]] += 1
                        total += pays.get(bt, 0)
            while pending and hp_left(pending[0]) <= 0:
                snaps[pending.pop(0)] = dict(counts)
        # округляем состояние сразу — приложение продолжит траекторию ровно с тех же чисел
        x, y, vx, vy = round(x, 5), round(y, 5), round(vx, 5), round(vy, 5)
        th, om = round(th, 5), round(om, 5)
        if record:
            events.append({"t": round(t, 5), "x": x, "y": y, "vx": vx, "vy": vy, "a": th, "w": om, "k": kind,
                           "c": [cx, cy] if kind == 1 else None, "hp": max(0, hp_left(hp0)),
                           "br": [[bx, by, round(pays.get(bt, 0), 4)] for bx, by, bt in broken]})
    for h in pending:
        snaps[h] = dict(counts)
    out: dict[str, Any] = {"mult": round(min(total, PICKAXE_MAX_X), 2), "counts": counts, "snaps": snaps, "t": t}
    if record:
        rows = max([yy for _, yy in world] + [0]) + 10
        for xx in range(PICK_COLS):             # мир с запасом ниже, чтобы камере было что показать
            for yy in range(rows):
                get(xx, yy)
        out["world"] = ["".join(PICK_CODES[world[(xx, yy)][0]] for xx in range(PICK_COLS)) for yy in range(rows)]
        out["events"] = events
        out["start"] = start
    return out


def pickaxe_class_counts(n: int, rng: random.Random) -> dict[str, dict[int, float]]:
    """Монте-Карло для PICKAXE_CLASS_COUNTS: одна длинная партия даёт счётчики сразу для всех кирок."""
    hp_by = {h: k for k, h in PICK_TIERS.items()}
    acc = {k: {c: 0 for c in _PAY_CLASSES} for k in PICK_TIERS}
    for _ in range(n):
        run = pickaxe_run(max(PICK_TIERS.values()), {}, rng, record=False, checkpoints=tuple(PICK_TIERS.values()))
        for h, cnt in run["snaps"].items():
            for c, v in cnt.items():
                acc[hp_by[h]][c] += v
    return {k: {c: v / n for c, v in a.items()} for k, a in acc.items()}


def _class_value(pays: dict[str, float], counts: dict[int, float]) -> float:
    ev = 0.0
    for k in _PAY_CLASSES:
        members = [t for t in PICK_WEIGHTS if PICK_HARD[t] == k]
        wsum = sum(PICK_WEIGHTS[t] for t in members)
        ev += counts[k] * sum(PICK_WEIGHTS[t] * pays.get(t, 0) for t in members) / wsum
    return ev


def _wheel_value(pays: dict[str, float]) -> float:
    return sum(p * _class_value(pays, PICKAXE_CLASS_COUNTS[k]) for k, p in PICK_WHEEL if k != "none")


def _pick_table() -> dict[str, float]:
    if not PICKAXE_CLASS_COUNTS:
        return dict(_PICK_VALUES)
    f = PICKAXE_RTP / _wheel_value(_PICK_VALUES)
    return {t: math.floor(v * f * 1000) / 1000 for t, v in _PICK_VALUES.items()}   # вниз — RTP не выше заданного


PICKAXE_TABLE = _pick_table()


def pickaxe_rtp() -> float:
    """Возврат игрокам с учётом колеса (и шанса, что кирка не выпадет)."""
    return _wheel_value(PICKAXE_TABLE)


def pickaxe_play(rng: random.Random = RNG) -> dict[str, Any]:
    tier = pickaxe_spin(rng)
    if tier == "none":
        return {"tier": "none", "mult": 0.0}
    return {"tier": tier, "hp": PICK_TIERS[tier], **pickaxe_run(PICK_TIERS[tier], rng=rng)}


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
