"""Команды бота через диспетчер aiogram с поддельным Telegram."""
from __future__ import annotations

import time
from typing import Any

import pytest
from pydantic import TypeAdapter
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerPreCheckoutQuery, GetMe, SendDice, SendGift, SendInvoice, SendMessage, TelegramMethod
from aiogram.types import Update

from app.bot import build_router
from app.casino import Casino
from app.config import Config
from app.db import Database

ADMIN = 5349009098
PLAYER = 222


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.gift_error: str | None = None
        self.dice_value = 64

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        self.calls.append(method)
        if isinstance(method, SendGift) and self.gift_error:
            raise TelegramBadRequest(method=method, message=self.gift_error)
        return TypeAdapter(method.__returning__).validate_python(self._raw(method), context={"bot": bot})

    def _raw(self, method: TelegramMethod) -> Any:
        if isinstance(method, SendDice):
            return {"message_id": 77, "date": int(time.time()), "chat": {"id": method.chat_id, "type": "private"},
                    "dice": {"emoji": "🎰", "value": self.dice_value}}
        if isinstance(method, GetMe):
            return {"id": 1, "is_bot": True, "first_name": "Casino", "username": "casino_bot"}
        if isinstance(method, (SendMessage, SendInvoice)):
            return {"message_id": 1, "date": int(time.time()), "chat": {"id": method.chat_id, "type": "private"},
                    "text": getattr(method, "text", "")}
        return True

    async def close(self) -> None:
        pass

    async def stream_content(self, *a: Any, **kw: Any):  # pragma: no cover
        yield b""

    def texts(self, chat_id: int) -> list[str]:
        return [c.text for c in self.calls if isinstance(c, SendMessage) and c.chat_id == chat_id]


def msg(uid: int, user_id: int, text: str) -> dict:
    m = {"message_id": uid, "date": int(time.time()), "chat": {"id": user_id, "type": "private"},
         "from": {"id": user_id, "is_bot": False, "first_name": f"U{user_id}", "username": f"u{user_id}"}, "text": text}
    if text.startswith("/"):
        m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return {"update_id": uid, "message": m}


@pytest.fixture
async def env(tmp_path):
    cfg = Config(bot_token="1:x", admin_ids=frozenset({ADMIN}), webapp_url="https://casino.example",
                 db_path=str(tmp_path / "b.db"), port=0, min_bet=1, max_bet=1000, start_bonus=0, log_level="INFO")
    db = Database(cfg.db_path)
    await db.connect()
    casino = Casino(db, cfg)
    session = FakeSession()
    bot = Bot("1:x", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(build_router(cfg, casino))

    async def feed(raw: dict) -> None:
        await dp.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))

    yield {"feed": feed, "session": session, "casino": casino, "db": db}
    await db.close()


async def test_admin_check_flow(env):
    feed, session, db = env["feed"], env["session"], env["db"]
    await feed(msg(1, ADMIN, "/check 150 2"))
    text = session.texts(ADMIN)[-1]
    assert "Чек на 150 ⭐" in text and "https://t.me/casino_bot?start=c_" in text
    code = text.split("start=c_")[1].split()[0]

    await feed(msg(2, PLAYER, f"/start c_{code}"))
    assert any("+150 ⭐" in t for t in session.texts(PLAYER))
    assert (await db.get_user(PLAYER))["balance"] == 150
    await feed(msg(3, PLAYER, f"/start c_{code}"))
    assert "уже активировали" in session.texts(PLAYER)[-2]

    await feed(msg(4, ADMIN, "/checks"))
    assert code in session.texts(ADMIN)[-1]
    await feed(msg(5, ADMIN, f"/revoke {code}"))
    assert "отозван" in session.texts(ADMIN)[-1]
    await feed(msg(6, ADMIN, "/stats"))
    assert "Выдано по чекам: 150" in session.texts(ADMIN)[-1]


async def test_non_admin_cannot_issue_checks(env):
    feed, session, db = env["feed"], env["session"], env["db"]
    await feed(msg(1, PLAYER, "/check 1000000 100"))
    assert not any("Чек на" in t for t in session.texts(PLAYER))
    assert (await db.one("SELECT COUNT(*) n FROM checks"))["n"] == 0


async def test_stars_payment(env):
    feed, session, db = env["feed"], env["session"], env["db"]
    await feed(msg(1, PLAYER, "/deposit 100"))
    inv = [c for c in session.calls if isinstance(c, SendInvoice)][-1]
    assert inv.currency == "XTR" and inv.prices[0].amount == 100

    def pcq(uid, payload, amount, from_id=PLAYER):
        return {"update_id": uid, "pre_checkout_query": {
            "id": str(uid), "from": {"id": from_id, "is_bot": False, "first_name": "P"},
            "currency": "XTR", "total_amount": amount, "invoice_payload": payload}}

    await feed(pcq(2, inv.payload, 100))
    assert [c for c in session.calls if isinstance(c, AnswerPreCheckoutQuery)][-1].ok is True
    await feed(pcq(3, inv.payload, 1))          # сумма подменена
    assert [c for c in session.calls if isinstance(c, AnswerPreCheckoutQuery)][-1].ok is False
    await feed(pcq(4, inv.payload, 100, 999))   # чужой счёт
    assert [c for c in session.calls if isinstance(c, AnswerPreCheckoutQuery)][-1].ok is False

    paid = msg(5, PLAYER, "x")
    del paid["message"]["text"]
    paid["message"]["successful_payment"] = {
        "currency": "XTR", "total_amount": 100, "invoice_payload": inv.payload,
        "telegram_payment_charge_id": "tg-charge-1", "provider_payment_charge_id": ""}
    await feed(paid)
    await feed(paid)  # повторная доставка апдейта
    assert (await db.get_user(PLAYER))["balance"] == 100
    assert any("Зачислено <b>100 ⭐" in t for t in session.texts(PLAYER))


async def test_start_shows_play_button(env):
    feed, session = env["feed"], env["session"]
    await feed(msg(1, PLAYER, "/start"))
    last = [c for c in session.calls if isinstance(c, SendMessage)][-1]
    assert last.reply_markup.inline_keyboard[0][0].web_app.url == "https://casino.example"


def cb(uid: int, user_id: int, data: str) -> dict:
    return {"update_id": uid, "callback_query": {
        "id": str(uid), "from": {"id": user_id, "is_bot": False, "first_name": "A"}, "chat_instance": "x", "data": data,
        "message": {"message_id": 9, "date": int(time.time()), "chat": {"id": user_id, "type": "private"}, "text": "заявка"}}}


async def test_withdraw_admin_buttons(env):
    feed, session, casino, db = env["feed"], env["session"], env["casino"], env["db"]
    await db.touch_user(PLAYER, "p", "P")
    await db.credit_payment("c1", PLAYER, 200)
    wd = await casino.withdraw_request(PLAYER, "gift50", 50, "🧸")

    await feed(cb(1, PLAYER, f"w:ok:{wd['id']}"))            # не админ — ничего не происходит
    assert (await casino.get_withdrawal(wd["id"]))["status"] == "pending"

    session.gift_error = "BALANCE_TOO_LOW"
    await feed(cb(2, ADMIN, f"w:ok:{wd['id']}"))
    row = await casino.get_withdrawal(wd["id"])
    assert row["status"] == "pending" and "BALANCE_TOO_LOW" in row["error"]

    session.gift_error = None
    await feed(cb(3, ADMIN, f"w:ok:{wd['id']}"))
    assert (await casino.get_withdrawal(wd["id"]))["status"] == "sent"
    gift = [c for c in session.calls if isinstance(c, SendGift)][-1]
    assert gift.user_id == PLAYER and gift.gift_id == "gift50"
    assert any("Вывод №" in t for t in session.texts(PLAYER))

    wd2 = await casino.withdraw_request(PLAYER, "gift50", 50, "🧸")
    await feed(cb(4, ADMIN, f"w:no:{wd2['id']}"))
    assert (await casino.get_withdrawal(wd2["id"]))["status"] == "rejected"
    assert (await db.get_user(PLAYER))["balance"] == 150


async def test_slot_in_chat_uses_telegram_dice(env, monkeypatch):
    import app.bot as bot_mod
    async def no_sleep(_):
        return None
    monkeypatch.setattr(bot_mod.asyncio, "sleep", no_sleep)
    feed, session, db = env["feed"], env["session"], env["db"]
    await db.touch_user(PLAYER, "p", "P")
    await db.credit_payment("c1", PLAYER, 100)
    await feed(msg(1, PLAYER, "/slot 10"))                       # 🎰 выпало 64 = 777 -> ×5
    assert any(isinstance(c, SendDice) and c.emoji == "🎰" for c in session.calls)
    assert "7️⃣ 7️⃣ 7️⃣" in session.texts(PLAYER)[-1] and "×5" in session.texts(PLAYER)[-1]
    assert (await db.get_user(PLAYER))["balance"] == 140
    session.dice_value = 3                                        # 🍋 BAR BAR — пара, возврат ставки
    await feed(msg(2, PLAYER, "/slot 10"))
    assert "ставка возвращена" in session.texts(PLAYER)[-1]
    assert (await db.get_user(PLAYER))["balance"] == 140
    await feed(msg(3, PLAYER, "/slot 500"))                      # не хватает звёзд — кубик не бросается
    assert "Недостаточно" in session.texts(PLAYER)[-1]
    assert sum(isinstance(c, SendDice) for c in session.calls) == 2
