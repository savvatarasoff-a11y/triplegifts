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
BOT = web.AppKey("bot", object)

TOKEN = "123456:TEST-token"


def init_data(user_id: int, token: str = TOKEN, auth_date: int | None = None, first_name: str = "Петя") -> str:
    fields = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": first_name, "username": f"u{user_id}"}),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class FakeBot:
    def __init__(self):
        self.invoices = []

    async def create_invoice_link(self, **kwargs):
        self.invoices.append(kwargs)
        return "https://t.me/$invoice"


@pytest.fixture
async def client(tmp_path):
    cfg = Config(
        bot_token=TOKEN, admin_ids=frozenset(), webapp_url="https://x", db_path=str(tmp_path / "w.db"),
        port=0, min_bet=1, max_bet=1000, start_bonus=25, log_level="INFO",
    )
    db = Database(cfg.db_path)
    await db.connect()
    casino = Casino(db, cfg)
    bot = FakeBot()
    app = build_app(cfg, casino, bot)
    app[CASINO] = casino
    app[BOT] = bot
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
    assert data["config"]["cases"][0]["id"] == "bear"
    assert data["config"]["cases"][0]["prizes"][-1] == {"amount": 2500, "gift": "💍", "chance": 0.15}
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
    r = await client.post("/api/dice", headers=auth(), json={"bet": 1, "chance": 50})
    assert r.status == 200
    r = await client.post("/api/mines/start", headers=auth(), json={"bet": 1, "mines": 3})
    assert r.status == 200
    r = await client.get("/api/mines", headers=auth())
    assert (await r.json())["game"]["mines"] == 3
    r = await client.post("/api/crash/start", headers=auth(), json={"bet": 1, "auto": 2})
    assert r.status == 200
    r = await client.get("/api/pvp", headers=auth())
    assert r.status == 200
    r = await client.post("/api/dice", headers=auth(), json={"bet": 1, "chance": 12.5, "over": True})
    assert (await r.json())["multiplier"] == 7.92
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
