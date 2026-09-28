"""Имитация человека: пауза перед ответом, «печатает…», отправка по частям."""
from __future__ import annotations

import asyncio
import random

from aiogram import Bot
from aiogram.enums import ChatAction

TYPING_REFRESH = 4.5  # статус «печатает» живёт ~5 секунд
CHARS_PER_SECOND = 7.0


def typing_seconds(text: str) -> float:
    """Сколько «печатать» сообщение: примерно как быстрый набор на телефоне, 1.5–12 секунд."""
    return max(1.5, min(12.0, len(text) / CHARS_PER_SECOND))


def plan_delays(parts: list[str], delay_min: int, delay_max: int) -> tuple[float, list[float]]:
    """Возвращает (молчаливая пауза до начала набора, время набора каждой части).

    Общая задержка до первого сообщения укладывается в [delay_min, delay_max].
    """
    total_before_first = random.uniform(delay_min, delay_max)
    typing = [typing_seconds(p) for p in parts]
    first_typing = min(typing[0], total_before_first) if typing else 0.0
    silent = max(0.0, total_before_first - first_typing)
    if typing:
        typing[0] = first_typing
    return silent, typing


async def _typing(bot: Bot, chat_id: int, connection_id: str, seconds: float) -> None:
    remaining = seconds
    while remaining > 0:
        try:
            await bot.send_chat_action(
                chat_id=chat_id, action=ChatAction.TYPING, business_connection_id=connection_id
            )
        except Exception:
            pass  # статус «печатает» не критичен
        step = min(TYPING_REFRESH, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def send_humanlike(
    bot: Bot,
    chat_id: int,
    connection_id: str,
    parts: list[str],
    delay_min: int,
    delay_max: int,
    wait: bool = True,
) -> None:
    """Отправляет части ответа как человек. wait=False — без начальной паузы (например, из черновика)."""
    silent, typing = plan_delays(parts, delay_min, delay_max)
    if wait and silent:
        await asyncio.sleep(silent)
    for i, part in enumerate(parts):
        seconds = typing[i] if (wait or i > 0) else min(typing[i], 2.0)
        await _typing(bot, chat_id, connection_id, seconds)
        await bot.send_message(
            chat_id=chat_id, text=part, business_connection_id=connection_id, parse_mode=None
        )
        if i < len(parts) - 1:
            await asyncio.sleep(random.uniform(0.4, 1.5))
