import math
import random
from collections import Counter

import pytest

from app.games import logic as g


def test_slots_rtp_below_one():
    assert 0.9 < g.slots_rtp() < 0.97


def test_slots_multiplier_rules():
    assert g.slots_multiplier(["7️⃣", "7️⃣", "7️⃣"]) == 180
    assert g.slots_multiplier(["🍒", "🍒", "🍋"]) == 3
    assert g.slots_multiplier(["🍋", "🍒", "🍇"]) == 0.4
    assert g.slots_multiplier(["🍋", "🍇", "🔔"]) == 0


def test_dice_expected_value():
    for chance in (1, 10, 50, 95):
        ev = chance / 100 * g.dice_multiplier(chance)
        assert ev == pytest.approx(0.97, abs=0.001)
    with pytest.raises(ValueError):
        g.dice_roll(96)


def test_dice_roll_range():
    rng = random.Random(1)
    for _ in range(1000):
        roll, win, mult = g.dice_roll(50, rng)
        assert 0 <= roll < 100
        assert win == (roll < 50)
        assert mult == (1.94 if win else 0)


def test_roulette_rtp():
    for bet_type in ["red", "black", "even", "odd", "low", "high", "dozen1", "dozen2", "dozen3"]:
        ev = sum(g.roulette_multiplier(bet_type, None, n) for n in range(37)) / 37
        assert ev == pytest.approx(36 / 37)
    ev = sum(g.roulette_multiplier("number", 17, n) for n in range(37)) / 37
    assert ev == pytest.approx(36 / 37)
    assert g.roulette_multiplier("red", None, 0) == 0
    assert g.roulette_color(0) == "green" and g.roulette_color(1) == "red" and g.roulette_color(2) == "black"


def test_mines_multiplier_expected_value():
    # EV любого стоп-правила = 0.97: вероятность пройти k клеток × множитель
    for mines in (1, 3, 10, 24):
        for k in range(1, 25 - mines + 1):
            survive = math.comb(25 - mines, k) / math.comb(25, k)
            assert survive * g.mines_multiplier(mines, k) == pytest.approx(0.97, rel=1e-3)
    assert g.mines_multiplier(3, 0) == 1.0
    assert len(set(g.mines_place(5))) == 5


def test_crash_distribution():
    rng = random.Random(42)
    points = [g.crash_point(rng) for _ in range(200_000)]
    assert min(points) >= 1.0
    # P(point >= 2) ≈ 0.97 / 2
    share = sum(1 for p in points if p >= 2) / len(points)
    assert share == pytest.approx(0.485, abs=0.01)


def test_crash_time_roundtrip():
    for m in (1.0, 1.5, 2.0, 10.0):
        assert g.crash_multiplier_at(g.crash_time_of(m) + 1e-6) == pytest.approx(m, abs=0.011)


def test_cases_have_house_edge():
    for case in g.CASES:
        assert case.expected_value() < case.price
        assert g.case_open(case) in {p for p, _ in case.prizes}


def test_pvp_winner_proportional():
    rng = random.Random(7)
    wins = Counter(g.pvp_pick_winner([(1, 75), (2, 25)], rng)[0] for _ in range(20000))
    assert wins[1] / 20000 == pytest.approx(0.75, abs=0.02)
    assert g.pvp_payout(100) == 95
    assert g.pvp_payout(7) == 6


def test_payout_floors():
    assert g.payout(10, 1.94) == 19
    assert g.payout(3, 0.4) == 1
    assert g.payout(5, 0.0) == 0
