import pytest

from app.games import logic as g


@pytest.fixture(autouse=True)
def no_newbie_bonus(request, monkeypatch):
    """Тесты игр сверяют точные выплаты — бонус новичка в них выключен; его проверяют тесты с меткой newbie."""
    if "newbie" not in request.keywords:
        monkeypatch.setattr(g, "NEWBIE_GAMES", 0)
