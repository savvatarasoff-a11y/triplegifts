import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.casino import Casino
from app.config import Config
from app.db import Database
from aiohttp import web

from app.web import build_app, parse_deposit_payload

CASINO = web.AppKey("casino", object)
RELAYER = web.AppKey("relayer", object)
BOT = web.AppKey("bot", object)

TOKEN = "123456:TEST-token"


def init_data(user_id: int, token: str = TOKEN, auth_date: int | None = None, first_name: str = "Петя",
              start_param: str | None = None) -> str:
    fields = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": first_name, "username": f"u{user_id}"}),
    }
    if start_param:
        fields["start_param"] = start_param
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class FakeGift:
    def __init__(self, gid, stars, emoji, premium=False, remaining=None):
        self.id, self.star_count, self.is_premium, self.remaining_count = gid, stars, premium, remaining
        self.sticker = type("S", (), {"emoji": emoji})()


def unique_gift(owned_id, collection_id, base, number, model, transfer=25):
    from aiogram.types import OwnedGiftUnique
    sticker = {"file_id": "f", "file_unique_id": "u", "type": "custom_emoji", "width": 1, "height": 1,
               "is_animated": False, "is_video": False, "emoji": "🐸"}
    return OwnedGiftUnique.model_validate({
        "type": "unique", "owned_gift_id": owned_id, "send_date": 0, "can_be_transferred": True,
        "transfer_star_count": transfer,
        "gift": {"gift_id": collection_id, "base_name": base, "name": f"{base}-{number}", "number": number,
                 "model": {"name": model, "rarity_per_mille": 15, "sticker": sticker},
                 "symbol": {"name": "s", "rarity_per_mille": 10, "sticker": sticker},
                 "backdrop": {"name": "b", "rarity_per_mille": 10, "colors": {
                     "center_color": 0, "edge_color": 0, "symbol_color": 0, "text_color": 0}}},
    })


class FakeBot:
    def __init__(self):
        self.invoices = []
        self.messages = []
        self.transfers = []
        self.prices = {}
        self.inventory = []

    async def get_business_account_gifts(self, **kwargs):
        return type("O", (), {"gifts": self.inventory, "next_offset": None})()

    async def create_invoice_link(self, **kwargs):
        self.invoices.append(kwargs)
        return "https://t.me/$invoice"

    async def get_available_gifts(self):
        std = [("💝", 15), ("🧸", 15), ("🎁", 25), ("🌹", 25), ("🎂", 50), ("💐", 50), ("🚀", 50), ("🍾", 50),
               ("🏆", 100), ("💍", 100), ("💎", 100)]
        gifts = [FakeGift(f"id{e}", self.prices.get(e, p), e) for e, p in std]
        return type("G", (), {"gifts": gifts + [
            FakeGift("gp", 100, "🦄", premium=True), FakeGift("gsold", 25, "🐉", remaining=0),
        ]})()

    async def transfer_gift(self, **kwargs):
        self.transfers.append(kwargs)
        return True

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))

    async def get_me(self):
        return type("Me", (), {"username": "triple_gifts_bot"})()

    async def get_user_profile_photos(self, user_id, limit=1):
        size = type("P", (), {"file_id": "f1", "width": 160})()
        return type("Ph", (), {"total_count": 1, "photos": [[size]]})()

    async def download(self, file_id):
        import io
        return io.BytesIO(b"\xff\xd8jpeg")


@pytest.fixture
async def client(tmp_path):
    cfg = Config(
        bot_token=TOKEN, admin_ids=frozenset({777}), webapp_url="https://x", db_path=str(tmp_path / "w.db"),
        port=0, min_bet=1, max_bet=1000, start_bonus=25, log_level="INFO",
    )
    db = Database(cfg.db_path)
    await db.connect()
    casino = Casino(db, cfg)
    bot = FakeBot()
    from tests.test_nft import FakeRelayer
    relayer = FakeRelayer([])
    app = build_app(cfg, casino, bot, relayer)
    app[CASINO] = casino
    app[BOT] = bot
    app[RELAYER] = relayer
    async with TestClient(TestServer(app)) as c:
        yield c
    await db.close()


def auth(uid=1, **kw):
    return {"Authorization": f"tma {init_data(uid, **kw)}"}


async def test_rejects_missing_and_forged_auth(client):
    assert (await client.get("/api/me")).status == 401
    r = await client.get("/api/me", headers={"Authorization": f"tma {init_data(1, token='999:other')}"})
    assert r.status == 401
    r = await client.get("/api/me", headers=auth(auth_date=int(time.time()) - 3 * 86400))
    assert r.status == 401
    # подмена id пользователя ломает подпись
    forged = init_data(1).replace("%22id%22%3A+1", "%22id%22%3A+2")
    assert (await client.get("/api/me", headers={"Authorization": f"tma {forged}"})).status == 401


async def test_me_and_start_bonus(client):
    r = await client.get("/api/me", headers=auth())
    assert r.status == 200
    data = await r.json()
    assert data["balance"] == 25 and data["user"]["name"] == "Петя"
    cases = {c["id"]: c for c in data["config"]["cases"]}
    # NFT-кейсов нет, пока нет NFT с ценой; «Люкс» без NFT-джекпота скрыт: все его подарки дешевле кейса
    assert set(cases) == {"bear", "rocket", "heart", "party"}
    bear = cases["bear"]["prizes"]
    assert {p["emoji"]: p["amount"] for p in bear}["🧸"] == 15    # реальная цена из каталога
    assert sum(p["chance"] for p in bear) == pytest.approx(100, abs=0.01)
    # повторный вход бонус не начисляет
    data = await (await client.get("/api/me", headers=auth())).json()
    assert data["balance"] == 25


async def test_play_and_errors(client):
    r = await client.post("/api/slots", headers=auth(), json={"bet": 5})
    assert r.status == 200 and "reels" in await r.json()
    r = await client.post("/api/slots", headers=auth(), json={"bet": 100000})
    assert r.status == 400 and "Максимальная" in (await r.json())["error"]
    r = await client.post("/api/slots", headers=auth(), data="not json")
    assert r.status == 400
    r = await client.post("/api/plinko", headers=auth(), json={"bet": 1, "rows": 8, "risk": "high"})
    assert r.status == 200 and len((await r.json())["path"]) == 8
    r = await client.post("/api/pickaxe", headers=auth(), json={"bet": 1})
    assert r.status == 200 and (await r.json())["tier"] in ("none", "wood", "iron", "gold", "diamond")
    r = await client.post("/api/mines/start", headers=auth(), json={"bet": 1, "mines": 5})
    assert r.status == 200
    r = await client.get("/api/mines", headers=auth())
    assert (await r.json())["game"]["mines"] == 5
    await client.app[CASINO].crash_tick()
    r = await client.post("/api/crash/bet", headers=auth(), json={"bet": 1, "auto": 2})
    assert r.status == 200
    st = await (await client.get("/api/crash", headers=auth())).json()
    assert st["round"]["phase"] == "betting" and st["players"][0]["bet"] == 1
    r = await client.get("/api/pvp", headers=auth())
    assert r.status == 200
    r = await client.post("/api/plinko", headers=auth(), json={"bet": 1, "rows": 9})
    assert r.status == 400
    assert (await client.get("/api/feed", headers=auth())).status == 200


async def test_deposit_link_and_payload(client):
    r = await client.post("/api/deposit", headers=auth(5), json={"amount": 100})
    assert (await r.json())["link"].startswith("https://t.me/")
    inv = client.app[BOT].invoices[-1]
    assert inv["currency"] == "XTR" and parse_deposit_payload(inv["payload"]) == (5, 100)
    r = await client.post("/api/deposit", headers=auth(5), json={"amount": 0})
    assert r.status == 400
    assert parse_deposit_payload("dep:x:1") is None


async def test_check_via_api(client):
    code = await client.app[CASINO].create_check(1, 40, 1)
    r = await client.post("/api/check", headers=auth(9), json={"code": f"https://t.me/bot?start=c_{code}"})
    data = await r.json()
    assert data["amount"] == 40 and data["balance"] == 65


async def test_index_served(client):
    r = await client.get("/")
    assert r.status == 200 and "telegram-web-app.js" in await r.text()


async def test_withdraw_api(client):
    r = await client.get("/api/withdraw", headers=auth(3))
    data = await r.json()
    ids = [g["id"] for g in data["gifts"]]
    assert "gp" not in ids and "gsold" not in ids and ids[0] in ("id💝", "id🧸")   # премиум и распроданные скрыты
    assert data["wager"]["left"] == 25                           # стартовый бонус нужно отыграть
    r = await client.post("/api/withdraw", headers=auth(3), json={"gift_id": "id🧸"})
    assert r.status == 400 and "пополнение от 100" in (await r.json())["error"]
    assert await client.app[CASINO].db.credit_payment("p3", 3, 100)
    await client.app[CASINO].db.conn.execute("UPDATE users SET wagered=0 WHERE id=3")
    r = await client.post("/api/withdraw", headers=auth(3), json={"gift_id": "id🧸"})
    assert r.status == 400 and "отыграйте" in (await r.json())["error"]
    await client.app[CASINO].db.conn.execute("UPDATE users SET wagered=25, balance=80 WHERE id=3")
    r = await client.post("/api/withdraw", headers=auth(3), json={"gift_id": "id🎂"})
    data = await r.json()
    assert data["amount"] == 50 and data["balance"] == 30
    assert client.app[BOT].messages[-1][0] == 777 and "Заявка на вывод" in client.app[BOT].messages[-1][1]
    r = await client.post("/api/withdraw", headers=auth(3), json={"gift_id": "gp"})
    assert r.status == 400
    hist = (await (await client.get("/api/withdraw", headers=auth(3))).json())["history"]
    assert hist[0]["status"] == "pending"


async def test_avatar(client):
    assert (await client.get("/avatar/424242")).status == 404       # не игрок казино
    await client.get("/api/me", headers=auth(5))
    assert (await client.get("/avatar/5")).status == 200
    assert (await client.get("/avatar/abc")).status == 404


async def test_cases_api_real_prices_and_nft(client, monkeypatch):
    from app.games import logic as g
    casino = client.app[CASINO]
    await client.get("/api/me", headers=auth(8))
    await casino.db.conn.execute("UPDATE users SET balance=1000 WHERE id=8")
    r = await client.post("/api/case", headers=auth(8), json={"case": "bear"})
    data = await r.json()
    assert data["kind"] == "gift" and data["prize"] in (15, 25, 50, 100)
    assert (await client.post("/api/case", headers=auth(8), json={"case": "nft"})).status == 400

    # Модель у релейера с ценой с маркета -> NFT-кейс; выигравшему релейер передаёт случайный подарок модели
    import time as _t
    from tests.test_nft import FakeRelayer, item
    relayer = FakeRelayer([item(1, "555", "Plush Pepe", 11, "Frog Prince"), item(2, "555", "Plush Pepe", 12, "Frog Prince"),
                           item(3, "555", "Plush Pepe", 13, "Other")], known_users=[8])
    client.app[RELAYER].items = relayer.items
    client.app[RELAYER].known = relayer.known
    await casino.db.conn.execute(
        "INSERT INTO nft_models(collection_id, collection_name, model, rarity, emoji, stock, price, price_at) "
        "VALUES ('555','Plush Pepe','Frog Prince',1.5,'🐸',2,5000,?)", (_t.time(),))
    cases = {c["id"]: c for c in (await (await client.get("/api/me", headers=auth(8))).json())["config"]["cases"]}
    nft_prize = next(p for p in cases["nft"]["prizes"] if p["kind"] == "nft")
    assert nft_prize["title"] == "Plush Pepe" and nft_prize["amount"] == 5000 and nft_prize["model"] == "Frog Prince"
    monkeypatch.setattr(g, "pick_weighted", lambda items, weights, rng=None: items[0])
    r = await client.post("/api/case", headers=auth(8), json={"case": "nft"})
    data = await r.json()
    assert data["kind"] == "nft" and data["nft"]["model"] == "Frog Prince" and data["nft"]["in_profile"]
    gifts = await (await client.get("/api/gifts", headers=auth(8))).json()
    assert [x["model"] for x in gifts["gifts"]] == ["Frog Prince"]     # выигрыш — в «Мои подарки»
    assert client.app[RELAYER].transfers == []
    assert (await casino.db.one("SELECT status FROM nft_wins"))["status"] == "sent"
    assert (await casino.db.one("SELECT stock, reserved FROM nft_models")) == {"stock": 1, "reserved": 0}


async def test_case_disabled_when_real_prices_too_generous(client):
    client.app[BOT].prices = {"🧸": 60}      # подарок «подорожал» до первого запроса каталога
    cases = [c["id"] for c in (await (await client.get("/api/me", headers=auth(9))).json())["config"]["cases"]]
    assert cases == []                       # кейсы с подорожавшим 🧸 убыточны — выключены, «Люкс» без джекпота скрыт
    r = await client.post("/api/case", headers=auth(9), json={"case": "bear"})
    assert r.status == 400


async def test_slots_777_nft_via_api(client, monkeypatch):
    import asyncio
    import time as _t
    from app.games import logic as g
    from tests.test_nft import item
    casino = client.app[CASINO]
    await client.get("/api/me", headers=auth(8))
    await casino.db.conn.execute("UPDATE users SET balance=1000 WHERE id=8")
    await casino.db.conn.execute(
        "INSERT INTO nft_models(collection_id, collection_name, model, emoji, stock, price, price_at) "
        "VALUES ('555','Plush Pepe','Frog','🐸',1,4000,?)", (_t.time(),))
    relayer = client.app[RELAYER]
    relayer.items = [item(9, "555", "Plush Pepe", 77, "Frog")]
    relayer.known = {8}
    monkeypatch.setattr(g, "slots_spin", lambda rng=None: (64, ["seven", "seven", "seven"], 40))
    data = await (await client.post("/api/slots", headers=auth(8), json={"bet": 100})).json()
    assert data["nft"]["title"] == "Plush Pepe" and data["balance"] == 900 and data["nft"]["in_profile"]
    assert [(x["title"], x["model"]) for x in await casino.gifts(8)] == [("Plush Pepe #77", "Frog")]
    assert relayer.transfers == []


async def test_referral_via_start_param_and_pages(client):
    casino, bot = client.app[CASINO], client.app[BOT]
    await client.get("/api/me", headers={"Authorization": "tma " + init_data(500)})
    r = await client.get("/api/me", headers={"Authorization": "tma " + init_data(501, start_param="r_500")})
    assert r.status == 200
    assert (await casino.db.get_user(501))["referrer_id"] == 500
    assert bot.messages[-1][0] == 500
    await casino.db.credit_payment("x", 501, 100)
    r = await client.get("/api/referrals", headers={"Authorization": "tma " + init_data(500)})
    data = await r.json()
    assert data["link"] == "https://t.me/triple_gifts_bot?start=r_500"
    assert (data["count"], data["earned"]) == (1, 10)
    r = await client.get("/api/profile", headers={"Authorization": "tma " + init_data(501)})
    assert (await r.json())["deposited"] == 100


async def test_gift_cases_get_nft_jackpot(client):
    import time as _t
    casino = client.app[CASINO]
    await casino.db.conn.execute(
        "INSERT INTO nft_models(collection_id, collection_name, model, emoji, stock, price, price_at) VALUES "
        "('1','Lol Pop','Pink','🍭',5,400,?), ('2','Plush Pepe','Frog','🐸',1,9000,?)", (_t.time(), _t.time()))
    player = (await (await client.get("/api/me", headers=auth(3))).json())["config"]["cases"]
    assert all("rtp" not in c for c in player)                           # игроки RTP не видят
    cases = {c["id"]: c for c in (await (await client.get("/api/me", headers=auth(777))).json())["config"]["cases"]}
    lux = cases["lux"]
    assert max(p["amount"] for p in lux["prizes"]) > lux["price"]          # есть ради чего открывать
    jackpot = [p for p in lux["prizes"] if p["kind"] == "nft"]
    assert {p["model"] for p in jackpot} == {"Pink", "Frog"} and 0 < sum(p["chance"] for p in jackpot) < 5
    for c in cases.values():
        assert sum(p["chance"] for p in c["prizes"]) == pytest.approx(100, abs=0.05)
        assert c["rtp"] <= 0.92


async def test_play_requires_channel_subscription(client):
    bot = client.app[BOT]
    status = {"s": "left"}

    async def get_chat_member(chat_id, user_id):
        return type("M", (), {"status": status["s"]})()

    bot.get_chat_member = get_chat_member
    me = await (await client.get("/api/me", headers=auth(31))).json()
    assert me["subscribed"] is False and me["free_case"]["available"]
    r = await client.post("/api/free_case", headers=auth(31))
    assert r.status == 403 and (await r.json())["need_sub"] == "TripleGifts"
    assert (await client.post("/api/slots", headers=auth(31), json={"bet": 1})).status == 403
    status["s"] = "member"
    assert (await (await client.get("/api/sub", headers=auth(31))).json())["subscribed"] is True
    r = await client.post("/api/free_case", headers=auth(31))
    assert r.status == 200 and (await r.json())["prize"] >= 1


async def test_cors_for_pages_origin(client):
    pages = {"Origin": "https://savvat133-dev.github.io"}
    r = await client.options("/api/me", headers={**pages, "Access-Control-Request-Method": "GET"})
    assert r.status == 204 and r.headers["Access-Control-Allow-Origin"] == pages["Origin"]
    assert "Authorization" in r.headers["Access-Control-Allow-Headers"]
    r = await client.get("/api/me", headers={**auth(), **pages})
    assert r.status == 200 and r.headers["Access-Control-Allow-Origin"] == pages["Origin"]
    r = await client.get("/api/me", headers={**auth(), "Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in r.headers
    assert (await client.options("/api/me", headers={"Origin": "https://evil.example"})).status == 403


async def test_league_logo_served_from_own_domain(tmp_path):
    """Лига без эмодзи: логотип турнира ESPN скачивает сервер и отдаёт со своего адреса."""
    from io import BytesIO

    from PIL import Image

    from app import sports as sp
    from app.nftimg import NftImages
    buf = BytesIO()
    Image.new("RGBA", (64, 64), (20, 120, 220, 255)).save(buf, "PNG")
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return buf.getvalue()

    class Espn:
        enabled = True
        logos = {}

        async def league_logo(self, code):
            return f"https://a.espncdn.com/{code}-dark.png"

    cfg = Config(bot_token=TOKEN, admin_ids=frozenset(), webapp_url="https://x", db_path=str(tmp_path / "l.db"),
                 port=0, min_bet=1, max_bet=1000, start_bonus=0, log_level="INFO")
    db = Database(cfg.db_path)
    await db.connect()
    casino = Casino(db, cfg)
    bot = FakeBot()                 # набора эмодзи нет — get_sticker_set отсутствует
    app = build_app(cfg, casino, bot, images=NftImages(fetch=fetch), sports=sp.Sportsbook(casino, Espn()))
    async with TestClient(TestServer(app)) as c:
        r = await (await c.get("/api/sports", headers=auth())).json()
        imgs = {lg["id"]: lg["img"] for lg in r["leagues"]}
        assert imgs["unl"].startswith("static/mc/unl.png?v=") and (await c.get("/" + imgs["unl"])).status == 200
        await db.kv_set(sp.ICON_KEY, '{"unl": "e1"}')                   # даже с назначенным эмодзи — свой флаг
        r = await (await c.get("/api/sports", headers=auth())).json()
        assert next(lg["img"] for lg in r["leagues"] if lg["id"] == "unl").startswith("static/mc/unl.png")
        old = await c.get("/sporticon?league=unl&v=logo2")
        assert old.status == 200 and (await old.read())[:4] == b"\x89PNG"
        assert imgs["ucl"].startswith("sporticon?league=ucl")
        resp = await c.get("/" + imgs["ucl"])
        assert resp.status == 200 and (await resp.read())[:4] in (b"\x89PNG", b"RIFF")
        assert fetched == ["https://a.espncdn.com/uefa.champions-dark.png"]
        assert await db.kv_get("sports:logo:ucl") == fetched[0]
        assert (await c.get("/sporticon?league=nope")).status == 404
    await db.close()
