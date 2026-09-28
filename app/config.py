"""Настройки из переменных окружения. Секреты в коде не хранятся."""
from __future__ import annotations

import os
from dataclasses import dataclass


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


@dataclass(frozen=True)
class Config:
    bot_token: str
    anthropic_api_key: str
    owner_id: int
    claude_model: str
    owner_name: str
    db_path: str
    delay_min: int
    delay_max: int
    owner_pause_minutes: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        owner_raw = _required("OWNER_ID")
        try:
            owner_id = int(owner_raw)
        except ValueError as e:
            raise ConfigError("OWNER_ID должен быть числом (узнать у @userinfobot)") from e
        delay_min = max(0, _int("REPLY_DELAY_MIN", 5))
        delay_max = max(delay_min, _int("REPLY_DELAY_MAX", 60))
        return cls(
            bot_token=_required("BOT_TOKEN"),
            anthropic_api_key=_required("ANTHROPIC_API_KEY"),
            owner_id=owner_id,
            claude_model=os.getenv("CLAUDE_MODEL", "").strip() or "claude-opus-5-5",
            owner_name=os.getenv("OWNER_NAME", "").strip() or "Савва",
            db_path=os.getenv("DB_PATH", "").strip() or "bot.db",
            delay_min=delay_min,
            delay_max=delay_max,
            owner_pause_minutes=max(0, _int("OWNER_PAUSE_MINUTES", 30)),
            log_level=(os.getenv("LOG_LEVEL", "").strip() or "INFO").upper(),
        )

    def secrets(self) -> list[str]:
        return [self.bot_token, self.anthropic_api_key]
