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


def test_plinko_tables():
    for (rows, risk), table in g.PLINKO_TABLES.items():
        assert len(table) == rows + 1 and table == table[::-1]                  # симметрия
        assert sum(g.plinko_probs(rows)) == pytest.approx(1)
        assert 0.92 < g.plinko_rtp(rows, risk) <= g.PLINKO_RTP                 # ≈ 93%, не выше 94%
    path, bucket, mult = g.plinko_drop(12, "medium")
    assert len(path) == 12 and bucket == sum(path) and mult == g.PLINKO_TABLES[(12, "medium")][bucket]


def test_pickaxe_physics_and_rtp():
    import random
    assert 0.86 < g.pickaxe_rtp() <= g.PICKAXE_RTP                             # с учётом «кирка не выпала»
    assert abs(sum(p for _, p in g.PICK_WHEEL) - 1) < 1e-9 and dict(g.PICK_WHEEL)["none"] > 0
    rng = random.Random(11)
    run = g.pickaxe_run("wood", rng=rng)
    ev = run["events"]
    assert ev and ev[-1]["hp"] == 0 and all(a["t"] <= b["t"] for a, b in zip(ev, ev[1:]))
    assert all(len(row) == g.PICK_COLS for row in run["world"]) and set(run["world"][0]) == {"g"}   # сверху трава
    # из барабана: центр спрайта — в центре барабана, физика ведёт центр тяжести (он ближе к наконечнику)
    com = [(g.PICK_COM[i] - 8) / 16 * g.PICK_SIZE for i in (0, 1)]
    assert run["start"]["a"] == 0 and run["start"]["x"] == pytest.approx(g.PICK_START[0] + com[0])
    assert run["start"]["y"] == pytest.approx(g.PICK_START[1] + com[1]) and com[0] > 0 and com[1] < 0
    codes = {v: k for k, v in g.PICK_CODES.items()}
    # прочность: −PICK_TOUCH за касание блока, стены и выталкивания бесплатны, верстак чинит полностью
    lv = 0
    hp = g.PICK_TIERS["wood"]
    for e in ev:
        if e["k"] == 1:
            hp -= g.PICK_TOUCH
        benches = sum(1 for bx, by, _ in e["br"] if run["world"][by][bx] == "b")
        if benches:                                                            # верстак: уровень выше и полная прочность
            lv = min(lv + benches, len(g.PICK_ORDER) - 1)
            hp = g.PICK_TIERS[g.PICK_ORDER[lv]]
            assert e["lv"] == g.PICK_ORDER[lv] and e["mx"] == hp
        else:
            assert "lv" not in e
        assert e["hp"] == max(0, hp)
        assert (e["c"] is not None) == (e["k"] == 1) and (e["k"] == 1 or not e["br"])
    # блок ломается не раньше, чем получит столько ударов, сколько у него HP (или от взрыва TNT)
    hits, broke = {}, {}
    for e in ev:
        if e["c"]:
            hits[tuple(e["c"])] = hits.get(tuple(e["c"]), 0) + 1
            if any((bx, by) == tuple(e["c"]) for bx, by, _ in e["br"]):
                assert hits[tuple(e["c"])] == g.PICK_HARD[codes[run["world"][e["c"][1]][e["c"][0]]]]
        for bx, by, m in e["br"]:
            broke[(bx, by)] = m
    assert run["mult"] == round(min(sum(broke.values()), g.PICKAXE_MAX_X), 2)
    # траектория не зависит от прочности: короткая партия — начало длинной (на этом стоит калибровка)
    strip = lambda e: {k: v for k, v in e.items() if k not in ("hp", "lv", "mx")}
    for start in g.PICK_ORDER[:-1]:
        short = g.pickaxe_run(start, rng=random.Random(5))
        long_ = g.pickaxe_run("diamond", rng=random.Random(5), track=(start,))
        assert [strip(e) for e in short["events"]] == [strip(e) for e in long_["events"][:len(short["events"])]]
        assert long_["snaps"][start] == short["counts"]
    # колесо выдаёт кирки примерно с заявленными шансами
    spins = [g.pickaxe_spin(rng) for _ in range(20000)]
    for tier, p in g.PICK_WHEEL:
        assert abs(spins.count(tier) / len(spins) - p) < 0.02, tier


def test_mines_multiplier_expected_value():
    # EV любого стоп-правила = 0.90: вероятность пройти k клеток × множитель
    for mines in (3, 10, 24):
        for k in range(1, 25 - mines + 1):
            survive = math.comb(25 - mines, k) / math.comb(25, k)
            assert survive * g.mines_multiplier(mines, k) == pytest.approx(1 - g.MINES_EDGE, rel=1e-3)
    assert g.mines_multiplier(5, 0) == 1.0
    assert len(set(g.mines_place(5))) == 5
    assert g.mines_multiplier(5, 1) > 1                      # с минимумом мин первая клетка уже в плюс
    with pytest.raises(ValueError):
        g.mines_place(4)


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
        p_nft, p_gift = g.nft_case_weights(price, [3000, 8000, 20000], gifts, target_rtp=d.rtp, share=d.share)
        ev = sum(p * v for p, v in zip(p_nft, [3000, 8000, 20000])) + sum(p * v for p, v in zip(p_gift, gifts))
        assert sum(p_nft) + sum(p_gift) == pytest.approx(1)
        assert d.rtp - g.CASE_RTP_TOLERANCE <= ev / price <= min(d.rtp + 0.01, g.CASE_MAX_RTP), d.id
    # чем дороже NFT-кейс, тем меньше он возвращает
    rtps = [d.rtp for d in sorted(g.NFT_CASE_DEFS, key=lambda d: d.price or 250)]
    assert rtps == sorted(rtps, reverse=True) and rtps[-1] < rtps[0]


def test_house_edge_everywhere_even_with_max_rakeback():
    """Сводка математики: возврат игроку + максимальный рейкбек (1%) < 100% в каждой игре."""
    rake = max(lv[3] for lv in g.LEVELS)
    rtp = {
        "slots": g.slots_rtp(),                                   # 777 оценён ровно ×40 — NFT не дороже
        "plinko": max(g.plinko_rtp(r, k) for r, k in g.PLINKO_TABLES),
        "pickaxe": g.pickaxe_rtp(),
        "mines": 1 - g.MINES_EDGE,                                # любая стратегия вывода
        "crash": 0.95,                                            # P(x ≥ m) = 0.95 / m
        "cases": g.CASE_MAX_RTP,                                  # выше — кейс выключается
        "upgrade": 1 - g.UPGRADE_EDGE,
        "pvp": 1 - g.PVP_COMMISSION,
    }
    for game, value in rtp.items():
        assert value + rake < 1, game
    # краш: вероятность дожить до ×m у формулы crash_point
    import random
    rng = random.Random(7)
    points = [g.crash_point(rng) for _ in range(200_000)]
    for m in (1.5, 2, 5):
        assert sum(p >= m for p in points) / len(points) == pytest.approx(0.95 / m, rel=0.03)
    # мины: EV = 0.9 для любого числа мин и открытых клеток
    for mines in (3, 5, 10, 24):
        for k in range(1, 25 - mines + 1):
            assert math.comb(25 - mines, k) / math.comb(25, k) * g.mines_multiplier(mines, k) <= 1 - g.MINES_EDGE + 1e-3
