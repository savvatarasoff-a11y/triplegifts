"""HTTP-сервер: API мини-приложения и его статические файлы."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import Bot
from aiogram.types import LabeledPrice
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

from .casino import Casino, GameError, display_name
from .config import Config
from .games import logic as g
from .cases import CaseCatalog
from .nft import deliver as deliver_nft
from .relayer import Relayer
from .withdraw import GiftCatalog, notify_admins

log = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
INIT_DATA_TTL = 24 * 3600
AVATAR_TTL = 6 * 3600
AVATAR_CACHE_MAX = 1000
DEPOSIT_PRESETS = [50, 100, 250, 500, 1000, 2500]
DEPOSIT_MIN, DEPOSIT_MAX = 1, 10000

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
USER_ID = web.RequestKey("user_id", int)


def deposit_invoice_kwargs(user_id: int, amount: int) -> dict[str, Any]:
    return {
        "title": f"Пополнение на {amount} ⭐",
        "description": "Пополнение баланса Svag Gifts. Вывод — подарками Telegram.",
        "payload": f"dep:{user_id}:{amount}",
        "currency": "XTR",
        "prices": [LabeledPrice(label=f"{amount} ⭐", amount=amount)],
    }


def parse_deposit_payload(payload: str) -> tuple[int, int] | None:
    parts = payload.split(":")
    if len(parts) != 3 or parts[0] != "dep" or not parts[1].isdigit() or not parts[2].isdigit():
        return None
    return int(parts[1]), int(parts[2])


def build_app(cfg: Config, casino: Casino, bot: Bot, relayer: Relayer | None = None) -> web.Application:
    @web.middleware
    async def errors(request: web.Request, handler: Handler) -> web.StreamResponse:
        try:
            return await handler(request)
        except GameError as e:
            return web.json_response({"error": str(e)}, status=400)
        except web.HTTPException:
            raise
        except Exception:
            log.exception("Ошибка API %s", request.path)
            return web.json_response({"error": "Внутренняя ошибка, попробуйте ещё раз"}, status=500)

    @web.middleware
    async def auth(request: web.Request, handler: Handler) -> web.StreamResponse:
        if not request.path.startswith("/api/"):
            return await handler(request)
        header = request.headers.get("Authorization", "")
        if not header.startswith("tma "):
            return web.json_response({"error": "Откройте казино через Telegram"}, status=401)
        try:
            data = safe_parse_webapp_init_data(cfg.bot_token, header[4:])
        except ValueError:
            return web.json_response({"error": "Сессия недействительна, перезапустите приложение"}, status=401)
        if data.user is None or time.time() - data.auth_date.timestamp() > INIT_DATA_TTL:
            return web.json_response({"error": "Сессия устарела, перезапустите приложение"}, status=401)
        request[USER_ID] = data.user.id
        await casino.register(data.user.id, data.user.username, data.user.first_name)
        return await handler(request)

    async def body(request: web.Request) -> dict[str, Any]:
        if not request.can_read_body:
            return {}
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise GameError("Некорректный запрос") from None
        if not isinstance(data, dict):
            raise GameError("Некорректный запрос")
        return data

    routes = web.RouteTableDef()
    catalog = GiftCatalog(bot)
    cases = CaseCatalog(catalog, casino.db, cfg.nft_case_price)
    avatars: dict[int, tuple[float, bytes | None]] = {}

    async def load_avatar(user_id: int) -> bytes | None:
        cached = avatars.get(user_id)
        if cached and time.time() - cached[0] < AVATAR_TTL:
            return cached[1]
        data: bytes | None = None
        try:
            photos = await bot.get_user_profile_photos(user_id=user_id, limit=1)
            if photos.total_count and photos.photos:
                sizes = photos.photos[0]
                # маленькая копия около 160 px — хватает для аватарки
                size = next((p for p in sizes if p.width >= 150), sizes[-1])
                buf = await bot.download(size.file_id)
                data = buf.read() if buf else None
        except Exception:
            log.debug("Аватарка %s недоступна", user_id)
        if len(avatars) >= AVATAR_CACHE_MAX:
            avatars.pop(next(iter(avatars)))
        avatars[user_id] = (time.time(), data)
        return data

    @routes.get("/health")
    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    @routes.get(r"/avatar/{user_id:\d+}")
    async def avatar(request: web.Request) -> web.Response:
        user_id = int(request.match_info["user_id"])
        # Отдаём только аватарки игроков казино
        if not await casino.db.get_user(user_id):
            raise web.HTTPNotFound()
        data = await load_avatar(user_id)
        if not data:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})

    @routes.get("/")
    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @routes.get("/api/me")
    async def me(request: web.Request) -> web.Response:
        uid = request[USER_ID]
        user = await casino.db.get_user(uid)
        return web.json_response({
            "user": {"id": uid, "name": display_name(user)},
            "balance": user["balance"],
            "history": await casino.db.recent_bets(uid, 15),
            "config": {
                "min_bet": cfg.min_bet,
                "max_bet": cfg.max_bet,
                "deposit_presets": DEPOSIT_PRESETS,
                "slots": {"symbols": g.SLOT_SYMBOLS, "777": g.SLOT_777, "triple": g.SLOT_TRIPLE,
                          "two_sevens": g.SLOT_TWO_SEVENS, "pair": g.SLOT_PAIR},
                "dice": {"min": g.DICE_MIN_CHANCE, "max": g.DICE_MAX_CHANCE, "edge": g.HOUSE_EDGE},
                "cases": [
                    {**{k: c[k] for k in ("id", "name", "emoji", "price")},
                     "prizes": [{k: p.get(k) for k in ("kind", "emoji", "amount", "chance", "title", "model", "rarity")}
                                for p in c["prizes"]]}
                    for c in await cases.list()
                ],
                "mines_min": g.MINES_MIN,
                "crash_growth": g.CRASH_GROWTH,
                "pvp_commission": g.PVP_COMMISSION,
            },
        })

    @routes.get("/api/feed")
    async def feed(_: web.Request) -> web.Response:
        return web.json_response({"wins": await casino.big_wins()})

    @routes.post("/api/deposit")
    async def deposit(request: web.Request) -> web.Response:
        amount = (await body(request)).get("amount")
        if not isinstance(amount, int) or isinstance(amount, bool) or not DEPOSIT_MIN <= amount <= DEPOSIT_MAX:
            raise GameError(f"Сумма пополнения — от {DEPOSIT_MIN} до {DEPOSIT_MAX} ⭐")
        link = await bot.create_invoice_link(**deposit_invoice_kwargs(request[USER_ID], amount))
        return web.json_response({"link": link})

    @routes.get("/api/withdraw")
    async def withdraw_info(request: web.Request) -> web.Response:
        uid = request[USER_ID]
        try:
            gifts = await catalog.list()
        except Exception:
            log.warning("Не удалось получить список подарков")
            gifts = []
        return web.json_response({
            "gifts": gifts,
            "wager": await casino.wager_status(uid),
            "history": [
                {"id": w["id"], "amount": w["amount"], "emoji": w["gift_emoji"], "status": w["status"],
                 "created_at": w["created_at"]}
                for w in await casino.withdrawals(user_id=uid, limit=10)
            ],
        })

    @routes.post("/api/withdraw")
    async def withdraw(request: web.Request) -> web.Response:
        gift_id = (await body(request)).get("gift_id")
        gift = await catalog.get(gift_id) if isinstance(gift_id, str) else None
        if not gift:
            raise GameError("Этот подарок сейчас недоступен")
        wd = await casino.withdraw_request(request[USER_ID], gift["id"], gift["stars"], gift["emoji"])
        await notify_admins(bot, cfg, casino, wd)
        return web.json_response(wd)

    @routes.post("/api/check")
    async def check(request: web.Request) -> web.Response:
        code = (await body(request)).get("code")
        if not isinstance(code, str):
            raise GameError("Введите код чека")
        code = code.strip().split("start=")[-1].removeprefix("c_")
        amount, balance = await casino.activate_check(request[USER_ID], code)
        return web.json_response({"amount": amount, "balance": balance})

    @routes.post("/api/slots")
    async def slots(request: web.Request) -> web.Response:
        data = await body(request)
        return web.json_response(await casino.slots(request[USER_ID], data.get("bet")))

    @routes.post("/api/dice")
    async def dice(request: web.Request) -> web.Response:
        data = await body(request)
        return web.json_response(await casino.dice(request[USER_ID], data.get("bet"), data.get("chance"), data.get("over", False)))

    @routes.post("/api/case")
    async def open_case(request: web.Request) -> web.Response:
        case_id = (await body(request)).get("case")
        case = await cases.get(case_id) if isinstance(case_id, str) else None
        if not case:
            raise GameError("Кейс сейчас недоступен")
        result = await casino.open_case(request[USER_ID], case)
        if result["kind"] == "nft":
            asyncio.create_task(deliver_nft(bot, casino.db, cfg, relayer, result["nft"]["win_id"]))
        return web.json_response(result)

    @routes.get("/api/mines")
    async def mines_state(request: web.Request) -> web.Response:
        return web.json_response({"game": await casino.mines_state(request[USER_ID])})

    @routes.post("/api/mines/start")
    async def mines_start(request: web.Request) -> web.Response:
        data = await body(request)
        return web.json_response(await casino.mines_start(request[USER_ID], data.get("bet"), data.get("mines")))

    @routes.post("/api/mines/open")
    async def mines_open(request: web.Request) -> web.Response:
        data = await body(request)
        return web.json_response(await casino.mines_open(request[USER_ID], data.get("cell")))

    @routes.post("/api/mines/cashout")
    async def mines_cashout(request: web.Request) -> web.Response:
        return web.json_response(await casino.mines_cashout(request[USER_ID]))

    @routes.get("/api/crash")
    async def crash_state(request: web.Request) -> web.Response:
        return web.json_response(await casino.crash_state(request[USER_ID]))

    @routes.post("/api/crash/bet")
    async def crash_bet(request: web.Request) -> web.Response:
        data = await body(request)
        return web.json_response(await casino.crash_bet(request[USER_ID], data.get("bet"), data.get("auto")))

    @routes.post("/api/crash/cashout")
    async def crash_cashout(request: web.Request) -> web.Response:
        return web.json_response(await casino.crash_cashout(request[USER_ID]))

    @routes.get("/api/pvp")
    async def pvp_state(request: web.Request) -> web.Response:
        game = request.query.get("game", "roulette")
        return web.json_response(await casino.pvp_state(request[USER_ID], game))

    @routes.post("/api/pvp/bet")
    async def pvp_bet(request: web.Request) -> web.Response:
        data = await body(request)
        return web.json_response(await casino.pvp_bet(request[USER_ID], data.get("amount"), data.get("game", "roulette")))

    app = web.Application(middlewares=[errors, auth], client_max_size=64 * 1024)
    app.add_routes(routes)
    app.router.add_static("/static/", WEBAPP_DIR, show_index=False)
    return app
