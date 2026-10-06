import time

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import CopyMessage

from app import broadcast as bc
from tests.test_casino import casino, fund  # noqa: F401  (фикстура casino)


async def test_audiences_and_list(casino):  # noqa: F811
    db = casino.db
    await db.touch_user(1, "payer", "Платил")
    await db.touch_user(2, "sleepy", "Спит")
    await db.touch_user(3, None, "Новичок")
    await fund(casino, 1, 100)
    await db.conn.execute("UPDATE users SET last_seen=? WHERE id=2", (time.time() - 8 * 86400,))
    assert await bc.audience(db, "all") == [1, 2, 3]
    assert await bc.audience(db, "paid") == [1]
    assert await bc.audience(db, "unpaid") == [2, 3]
    assert await bc.audience(db, "active") == [1, 3]
    assert await bc.audience(db, "sleeping") == [2]
    assert await bc.audience(db, "balance") == [1]
    assert (await bc.counts(db))["all"] == 3
    ids, missing = await bc.parse_list(db, "@PAYER, 3\n@nobody 999 @payer")
    assert ids == [1, 3] and missing == ["@nobody", "999"]


async def test_send_counts_blocked_and_waits_on_flood():
    calls = []

    class Bot:
        async def copy_message(self, chat_id, from_chat_id, message_id, reply_markup=None):
            calls.append(chat_id)
            m = CopyMessage(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id)
            if chat_id == 2:
                raise TelegramForbiddenError(method=m, message="blocked")
            if chat_id == 3 and calls.count(3) == 1:
                raise TelegramRetryAfter(method=m, message="flood", retry_after=0)
            return True

    res = await bc.send(Bot(), [1, 2, 3], 777, 5, pause=0)
    assert res == {"sent": 2, "blocked": 1, "failed": 0} and calls == [1, 2, 3, 3]
