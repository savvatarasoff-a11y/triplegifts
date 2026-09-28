"""Сквозной сценарий через диспетчер aiogram с поддельными Telegram и Claude."""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetBusinessConnection, SendChatAction, SendMessage, TelegramMethod
from aiogram.types import Update

from app import responder as responder_mod
from app.config import Config
from app.db import Database
from app.handlers.business import build_business_router
from app.handlers.callbacks import build_callbacks_router
from app.handlers.owner import build_owner_router
from app.llm import Reply
from app.main import build_strangers_router
from app.responder import Responder

OWNER = 111
FRIEND = 222
CONN = "bc1"


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self._msg_id = 1000

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        self.calls.append(method)
        if isinstance(method, SendMessage):
            self._msg_id += 1
            return {
                "message_id": self._msg_id,
                "date": int(time.time()),
                "chat": {"id": method.chat_id, "type": "private"},
                "text": method.text,
            }
        if isinstance(method, SendChatAction):
            return True
        if isinstance(method, GetBusinessConnection):
            return _connection(True)
        return True

    async def close(self) -> None:
        pass

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        yield b""

    def sent(self, chat_id: int) -> list[SendMessage]:
        return [c for c in self.calls if isinstance(c, SendMessage) and c.chat_id == chat_id]


class FakeLLM:
    def __init__(self) -> None:
        self.reply = Reply(messages=["ку", "ща гляну"], needs_owner=False)
        self.requests: list[dict] = []

    async def generate_reply(self, **kwargs: Any) -> Reply:
        self.requests.append(kwargs)
        return self.reply

    async def summarize_style(self, *args: Any) -> str:
        return "пишет коротко"

    async def read_screenshot(self, *args: Any, **kwargs: Any):
        return []


def _connection(can_reply: bool) -> dict:
    return {
        "id": CONN,
        "user": {"id": OWNER, "is_bot": False, "first_name": "Савва"},
        "user_chat_id": OWNER,
        "date": int(time.time()),
        "is_enabled": True,
        "rights": {"can_reply": can_reply},
    }


def _biz_message(uid: int, from_id: int, text: str, *, date: int | None = None) -> dict:
    return {
        "update_id": uid,
        "business_message": {
            "message_id": uid,
            "business_connection_id": CONN,
            "date": date or int(time.time()),
            "chat": {"id": FRIEND, "type": "private", "first_name": "Петя", "username": "petya"},
            "from": {"id": from_id, "is_bot": False, "first_name": "Петя" if from_id == FRIEND else "Савва"},
            "text": text,
        },
    }


def _owner_dm(uid: int, text: str) -> dict:
    entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else None
    msg = {
        "message_id": uid,
        "date": int(time.time()),
        "chat": {"id": OWNER, "type": "private", "first_name": "Савва"},
        "from": {"id": OWNER, "is_bot": False, "first_name": "Савва"},
        "text": text,
    }
    if entities:
        msg["entities"] = entities
    return {"update_id": uid, "message": msg}


def _callback(uid: int, data: str) -> dict:
    return {
        "update_id": uid,
        "callback_query": {
            "id": str(uid),
            "from": {"id": OWNER, "is_bot": False, "first_name": "Савва"},
            "chat_instance": "x",
            "data": data,
            "message": {
                "message_id": 5,
                "date": int(time.time()),
                "chat": {"id": OWNER, "type": "private"},
                "text": "карточка",
            },
        },
    }


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(responder_mod, "DEBOUNCE_SECONDS", 0)
    cfg = Config(
        bot_token="123:abc", anthropic_api_key="k", owner_id=OWNER, claude_model="m",
        owner_name="Савва", db_path=str(tmp_path / "t.db"), delay_min=0, delay_max=0,
        owner_pause_minutes=30, log_level="INFO",
    )
    db = Database(cfg.db_path)
    await db.connect()
    session = FakeSession()
    bot = Bot("123:abc", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    llm = FakeLLM()
    resp = Responder(bot, cfg, db, llm)
    dp = Dispatcher()
    dp.include_router(build_business_router(cfg, db, resp))
    dp.include_router(build_callbacks_router(cfg, db, resp))
    dp.include_router(build_owner_router(cfg, db, llm, resp))
    dp.include_router(build_strangers_router(OWNER))

    async def feed(raw: dict) -> None:
        await dp.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))

    async def settle() -> None:
        for _ in range(50):
            pending = [t for t in resp._pending.values() if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    yield {"db": db, "session": session, "llm": llm, "feed": feed, "settle": settle, "resp": resp}
    await db.close()


async def test_full_flow(env):
    db, session, llm, feed, settle = env["db"], env["session"], env["llm"], env["feed"], env["settle"]

    await feed({"update_id": 1, "business_connection": _connection(True)})
    assert (await db.get_connection(CONN))["can_reply"] == 1

    # Новый чат: только карточка владельцу, ответа нет
    await feed(_biz_message(2, FRIEND, "привет, как дела?"))
    await settle()
    card = session.sent(OWNER)[-1]
    assert "Новый чат" in card.text and card.reply_markup is not None
    assert session.sent(FRIEND) == []

    # Разрешаем чат -> по умолчанию режим черновиков
    await feed(_callback(3, f"c:allow:{FRIEND}"))
    await settle()
    draft_card = session.sent(OWNER)[-1]
    assert "Черновик" in draft_card.text
    assert session.sent(FRIEND) == []
    assert llm.requests[-1]["incoming"] == "привет, как дела?"

    # Нажимаем «Отправить»
    draft_id = int(draft_card.reply_markup.inline_keyboard[0][0].callback_data.split(":")[2])
    await feed(_callback(4, f"d:send:{draft_id}"))
    sent = session.sent(FRIEND)
    assert [m.text for m in sent] == ["ку", "ща гляну"]
    assert all(m.business_connection_id == CONN and m.parse_mode is None for m in sent)
    assert (await db.stats())[0]["n"] == 1

    # Выключаем черновики -> ответ уходит сам
    await feed(_owner_dm(5, "/draft off"))
    await feed(_biz_message(6, FRIEND, "го завтра в кино?"))
    await settle()
    assert len(session.sent(FRIEND)) == 4

    # Я написал сам -> пример сохранён, чат на паузе, бот молчит
    await feed(_biz_message(7, OWNER, "давай в 7"))
    examples = await db.examples()
    assert examples[-1]["reply"] == "давай в 7"
    assert examples[-1]["incoming"] is None  # бот уже ответил, моё сообщение — продолжение
    await feed(_biz_message(8, FRIEND, "ок"))
    await settle()
    assert len(session.sent(FRIEND)) == 4

    # Старые сообщения (бот был выключен) игнорируются
    await db.pause_chat(FRIEND, 0)
    await feed(_biz_message(9, FRIEND, "старое", date=int(time.time()) - 3600))
    await settle()
    assert len(session.sent(FRIEND)) == 4


async def test_needs_owner_notifies(env):
    db, session, llm, feed, settle = env["db"], env["session"], env["llm"], env["feed"], env["settle"]
    await feed({"update_id": 1, "business_connection": _connection(True)})
    await db.set_chat_status(FRIEND, "allow")
    await db.set_flag("draft_mode", False)
    llm.reply = Reply(messages=["ща гляну, отпишу"], needs_owner=True, owner_reason="встреча в субботу")
    await feed(_biz_message(2, FRIEND, "встретимся в субботу?"))
    await settle()
    assert [m.text for m in session.sent(FRIEND)] == ["ща гляну, отпишу"]
    assert any("Нужно твоё решение" in m.text for m in session.sent(OWNER))


async def test_owner_commands_and_examples(env):
    db, session, feed = env["db"], env["session"], env["feed"]
    await feed(_owner_dm(1, "/style пишу коротко, без точек, часто мат"))
    assert (await db.get_style())["description"] == "пишу коротко, без точек, часто мат"
    await feed(_owner_dm(2, "/examples"))
    await feed(_owner_dm(3, "мне: го гулять\nя: ща\nне могу"))
    ex = await db.examples()
    assert ex[-1] == {"id": ex[-1]["id"], "incoming": "го гулять", "reply": "ща\nне могу", "source": "manual"}
    await feed(_owner_dm(4, "/done"))
    assert "Профиль стиля" in session.sent(OWNER)[-1].text
    await feed(_owner_dm(5, "/off"))
    assert not await db.get_flag("enabled")


async def test_stranger_gets_refusal(env):
    session, feed = env["session"], env["feed"]
    raw = _owner_dm(1, "/stats")
    raw["message"]["from"]["id"] = 999
    raw["message"]["chat"]["id"] = 999
    await feed(raw)
    assert "личный бот" in session.sent(999)[-1].text
    assert session.sent(OWNER) == []


async def test_edit_draft(env):
    db, session, feed, settle = env["db"], env["session"], env["feed"], env["settle"]
    await feed({"update_id": 1, "business_connection": _connection(True)})
    await db.set_chat_status(FRIEND, "allow")
    await feed(_biz_message(2, FRIEND, "скинь фотки"))
    await settle()
    card = session.sent(OWNER)[-1]
    draft_id = int(card.reply_markup.inline_keyboard[0][0].callback_data.split(":")[2])
    await feed(_callback(3, f"d:edit:{draft_id}"))
    await feed(_owner_dm(4, "вечером скину\nща занят"))
    assert [m.text for m in session.sent(FRIEND)] == ["вечером скину", "ща занят"]
    ex = await db.examples()
    assert ex[-1]["reply"] == "вечером скину\nща занят" and ex[-1]["source"] == "edit"


async def test_owner_reply_before_bot_cancels_and_pairs(env):
    db, session, feed, settle, resp = env["db"], env["session"], env["feed"], env["settle"], env["resp"]
    await feed({"update_id": 1, "business_connection": _connection(True)})
    await db.set_chat_status(FRIEND, "allow")
    await db.set_flag("draft_mode", False)
    responder_mod.DEBOUNCE_SECONDS = 5  # бот ещё «думает»
    await feed(_biz_message(2, FRIEND, "ты где?"))
    await feed(_biz_message(3, OWNER, "дома"))
    await feed(_biz_message(4, OWNER, "а чо"))
    await settle()
    assert session.sent(FRIEND) == []
    ex = await db.examples()
    assert len(ex) == 1 and ex[0]["incoming"] == "ты где?" and ex[0]["reply"] == "дома\nа чо"
    assert (await db.get_chat(FRIEND)).paused_until > time.time() + 29 * 60
