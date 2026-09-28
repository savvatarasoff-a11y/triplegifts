"""Настройки: несекретные значения вшиты в код, токен бота — только из переменной окружения BOT_TOKEN."""
from __future__ import annotations

import os
from dataclasses import dataclass


# Значения по умолчанию: репозиторий приватный, поэтому несекретные настройки вшиты в код.
# Любое из них можно переопределить одноимённой переменной окружения.
DEFAULT_ADMIN_IDS = "5349009098"   # @wodoias
DEFAULT_DB_PATH = "/data/bot.db" if os.path.isdir("/data") else "bot.db"
DEFAULT_MIN_BET = 1
DEFAULT_MAX_BET = 10000
DEFAULT_START_BONUS = 0


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Не задана переменная окружения {name}")
    return value


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} должна быть числом, получено: {raw!r}") from e


def _ids(name: str, default: str = "") -> frozenset[int]:
    raw = (os.getenv(name, "").strip() or default).replace(";", ",").replace(" ", ",")
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            raise ConfigError(f"{name}: «{part}» — не числовой ID (узнать через @userinfobot)")
        ids.add(int(part))
    return frozenset(ids)


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    webapp_url: str
    db_path: str
    port: int
    min_bet: int
    max_bet: int
    start_bonus: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        webapp_url = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
        if not webapp_url and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
            webapp_url = f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN'].strip()}"
        min_bet = max(1, _int("MIN_BET", DEFAULT_MIN_BET))
        return cls(
            bot_token=_required("BOT_TOKEN"),
            admin_ids=_ids("ADMIN_IDS", DEFAULT_ADMIN_IDS),
            webapp_url=webapp_url,
            db_path=os.getenv("DB_PATH", "").strip() or DEFAULT_DB_PATH,
            port=_int("PORT", 8080),
            min_bet=min_bet,
            max_bet=max(min_bet, _int("MAX_BET", DEFAULT_MAX_BET)),
            start_bonus=max(0, _int("START_BONUS", DEFAULT_START_BONUS)),
            log_level=(os.getenv("LOG_LEVEL", "").strip() or "INFO").upper(),
        )

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids
