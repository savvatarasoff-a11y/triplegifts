"""Канал: оформление, приветственный пост, автопостинг крупных выигрышей."""
import json

import pytest

from app import channel
from app.casino import Casino
from app.config import Config
from app.db import Database


class ChanBot:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    async def get_me(self):
        return type("Me", (), {"username": "triple_bot"})()

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            if name in self.fail:
                raise RuntimeError("not enough rights")
            self.calls.append((name, args, kwargs))
            return type("Msg", (), {"message_id": 5})()
        return call


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "c.db"))
    await d.connect()
    yield d
    await d.close()


def test_banner_and_text():
    png = channel.banner()
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 20_000
    text = channel.welcome_text()
    assert "Triple Gifts" in text and "18+" in text


async def test_setup_posts_and_pins():
    bot = ChanBot()
    report = await channel.setup(bot)
    names = [c[0] for c in bot.calls]
    assert names == ["set_chat_title", "set_chat_description", "set_chat_photo", "send_photo", "pin_chat_message"]
    assert all(r.startswith("✅") for r in report)
    kb = bot.calls[3][2]["reply_markup"]
    assert kb.inline_keyboard[0][0].url == "https://t.me/triple_bot?start=channel"
    # нет прав на изменение профиля — пост всё равно публикуется, в отчёте видно, что не вышло
    bot = ChanBot(fail={"set_chat_title", "set_chat_photo"})
    report = await channel.setup(bot)
    assert sum(r.startswith("⚠️") for r in report) == 2 and "send_photo" in [c[0] for c in bot.calls]


async def test_post_big_wins_and_nft(db):
    cfg = Config(bot_token="1:x", admin_ids=frozenset(), webapp_url="", db_path="", port=0, min_bet=1,
                 max_bet=100000, start_bonus=0, log_level="INFO")
    Casino(db, cfg)
    await db.touch_user(1, "vasya", "Вася")
    bot = ChanBot()
    assert await channel.post_wins(bot, db) == 0                        # первый запуск — только метка

    async def bet(game, b, w, detail):
        async with db.tx() as c:
            await db.log_bet(c, 1, game, b, w, json.dumps(detail))

    await bet("slots", 10, 50, {})                                      # мелкий — не постим
    await bet("crash", 100, 2500, {})                                   # ×25 и 2500 ⭐ — постим
    await bet("case", 50, 9000, {"kind": "nft", "demo_nft": "Frog", "title": "Plush Pepe"})
    assert await channel.post_wins(bot, db) == 2
    texts = [c[1][1] for c in bot.calls]
    assert "2 500 ⭐" in texts[0] and "Краш" in texts[0]
    assert "Plush Pepe «Frog»" in texts[1] and "демо" in texts[1]
    assert await channel.post_wins(bot, db) == 0                        # повторно не постит
    await db.kv_set(channel.WINS_ON_KEY, "off")
    await bet("crash", 100, 5000, {})
    assert await channel.post_wins(bot, db) == 0
