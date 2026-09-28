import math
import random
from collections import Counter

import pytest

from app.games import logic as g


def test_slots_rtp_below_one():
    assert 0.95 < g.slots_rtp() < 0.97


def test_slots_multiplier_rules():
    assert g.slots_multiplier(["💎", "💎", "💎"]) == 1000
    assert g.slots_multiplier(["7️⃣", "7️⃣", "7️⃣"]) == 250
    assert g.slots_multiplier(["💎", "🍋", "💎"]) == 10
    assert g.slots_multiplier(["🍒", "🍒", "🍋"]) == 2.5
    assert g.slots_multiplier(["🍋", "🍒", "🍇"]) == 0


def test_dice_expected_value():
    for chance in (0.01, 1, 10, 49.5, 50, 98):
        ev = chance / 100 * g.dice_multiplier(chance)
        assert ev == pytest.approx(0.99, abs=0.001)
    assert g.dice_multiplier(50) == 1.98 and g.dice_multiplier(0.01) == 9900
    for bad in (0, 98.01, 1.001):
        with pytest.raises(ValueError):
            g.dice_roll(bad)


def test_dice_roll_under_and_over():
    rng = random.Random(1)
    for _ in range(1000):
        roll, win, mult = g.dice_roll(50, False, rng)
        assert 0 <= roll < 100 and win == (roll < 50) and mult == (1.98 if win else 0)
        roll, win, _ = g.dice_roll(25, True, rng)
        assert win == (roll >= 75)
    # «больше» с шансом 0.01% выигрывает ровно на 99.99
    assert sum(g.dice_roll(0.01, True, random.Random(i))[1] for i in range(3000)) < 5


def test_roulette_rtp():
    for bet_type in ["red", "black", "even", "odd", "low", "high", "dozen1", "dozen2", "dozen3"]:
        ev = sum(g.roulette_multiplier(bet_type, None, n) for n in range(37)) / 37
        assert ev == pytest.approx(36 / 37)
    ev = sum(g.roulette_multiplier("number", 17, n) for n in range(37)) / 37
    assert ev == pytest.approx(36 / 37)
    assert g.roulette_multiplier("red", None, 0) == 0
    assert g.roulette_color(0) == "green" and g.roulette_color(1) == "red" and g.roulette_color(2) == "black"


def test_mines_multiplier_expected_value():
    # EV любого стоп-правила = 0.99: вероятность пройти k клеток × множитель
    for mines in (1, 3, 10, 24):
        for k in range(1, 25 - mines + 1):
            survive = math.comb(25 - mines, k) / math.comb(25, k)
            assert survive * g.mines_multiplier(mines, k) == pytest.approx(0.99, rel=1e-3)
    assert g.mines_multiplier(3, 0) == 1.0
    assert len(set(g.mines_place(5))) == 5


def test_crash_distribution():
    rng = random.Random(42)
    points = [g.crash_point(rng) for _ in range(200_000)]
    assert min(points) >= 1.0
    # P(point >= 2) ≈ 0.99 / 2, P(point >= 10) ≈ 0.099
    share = sum(1 for p in points if p >= 2) / len(points)
    assert share == pytest.approx(0.495, abs=0.01)
    share = sum(1 for p in points if p >= 10) / len(points)
    assert share == pytest.approx(0.099, abs=0.005)


def test_crash_time_roundtrip():
    for m in (1.0, 1.5, 2.0, 10.0):
        assert g.crash_multiplier_at(g.crash_time_of(m) + 1e-6) == pytest.approx(m, abs=0.011)


def test_cases_have_house_edge():
    for case in g.CASES:
        assert 0.88 < case.expected_value() / case.price < 0.92
        assert max(p for p, _, _ in case.prizes) == case.price * 100
        assert g.case_open(case) in {(p, gift) for p, _, gift in case.prizes}


def test_pvp_winner_proportional():
    rng = random.Random(7)
    wins = Counter(g.pvp_pick_winner([(1, 75), (2, 25)], rng)[0] for _ in range(20000))
    assert wins[1] / 20000 == pytest.approx(0.75, abs=0.02)
    assert g.pvp_payout(100) == 95
    assert g.pvp_payout(7) == 6


def test_payout_floors():
    assert g.payout(10, 1.98) == 19
    assert g.payout(3, 2.5) == 7
    assert g.payout(5, 0.0) == 0
