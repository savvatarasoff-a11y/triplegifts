"""Вывод звёзд: подарок отправляется со звёзд аккаунта-релейера."""
import pytest

from app.casino import Casino
from app.config import Config
from app.db import Database
from app.relayer import Relayer, RelayerError
from app.withdraw import NEED_CONTACT, approve, approve_waiting


class Bot:
    def __init__(self):
        self.messages = []
        self.gifts = []

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))

    async def send_gift(self, **kw):              # старый путь через бота не должен использоваться
        self.gifts.append(kw)


class FakeRelayer:
    ready = True

    def __init__(self):
        self.sent = []
        self.known = set()
        self.error = None

    async def send_gift(self, gift_id, user_id, username, text=""):
        if self.error:
            raise RelayerError(self.error)
        if user_id not in self.known and not username:
            raise RelayerError("NEED_CONTACT")
        self.sent.append((gift_id, user_id))

    async def username(self):
        return "svag_relayer"


@pytest.fixture
async def env(tmp_path):
    cfg = Config(bot_token="1:x", admin_ids=frozenset({7}), webapp_url="", db_path=str(tmp_path / "w.db"),
                 port=0, min_bet=1, max_bet=1000, start_bonus=0, log_level="INFO")
    db = Database(cfg.db_path)
    await db.connect()
    await db.touch_user(42, "pepe", "P")
    await db.touch_user(44, None, "NoName")
    for uid in (42, 44):
        await db.credit_payment(f"c{uid}", uid, 200)
    yield Casino(db, cfg)
    await db.close()


async def test_withdraw_sent_by_relayer(env):
    casino, bot, relayer = env, Bot(), FakeRelayer()
    wd = await casino.withdraw_request(42, "5170145012310081615", 50, "🧸")
    relayer.error = "BALANCE_TOO_LOW"
    ok, text = await approve(bot, casino, wd["id"], 7, relayer)
    assert not ok and "звёзд релейера" in text
    assert (await casino.get_withdrawal(wd["id"]))["status"] == "pending"
    relayer.error = None
    ok, _ = await approve(bot, casino, wd["id"], 7, relayer)
    assert ok and relayer.sent == [("5170145012310081615", 42)] and bot.gifts == []
    assert (await casino.get_withdrawal(wd["id"]))["status"] == "sent"


async def test_withdraw_waits_for_contact(env):
    casino, bot, relayer = env, Bot(), FakeRelayer()
    wd = await casino.withdraw_request(44, "g1", 50, "🧸")
    ok, text = await approve(bot, casino, wd["id"], 7, relayer)
    assert not ok and "автоматически" in text
    row = await casino.get_withdrawal(wd["id"])
    assert row["status"] == "pending" and row["error"] == NEED_CONTACT
    assert bot.messages[-1][0] == 44 and "@svag_relayer" in bot.messages[-1][1]
    relayer.known.add(44)                                         # игрок написал релейеру
    await approve_waiting(bot, casino, relayer, 44)
    assert (await casino.get_withdrawal(wd["id"]))["status"] == "sent"


async def test_withdraw_needs_relayer(env):
    casino, bot, relayer = env, Bot(), FakeRelayer()
    relayer.ready = False
    wd = await casino.withdraw_request(42, "g1", 50, "🧸")
    ok, text = await approve(bot, casino, wd["id"], 7, relayer)
    assert not ok and "/relayer" in text
    assert (await casino.get_withdrawal(wd["id"]))["status"] == "pending"


async def test_relayer_send_gift_mtproto(tmp_path):
    from telethon.tl import types as T
    from telethon.tl.functions.payments import GetPaymentFormRequest, SendStarsFormRequest

    calls = []

    class Client:
        async def __call__(self, req):
            calls.append(req)
            if isinstance(req, GetPaymentFormRequest):
                return type("F", (), {"form_id": 5})()
            return True

        async def get_input_entity(self, target):
            if target == "pepe":
                return T.InputPeerUser(user_id=42, access_hash=1)
            raise ValueError

    db = Database(str(tmp_path / "r.db"))
    await db.connect()
    relayer = Relayer(db, "1:x")
    relayer.client = Client()
    await relayer.send_gift("5170145012310081615", 42, "pepe", "Вывод")
    form, pay = calls
    assert isinstance(form, GetPaymentFormRequest) and isinstance(pay, SendStarsFormRequest)
    assert isinstance(form.invoice, T.InputInvoiceStarGift) and form.invoice.gift_id == 5170145012310081615
    assert form.invoice.peer.user_id == 42 and pay.form_id == 5 and form.invoice.message.text == "Вывод"
    with pytest.raises(RelayerError, match="NEED_CONTACT"):
        await relayer.send_gift("1", 44, None)
    await db.close()


async def test_relayer_survives_bot_token_change(tmp_path):
    """Смена бота: /rekey кладёт копию настроек под новый токен, новый бот её подхватывает."""
    from app.db import Database
    from app.relayer import Relayer
    db = Database(str(tmp_path / "r.db"))
    await db.connect()
    old = Relayer(db, "111:old-token")
    await old.set_api(12345, "hash")
    await old._set("session", "SESSION")
    assert await old.stage_rekey("222:new-token") == 3
    assert await old._get("session") == "SESSION"                 # старый бот продолжает работать
    new = Relayer(db, "222:new-token")
    assert await new._get("session") == "SESSION" and await new._get("api_id") == "12345"
    assert await db.kv_get("relayer_next:session") is None         # копия перенесена в основные настройки
    assert await Relayer(db, "222:new-token")._get("api_hash") == "hash"
    await db.close()


async def test_relayer_restore_with_old_token(tmp_path):
    from app.db import Database
    from app.relayer import Relayer
    db = Database(str(tmp_path / "r2.db"))
    await db.connect()
    old = Relayer(db, "111:old-token")
    await old.set_api(12345, "hash")
    await old._set("session", "SESSION")
    new = Relayer(db, "222:new-token")
    assert await new._get("session") is None                       # новый бот старые настройки не читает
    assert await new.restore_from_token("333:wrong") == 0
    assert await new.restore_from_token("111:old-token") == 3
    assert await Relayer(db, "222:new-token")._get("session") == "SESSION"
    await db.close()
