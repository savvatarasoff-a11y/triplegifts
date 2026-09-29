import math
import random
from collections import Counter

import pytest

from app.games import logic as g


def test_slots_like_telegram():
    # раскладка значения 1–64 как у 🎰 Telegram
    assert g.slots_reels(1) == ["bar", "bar", "bar"]
    assert g.slots_reels(22) == ["grape", "grape", "grape"]
    assert g.slots_reels(43) == ["lemon", "lemon", "lemon"]
    assert g.slots_reels(64) == ["seven", "seven", "seven"]
    assert g.slots_rtp() == pytest.approx(55 / 64)
    mults = [g.slots_multiplier(g.slots_reels(v)) for v in range(1, 65)]
    assert set(mults) == {0, 1, 2, 40}
    assert mults.count(40) == 1 and mults.count(2) == 3 and mults.count(1) == 9 and mults.count(0) == 51
    with pytest.raises(ValueError):
        g.slots_reels(65)


def test_slots_multiplier_rules():
    assert g.slots_multiplier(["seven", "seven", "seven"]) == 40
    assert g.slots_multiplier(["bar", "bar", "bar"]) == 2
    assert g.slots_multiplier(["seven", "lemon", "seven"]) == 1
    assert g.slots_multiplier(["grape", "grape", "seven"]) == 0
    assert g.slots_multiplier(["bar", "grape", "lemon"]) == 0


def test_dice_expected_value():
    for chance in (0.01, 1, 10, 49.5, 50, 90):
        ev = chance / 100 * g.dice_multiplier(chance)
        assert ev == pytest.approx(0.95, abs=0.001)
    assert g.dice_multiplier(50) == 1.9 and g.dice_multiplier(0.01) == 9500
    for bad in (0, 90.01, 1.001):
        with pytest.raises(ValueError):
            g.dice_roll(bad)


def test_dice_roll_under_and_over():
    rng = random.Random(1)
    for _ in range(1000):
        roll, win, mult = g.dice_roll(50, False, rng)
        assert 0 <= roll < 100 and win == (roll < 50) and mult == (1.9 if win else 0)
        roll, win, _ = g.dice_roll(25, True, rng)
        assert win == (roll >= 75)
    # «больше» с шансом 0.01% выигрывает ровно на 99.99
    assert sum(g.dice_roll(0.01, True, random.Random(i))[1] for i in range(3000)) < 5


def test_mines_multiplier_expected_value():
    # EV любого стоп-правила = 0.90: вероятность пройти k клеток × множитель
    for mines in (3, 10, 24):
        for k in range(1, 25 - mines + 1):
            survive = math.comb(25 - mines, k) / math.comb(25, k)
            assert survive * g.mines_multiplier(mines, k) == pytest.approx(0.90, rel=1e-3)
    assert g.mines_multiplier(3, 0) == 1.0
    assert len(set(g.mines_place(5))) == 5
    assert g.mines_multiplier(3, 1) > 1
    with pytest.raises(ValueError):
        g.mines_place(2)


def test_crash_distribution():
    rng = random.Random(42)
    points = [g.crash_point(rng) for _ in range(200_000)]
    assert min(points) >= 1.0
    # P(point >= 2) ≈ 0.95 / 2, P(point >= 10) ≈ 0.095, мгновенный краш ≈ 1 − 0.95/1.01
    share = sum(1 for p in points if p >= 2) / len(points)
    assert share == pytest.approx(0.475, abs=0.01)
    share = sum(1 for p in points if p >= 10) / len(points)
    assert share == pytest.approx(0.095, abs=0.005)
    assert sum(1 for p in points if p == 1.0) / len(points) == pytest.approx(1 - 0.95 / 1.01, abs=0.005)


def test_crash_time_roundtrip():
    for m in (1.0, 1.5, 2.0, 10.0):
        assert g.crash_multiplier_at(g.crash_time_of(m) + 1e-6) == pytest.approx(m, abs=0.011)


STD_GIFT_PRICES = {"💝": 15, "🧸": 15, "🎁": 25, "🌹": 25, "🎂": 50, "💐": 50, "🚀": 50, "🍾": 50,
                   "🏆": 100, "💍": 100, "💎": 100}


def test_case_defs_house_edge_with_real_prices():
    for case in g.CASE_DEFS:
        rtp = g.expected_value([(STD_GIFT_PRICES[e], w) for e, w in case.items]) / case.price
        assert 0.85 < rtp < 0.9


def test_nft_case_weights_hit_target():
    for nft_prices, exact in (([5000], True), ([5000, 1200], True), ([300, 800, 20000], False)):
        p_nft, p_gift = g.nft_case_weights(250, nft_prices, [15, 25, 50, 100])
        assert sum(p_nft) + sum(p_gift) == pytest.approx(1)
        ev = sum(p * v for p, v in zip(p_nft, nft_prices)) + sum(p * v for p, v in zip(p_gift, [15, 25, 50, 100]))
        if exact:
            assert ev / 250 == pytest.approx(0.87, abs=0.001)
        else:  # обычных подарков дороже 100 нет — RTP получается ниже цели, в пользу казино
            assert ev / 250 < 0.87
        # каждый NFT в среднем «съедает» равную долю
        shares = [p * v for p, v in zip(p_nft, nft_prices)]
        assert max(shares) == pytest.approx(min(shares))
    # дешёвые NFT: вероятность NFT не больше 50%, RTP не выше цели
    p_nft, p_gift = g.nft_case_weights(250, [260], [15, 25, 50, 100])
    ev = p_nft[0] * 260 + sum(p * v for p, v in zip(p_gift, [15, 25, 50, 100]))
    assert sum(p_nft) <= 0.5 + 1e-9 and ev / 250 <= 0.87 + 1e-9


def test_pvp_winner_proportional():
    rng = random.Random(7)
    wins = Counter(g.pvp_pick_winner([(1, 75), (2, 25)], rng)[0] for _ in range(20000))
    assert wins[1] / 20000 == pytest.approx(0.75, abs=0.02)
    assert g.pvp_payout(100) == 95
    assert g.pvp_payout(7) == 6


def test_payout_floors():
    assert g.payout(10, 1.9) == 19
    assert g.payout(3, 2.5) == 7
    assert g.payout(3, 5) == 15
    assert g.payout(5, 0.0) == 0


def test_hockey_zones_proportional():
    zones = g.hockey_zones([30, 10, 60])
    sizes = [y1 - y0 for y0, y1 in zones]
    assert sizes == pytest.approx([48, 16, 96])
    assert zones[0][0] == 0 and zones[-1][1] == g.HOCKEY_H


def test_hockey_shot_is_valid_path():
    for seed in range(300):
        rng = random.Random(seed)
        zone = (20.0, 45.0)
        shot = g.hockey_shot(zone, rng)
        pts = shot["points"]
        assert pts[0] == list(g.HOCKEY_START)
        tx, ty = pts[-1]
        assert 20 <= ty <= 45 and 0 <= tx <= g.HOCKEY_W        # останавливается в зоне победителя
        # сильный удар: путь не короче 1.5 длины поля, и длина ломаной совпадает с заявленной
        length = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
        assert length == pytest.approx(shot["length"], abs=0.05)
        assert length >= g.HOCKEY_MIN_PATH - 0.01
        for x, y in pts[1:-1]:                                  # каждая промежуточная точка — на борту
            on_wall = min(abs(x), abs(x - g.HOCKEY_W)) < 1e-6 or min(abs(y), abs(y - g.HOCKEY_H)) < 1e-6
            assert on_wall and -1e-6 <= x <= g.HOCKEY_W + 1e-6 and -1e-6 <= y <= g.HOCKEY_H + 1e-6


def test_hockey_directions_vary():
    angles = {round(g.hockey_shot((0, 160), random.Random(s))["angle"] / 45) for s in range(200)}
    assert len(angles) >= 6


def test_nft_cases_share_and_rtp():
    gifts = [15, 15, 25, 25, 50, 50, 50, 50, 100, 100, 100]
    for d in g.NFT_CASE_DEFS:
        price = d.price or 250
        p_nft, p_gift = g.nft_case_weights(price, [3000, 8000, 20000], gifts, share=d.share)
        ev = sum(p * v for p, v in zip(p_nft, [3000, 8000, 20000])) + sum(p * v for p, v in zip(p_gift, gifts))
        assert sum(p_nft) + sum(p_gift) == pytest.approx(1)
        assert g.CASE_MIN_RTP <= ev / price <= g.CASE_MAX_RTP, d.id          # и Premium за 1000 ★ отдаёт ~87%
