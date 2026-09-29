"""Валюты казино: звёзды Telegram и TON.

Звёзды хранятся целыми звёздами, TON — в nanoTON (1 TON = 10⁹), поэтому вся игровая
математика остаётся целочисленной и одинаковой для обеих валют.
"""
from __future__ import annotations

import math
from typing import Any

STARS, TON = "stars", "ton"
CURRENCIES = (STARS, TON)
NANO = 1_000_000_000

TON_MIN_BET = NANO // 100          # 0.01 TON
TON_MAX_BET = 100 * NANO           # 100 TON
TON_MIN_WITHDRAW = NANO // 10      # 0.1 TON
TON_MAX_WITHDRAW = 10_000 * NANO


class CurrencyError(ValueError):
    pass


def check(cur: Any) -> str:
    if cur is None:
        return STARS
    if cur not in CURRENCIES:
        raise CurrencyError("Неизвестная валюта")
    return cur


def column(cur: str) -> str:
    """Столбец баланса в users."""
    return "balance" if cur == STARS else "ton"


def fmt(amount: int, cur: str) -> str:
    if cur == STARS:
        return f"{amount} ⭐"
    ton = amount / NANO
    text = f"{ton:.4f}".rstrip("0").rstrip(".") if ton < 1 else f"{ton:.2f}".rstrip("0").rstrip(".")
    return f"{text or '0'} TON"


def stars_to(amount_stars: int, cur: str, rate: float | None, up: bool = False) -> int | None:
    """Звёзды → валюта. rate — сколько звёзд стоит 1 TON."""
    if cur == STARS:
        return amount_stars
    if not rate:
        return None
    value = amount_stars / rate * NANO
    return int(math.ceil(value)) if up else int(value)


def to_stars(amount: int, cur: str, rate: float | None) -> int | None:
    if cur == STARS:
        return amount
    if not rate:
        return None
    return int(amount / NANO * rate)
