from app.humanize import plan_delays, typing_seconds


def test_typing_seconds_bounds():
    assert typing_seconds("") == 1.5
    assert typing_seconds("x" * 1000) == 12.0


def test_plan_delays_within_range():
    for _ in range(200):
        silent, typing = plan_delays(["привет", "как дела у тебя сегодня"], 5, 60)
        assert 5 <= silent + typing[0] <= 60
        assert silent >= 0
        assert len(typing) == 2
