import pytest

from app.config import Config, ConfigError


def test_defaults_need_only_token(monkeypatch):
    for name in ("ADMIN_IDS", "DB_PATH", "MIN_BET", "MAX_BET", "START_BONUS", "WEBAPP_URL", "RAILWAY_PUBLIC_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    cfg = Config.from_env()
    assert cfg.admin_ids == frozenset({5349009098})
    assert cfg.min_bet == 1 and cfg.max_bet == 10000


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("ADMIN_IDS", "1, 2")
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "svag.up.railway.app")
    cfg = Config.from_env()
    assert cfg.admin_ids == frozenset({1, 2})
    assert cfg.webapp_url == "https://svag.up.railway.app"


def test_token_required(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        Config.from_env()
