import math
import random
from collections import Counter

import pytest

from app.games import logic as g


def test_slots_rtp_below_one():
    assert 0.89 < g.slots_rtp() < 0.91


def test_slots_multiplier_rules():
    assert g.slots_multiplier(["💎", "💎", "💎"]) == 1000
    assert g.slots_multiplier(["7️⃣", "7️⃣", "7️⃣"]) == 250
    assert g.slots_multiplier(["💎", "🍋", "💎"]) == 10
    assert g.slots_multiplier(["🍒", "🍒", "🍋"]) == 2
    assert g.slots_multiplier(["7️⃣", "7️⃣", "🍋"]) == 0
    assert g.slots_multiplier(["🍋", "🍒", "🍇"]) == 0


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


def test_roulette_rtp_american():
    for bet_type in ["red", "black", "even", "odd", "low", "high", "dozen1", "dozen2", "dozen3"]:
        ev = sum(g.roulette_multiplier(bet_type, None, n) for n in range(38)) / 38
        assert ev == pytest.approx(36 / 38)
    for value in (17, 0, g.DOUBLE_ZERO):
        ev = sum(g.roulette_multiplier("number", value, n) for n in range(38)) / 38
        assert ev == pytest.approx(36 / 38)
    assert g.roulette_multiplier("red", None, 0) == 0 and g.roulette_multiplier("even", None, g.DOUBLE_ZERO) == 0
    assert g.roulette_color(0) == "green" and g.roulette_color(37) == "green"
    assert g.roulette_color(1) == "red" and g.roulette_color(2) == "black"
    assert g.roulette_label(37) == "00" and g.roulette_label(5) == "5"
    assert {g.roulette_spin(random.Random(i)) for i in range(2000)} == set(range(38))


def test_mines_multiplier_expected_value():
    # EV любого стоп-правила = 0.95: вероятность пройти k клеток × множитель
    for mines in (2, 3, 10, 24):
        for k in range(1, 25 - mines + 1):
            survive = math.comb(25 - mines, k) / math.comb(25, k)
            assert survive * g.mines_multiplier(mines, k) == pytest.approx(0.95, rel=1e-3)
    assert g.mines_multiplier(3, 0) == 1.0
    assert len(set(g.mines_place(5))) == 5
    assert g.mines_multiplier(2, 1) > 1
    with pytest.raises(ValueError):
        g.mines_place(1)


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


def test_cases_have_house_edge():
    for case in g.CASES:
        assert 0.85 < case.expected_value() / case.price < 0.88
        assert max(p for p, _, _ in case.prizes) == case.price * 100
        assert g.case_open(case) in {(p, gift) for p, _, gift in case.prizes}


def test_pvp_winner_proportional():
    rng = random.Random(7)
    wins = Counter(g.pvp_pick_winner([(1, 75), (2, 25)], rng)[0] for _ in range(20000))
    assert wins[1] / 20000 == pytest.approx(0.75, abs=0.02)
    assert g.pvp_payout(100) == 95
    assert g.pvp_payout(7) == 6


def test_payout_floors():
    assert g.payout(10, 1.9) == 19
    assert g.payout(3, 2.5) == 7
    assert g.payout(5, 0.0) == 0
