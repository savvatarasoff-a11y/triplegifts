"""Модели NFT у релейера: синхронизация, цены с маркета, выдача случайного подарка модели."""
import time

import pytest

from app import nft
from app.config import Config
from app.db import Database
from app.relayer import Relayer
from tests.test_web import unique_gift


class FakeRelayer:
    ready = True

    def __init__(self, prices):
        self.prices = prices

    async def floor_price(self, collection_id, model):
        return self.prices.get((collection_id, model))


class Bot:
    def __init__(self, inventory):
        self.inventory = inventory
        self.transfers = []
        self.messages = []

    async def get_business_account_gifts(self, **kw):
        return type("O", (), {"gifts": self.inventory, "next_offset": None})()

    async def transfer_gift(self, **kw):
        self.transfers.append(kw)
        return True

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))


@pytest.fixture
async def env(tmp_path):
    cfg = Config(bot_token="1:x", admin_ids=frozenset({7}), webapp_url="", db_path=str(tmp_path / "n.db"),
                 port=0, min_bet=1, max_bet=1000, start_bonus=0, log_level="INFO")
    db = Database(cfg.db_path)
    await db.connect()
    await db.conn.execute(
        "INSERT INTO business_connections(id, user_id, can_gifts, is_enabled, updated_at) VALUES ('bc', 7, 1, 1, 0)")
    yield cfg, db
    await db.close()


async def test_sync_groups_by_model_with_market_prices(env):
    cfg, db = env
    bot = Bot([unique_gift("a", "100", "Plush Pepe", 1, "Frog"), unique_gift("b", "100", "Plush Pepe", 2, "Frog"),
               unique_gift("c", "100", "Plush Pepe", 3, "Toad"), unique_gift("d", "200", "Durov's Cap", 4, "Black")])
    relayer = FakeRelayer({(100, "Frog"): 9000, (200, "Black"): 30000})
    count, error = await nft.sync(bot, db, cfg, relayer)
    assert count == 3 and error is None
    rows = {(r["collection_name"], r["model"]): r for r in await db.all("SELECT * FROM nft_models")}
    assert rows[("Plush Pepe", "Frog")]["stock"] == 2 and rows[("Plush Pepe", "Frog")]["price"] == 9000
    assert rows[("Plush Pepe", "Toad")]["price"] is None         # нет лотов на маркете — цены нет
    assert rows[("Durov's Cap", "Black")]["price"] == 30000
    # подарки ушли с аккаунта -> запас обновляется
    bot.inventory = bot.inventory[:1]
    await nft.sync(bot, db, cfg, relayer)
    stock = {r["model"]: r["stock"] for r in await db.all("SELECT model, stock FROM nft_models")}
    assert stock == {"Frog": 1, "Toad": 0, "Black": 0}


async def test_sync_without_mtproto_reports_error(env):
    cfg, db = env
    relayer = FakeRelayer({})
    relayer.ready = False
    count, error = await nft.sync(Bot([unique_gift("a", "100", "Plush Pepe", 1, "Frog")]), db, cfg, relayer)
    assert count == 1 and "MTProto" in error


async def test_model_without_fresh_price_not_in_case(env):
    from app.cases import CaseCatalog

    class Gifts:
        async def list(self):
            return [{"id": "g" + e, "emoji": e, "stars": p} for e, p in (
                ("🧸", 15), ("💝", 15), ("🌹", 25), ("🎁", 25), ("🚀", 50), ("🍾", 50), ("🎂", 50),
                ("💎", 100), ("🏆", 100), ("💍", 100))]

    cfg, db = env
    now = time.time()
    await db.conn.execute(
        "INSERT INTO nft_models(collection_id, collection_name, model, stock, price, price_at) "
        "VALUES ('1','A','fresh',1,5000,?), ('2','B','old',1,5000,?), ('3','C','noprice',1,NULL,NULL), "
        "('4','D','nostock',0,5000,?)", (now, now - 5 * 3600, now))
    case = await CaseCatalog(Gifts(), db, 250).get("nft")
    assert [p["model"] for p in case["prizes"] if p["kind"] == "nft"] == ["fresh"]


async def test_deliver_random_gift_of_model(env):
    cfg, db = env
    bot = Bot([unique_gift("a", "100", "Plush Pepe", 1, "Frog"), unique_gift("b", "100", "Plush Pepe", 2, "Frog"),
               unique_gift("c", "100", "Plush Pepe", 3, "Toad")])
    await db.conn.execute("INSERT INTO nft_models(collection_id, collection_name, model, stock, reserved, price) "
                          "VALUES ('100','Plush Pepe','Frog',2,2,9000)")
    await db.conn.execute("INSERT INTO nft_wins(model_id, user_id, price, created_at) "
                          "VALUES (1, 42, 9000, 0), (1, 43, 9000, 0)")
    ok1, _ = await nft.deliver(bot, db, cfg, 1)
    ok2, _ = await nft.deliver(bot, db, cfg, 2)
    assert ok1 and ok2
    assert {t["owned_gift_id"] for t in bot.transfers} == {"a", "b"}   # разные подарки одной модели, «Toad» не тронут
    assert [t["new_owner_chat_id"] for t in bot.transfers] == [42, 43]
    assert all(t["star_count"] == 25 for t in bot.transfers)
    assert (await db.one("SELECT stock, reserved FROM nft_models")) == {"stock": 0, "reserved": 0}
    assert not (await nft.deliver(bot, db, cfg, 1))[0]                  # повторная выдача невозможна
    # подарков модели не осталось -> админу уведомление с командой повтора
    await db.conn.execute("INSERT INTO nft_wins(model_id, user_id, price, created_at) VALUES (1, 44, 9000, 0)")
    bot.inventory = [g for g in bot.inventory if g.owned_gift_id == "c"]
    ok, reason = await nft.deliver(bot, db, cfg, 3)
    assert not ok and "не осталось" in reason
    assert bot.messages[-1][0] == 7 and "/nftsend 3" in bot.messages[-1][1]


async def test_relayer_secrets_encrypted(env):
    cfg, db = env
    relayer = Relayer(db, "1:x")
    await relayer.set_api(12345, "abcdef")
    raw = await db.kv_get("relayer:api_hash")
    assert raw and "abcdef" not in raw                              # в базе только шифротекст
    assert await relayer._get("api_hash") == "abcdef"
    assert await Relayer(db, "2:other")._get("api_hash") is None    # без токена бота не расшифровать
    assert await relayer.start() is False                           # сессии ещё нет


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
            if not req.attributes:   # первый запрос — список моделей коллекции
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
    assert await relayer.floor_price(100, "Frog") == 8800           # из кэша, без нового запроса
    assert len(requests) == 2
    assert await relayer.floor_price(100, "Unknown") is None        # такой модели в коллекции нет
