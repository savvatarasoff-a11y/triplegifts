"""Модели NFT у релейера: синхронизация, цены с маркета, выдача случайного подарка модели."""
import time

import pytest

from app import nft
from app.config import Config
from app.db import Database
from app.relayer import Relayer, RelayerError


def item(ref, collection, name, number, model, transfer=0):
    return {"ref": ref, "collection_id": collection, "collection_name": name, "number": number, "model": model,
            "rarity": 1.5, "emoji": "🐸", "transfer_stars": transfer}


class FakeRelayer:
    ready = True

    def __init__(self, inventory, prices=None, known_users=None):
        self.items = inventory
        self.prices = prices or {}
        self.known = set(known_users or [])
        self.transfers = []

    async def inventory(self):
        return list(self.items)

    async def floor_price(self, collection_id, model):
        return self.prices.get((collection_id, model))

    async def transfer(self, it, user_id, username):
        if user_id not in self.known and not username:
            raise RelayerError("NEED_CONTACT")
        self.transfers.append((it["ref"], user_id))
        self.items = [i for i in self.items if i["ref"] != it["ref"]]

    async def username(self):
        return "svag_relayer"


class Bot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))


@pytest.fixture
async def env(tmp_path):
    cfg = Config(bot_token="1:x", admin_ids=frozenset({7}), webapp_url="", db_path=str(tmp_path / "n.db"),
                 port=0, min_bet=1, max_bet=1000, start_bonus=0, log_level="INFO")
    db = Database(cfg.db_path)
    await db.connect()
    for uid, username in ((42, "pepe_fan"), (43, "x"), (44, None)):
        await db.touch_user(uid, username, f"U{uid}")
    yield cfg, db
    await db.close()


async def test_sync_groups_by_model_with_market_prices(env):
    _, db = env
    relayer = FakeRelayer([item(1, "100", "Plush Pepe", 1, "Frog"), item(2, "100", "Plush Pepe", 2, "Frog"),
                           item(3, "100", "Plush Pepe", 3, "Toad"), item(4, "200", "Durov's Cap", 4, "Black")],
                          {(100, "Frog"): 9000, (200, "Black"): 30000})
    count, error = await nft.sync(db, relayer)
    assert count == 3 and error is None
    rows = {(r["collection_name"], r["model"]): r for r in await db.all("SELECT * FROM nft_models")}
    assert rows[("Plush Pepe", "Frog")]["stock"] == 2 and rows[("Plush Pepe", "Frog")]["price"] == 9000
    assert rows[("Plush Pepe", "Toad")]["price"] is None         # нет лотов на маркете — цены нет
    assert rows[("Durov's Cap", "Black")]["price"] == 30000
    relayer.items = relayer.items[:1]                            # подарки ушли с аккаунта -> запас обновляется
    await nft.sync(db, relayer)
    stock = {r["model"]: r["stock"] for r in await db.all("SELECT model, stock FROM nft_models")}
    assert stock == {"Frog": 1, "Toad": 0, "Black": 0}


async def test_sync_without_relayer(env):
    _, db = env
    relayer = FakeRelayer([])
    relayer.ready = False
    count, error = await nft.sync(db, relayer)
    assert count == 0 and "/relayer" in error


async def test_model_without_fresh_price_not_in_case(env):
    from app.cases import CaseCatalog

    class Gifts:
        async def list(self):
            return [{"id": "g" + e, "emoji": e, "stars": p} for e, p in (
                ("🧸", 15), ("💝", 15), ("🌹", 25), ("🎁", 25), ("🚀", 50), ("🍾", 50), ("🎂", 50),
                ("💎", 100), ("🏆", 100), ("💍", 100))]

    _, db = env
    now = time.time()
    await db.conn.execute(
        "INSERT INTO nft_models(collection_id, collection_name, model, stock, price, price_at) "
        "VALUES ('1','A','fresh',1,5000,?), ('2','B','old',1,5000,?), ('3','C','noprice',1,NULL,NULL), "
        "('4','D','nostock',0,5000,?)", (now, now - 5 * 3600, now))
    case = await CaseCatalog(Gifts(), db, 250).get("nft")
    assert [p["model"] for p in case["prizes"] if p["kind"] == "nft"] == ["fresh"]


async def test_deliver_random_gift_of_model(env):
    cfg, db = env
    bot = Bot()
    relayer = FakeRelayer([item(1, "100", "Plush Pepe", 1, "Frog"), item(2, "100", "Plush Pepe", 2, "Frog"),
                           item(3, "100", "Plush Pepe", 3, "Toad")])
    await db.conn.execute("INSERT INTO nft_models(collection_id, collection_name, model, stock, reserved, price) "
                          "VALUES ('100','Plush Pepe','Frog',2,2,9000)")
    await db.conn.execute("INSERT INTO nft_wins(model_id, user_id, price, created_at) "
                          "VALUES (1, 42, 9000, 0), (1, 43, 9000, 0)")
    assert (await nft.deliver(bot, db, cfg, relayer, 1))[0]
    assert (await nft.deliver(bot, db, cfg, relayer, 2))[0]
    assert sorted(r for r, _ in relayer.transfers) == [1, 2]      # разные подарки одной модели, «Toad» не тронут
    assert [u for _, u in relayer.transfers] == [42, 43]
    assert (await db.one("SELECT stock, reserved FROM nft_models")) == {"stock": 0, "reserved": 0}
    assert not (await nft.deliver(bot, db, cfg, relayer, 1))[0]   # повторная выдача невозможна
    # подарков модели не осталось -> админу уведомление с командой повтора
    await db.conn.execute("INSERT INTO nft_wins(model_id, user_id, price, created_at) VALUES (1, 42, 9000, 0)")
    ok, reason = await nft.deliver(bot, db, cfg, relayer, 3)
    assert not ok and "не осталось" in reason
    assert bot.messages[-1][0] == 7 and "/nftsend 3" in bot.messages[-1][1]


async def test_deliver_waits_for_player_without_username(env):
    cfg, db = env
    bot = Bot()
    relayer = FakeRelayer([item(1, "100", "Plush Pepe", 1, "Frog")])
    await db.conn.execute("INSERT INTO nft_models(collection_id, collection_name, model, stock, reserved, price) "
                          "VALUES ('100','Plush Pepe','Frog',1,1,9000)")
    await db.conn.execute("INSERT INTO nft_wins(model_id, user_id, price, created_at) VALUES (1, 44, 9000, 0)")
    ok, reason = await nft.deliver(bot, db, cfg, relayer, 1)
    assert not ok and "напишет" in reason
    assert (await db.one("SELECT status FROM nft_wins"))["status"] == "waiting"
    assert bot.messages[-1][0] == 44 and "@svag_relayer" in bot.messages[-1][1]
    relayer.known.add(44)                                         # игрок написал релейеру
    await nft.deliver_waiting(bot, db, cfg, relayer, 44)
    assert (await db.one("SELECT status FROM nft_wins"))["status"] == "sent"
    assert relayer.transfers == [(1, 44)]


async def test_relayer_secrets_encrypted(env):
    cfg, db = env
    relayer = Relayer(db, "1:x")
    await relayer.set_api(12345, "abcdef")
    raw = await db.kv_get("relayer:api_hash")
    assert raw and "abcdef" not in raw                            # в базе только шифротекст
    assert await relayer._get("api_hash") == "abcdef"
    assert await Relayer(db, "2:other")._get("api_hash") is None  # без токена бота не расшифровать
    assert await relayer.start() is False                         # сессии ещё нет


async def test_relayer_floor_price_parses_market(env):
    from telethon.tl import types as T
    from telethon.tl.functions.payments import GetResaleStarGiftsRequest

    def doc(i):
        return T.Document(id=i, access_hash=0, file_reference=b"", date=None, mime_type="", size=0, dc_id=1, attributes=[])

    def lot(num, amounts):
        return T.StarGiftUnique(id=num, gift_id=100, title="Plush Pepe", slug=f"pp-{num}", num=num, attributes=[],
                                availability_issued=1, availability_total=1, resell_amount=amounts)

    rarity = T.StarGiftAttributeRarity(permille=15)
    requests = []

    class Client:
        async def __call__(self, req):
            assert isinstance(req, GetResaleStarGiftsRequest)
            requests.append(req)
            if not req.attributes:
                return T.payments.ResaleStarGifts(count=0, gifts=[], chats=[], users=[], attributes=[
                    T.StarGiftAttributeModel(name="Frog", document=doc(11), rarity=rarity),
                    T.StarGiftAttributeModel(name="Toad", document=doc(22), rarity=rarity)])
            return T.payments.ResaleStarGifts(count=2, gifts=[
                lot(1, [T.StarsAmount(amount=9100, nanos=0), T.StarsTonAmount(amount=10)]),
                lot(2, [T.StarsAmount(amount=8800, nanos=0)])], chats=[], users=[])

    _, db = env
    relayer = Relayer(db, "1:x")
    relayer.client = Client()
    assert await relayer.floor_price(100, "Frog") == 8800           # минимум в звёздах, TON игнорируется
    req = requests[-1]
    assert req.sort_by_price and req.stars_only and req.attributes[0].document_id == 11
    assert await relayer.floor_price(100, "Frog") == 8800           # из кэша
    assert len(requests) == 2
    assert await relayer.floor_price(100, "Unknown") is None


async def test_relayer_inventory_and_paid_transfer(env):
    from datetime import datetime, timedelta, timezone

    from telethon.tl import types as T
    from telethon.tl.functions.payments import (GetPaymentFormRequest, GetSavedStarGiftsRequest, SendStarsFormRequest,
                                                TransferStarGiftRequest)

    doc = T.Document(id=1, access_hash=0, file_reference=b"", date=None, mime_type="", size=0, dc_id=1,
                     attributes=[T.DocumentAttributeCustomEmoji(alt="🐸", stickerset=T.InputStickerSetEmpty())])
    model = T.StarGiftAttributeModel(name="Frog", document=doc, rarity=T.StarGiftAttributeRarity(permille=15))

    def saved(num, msg_id, transfer=0, locked=False):
        gift = T.StarGiftUnique(id=num, gift_id=100, title="Plush Pepe", slug=f"pp-{num}", num=num,
                                attributes=[model], availability_issued=1, availability_total=1)
        until = datetime.now(timezone.utc) + timedelta(days=1) if locked else None
        return T.SavedStarGift(date=None, gift=gift, msg_id=msg_id, transfer_stars=transfer, can_transfer_at=until)

    calls = []

    class Client:
        async def __call__(self, req):
            calls.append(type(req).__name__)
            if isinstance(req, GetSavedStarGiftsRequest):
                return T.payments.SavedStarGifts(count=3, gifts=[saved(1, 11), saved(2, 12, transfer=25),
                                                                 saved(3, 13, locked=True)], chats=[], users=[])
            if isinstance(req, GetPaymentFormRequest):
                return type("F", (), {"form_id": 99})()
            if isinstance(req, SendStarsFormRequest):
                assert req.form_id == 99
            if isinstance(req, TransferStarGiftRequest):
                assert req.stargift.msg_id == 11
            return True

        async def get_input_entity(self, target):
            if target == "pepe_fan":
                return T.InputPeerUser(user_id=42, access_hash=5)
            raise ValueError("unknown")

    _, db = env
    relayer = Relayer(db, "1:x")
    relayer.client = Client()
    inv = await relayer.inventory()
    assert [(i["number"], i["model"], i["emoji"], i["rarity"], i["transfer_stars"]) for i in inv] == [
        (1, "Frog", "🐸", 1.5, 0), (2, "Frog", "🐸", 1.5, 25)]      # подарок с блокировкой передачи пропущен
    await relayer.transfer(inv[0], 42, "pepe_fan")                  # бесплатная передача
    await relayer.transfer(inv[1], 42, "pepe_fan")                  # платная: форма оплаты + оплата звёздами
    assert calls[-3:] == ["TransferStarGiftRequest", "GetPaymentFormRequest", "SendStarsFormRequest"]
    with pytest.raises(RelayerError, match="NEED_CONTACT"):
        await relayer.transfer(inv[0], 44, None)
