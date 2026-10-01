"""HTTP-сервер: API мини-приложения и его статические файлы."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import Bot
from aiogram.types import LabeledPrice
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

from .casino import GIFT_SELL_RATE, USER_CHECK_MAX_ACTIVATIONS, Casino, GameError, display_name
from . import money, ton
from .config import Config
from .db import REFERRAL_RATE
from .games import logic as g
from .cases import CaseCatalog
from .channel import CHANNEL
from .gifts import withdraw as withdraw_gift
from .nft import deliver as deliver_nft
from .nftimg import NftImages, render_tgs
from .relayer import Relayer
from .withdraw import GiftCatalog, notify_admins, notify_admins_ton

log = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
INIT_DATA_TTL = 24 * 3600
AVATAR_TTL = 6 * 3600
AVATAR_CACHE_MAX = 1000
DEPOSIT_PRESETS = [50, 100, 250, 500, 1000, 2500]
DEPOSIT_MIN, DEPOSIT_MAX = 1, 10000

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
USER_ID = web.RequestKey("user_id", int)


def parse_referral(arg: str | None) -> int | None:
    """Параметр реферальной ссылки r_<id> → id пригласившего."""
    if arg and arg.startswith("r_") and arg[2:].isdigit() and len(arg) <= 24:
        return int(arg[2:])
    return None


async def notify_referrer(bot: Bot, referrer_id: int, name: str | None) -> None:
    try:
        await bot.send_message(
            referrer_id,
            f"🤝 По вашей ссылке пришёл новый игрок{': ' + html.escape(name) if name else ''}. "
            f"Вы будете получать {int(REFERRAL_RATE * 100)}% от каждой его покупки звёзд.",
        )
    except Exception:
        pass


_bot_username: dict[int, str] = {}


async def bot_username(bot: Bot) -> str | None:
    if not _bot_username.get(id(bot)):
        try:
            _bot_username[id(bot)] = (await bot.get_me()).username
        except Exception:
            return None
    return _bot_username[id(bot)]


def referral_link(username: str | None, user_id: int) -> str | None:
    return f"https://t.me/{username}?start=r_{user_id}" if username else None


def deposit_invoice_kwargs(user_id: int, amount: int) -> dict[str, Any]:
    return {
        "title": f"Пополнение на {amount} ⭐",
        "description": "Пополнение баланса Triple Gifts. Вывод — подарками Telegram.",
        "payload": f"dep:{user_id}:{amount}",
        "currency": "XTR",
        "prices": [LabeledPrice(label=f"{amount} ⭐", amount=amount)],
    }


def parse_deposit_payload(payload: str) -> tuple[int, int] | None:
    parts = payload.split(":")
    if len(parts) != 3 or parts[0] != "dep" or not parts[1].isdigit() or not parts[2].isdigit():
        return None
    return int(parts[1]), int(parts[2])


# Мини-приложение с постоянного адреса (GitHub Pages) ходит к серверу с другого домена
PAGES_ORIGINS = frozenset(o.strip().rstrip("/") for o in
                          (os.getenv("PAGES_ORIGIN", "") or "https://savvat133-dev.github.io").split(",") if o.strip())
CORS_HEADERS = {"Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type", "Access-Control-Max-Age": "86400"}


@web.middleware
async def cors(request: web.Request, handler: Handler) -> web.StreamResponse:
    origin = request.headers.get("Origin", "").rstrip("/")
    allowed = origin in PAGES_ORIGINS
    if request.method == "OPTIONS":
        if not allowed:
            raise web.HTTPForbidden()
        return web.Response(status=204, headers={"Access-Control-Allow-Origin": origin, "Vary": "Origin", **CORS_HEADERS})
    try:
        response = await handler(request)
    except web.HTTPException as e:
        if allowed:
            e.headers["Access-Control-Allow-Origin"] = origin
            e.headers["Vary"] = "Origin"
        raise
    if allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


# Игровые действия — только для подписчиков канала (вывод, пополнение и продажа подарков доступны всем)
SUB_REQUIRED = frozenset({
    "/api/slots", "/api/plinko", "/api/pickaxe", "/api/case", "/api/mines/start", "/api/crash/bet", "/api/pvp/bet",
    "/api/upgrade", "/api/bonus", "/api/free_case",
})
SUB_OK_STATUSES = {"member", "administrator", "creator", "restricted"}


def build_app(cfg: Config, casino: Casino, bot: Bot, relayer: Relayer | None = None,
              images: NftImages | None = None) -> web.Application:
    sub_cache: dict[int, tuple[float, bool]] = {}

    async def subscribed(user_id: int, fresh: bool = False) -> bool:
        """Подписан ли игрок на канал. Если бот не админ канала и проверить нельзя — не блокируем."""
        if user_id in cfg.admin_ids:
            return True
        hit = sub_cache.get(user_id)
        if hit and not fresh and time.time() - hit[0] < (600 if hit[1] else 20):
            return hit[1]
        try:
            member = await bot.get_chat_member(CHANNEL, user_id)
            ok = getattr(member, "status", "") in SUB_OK_STATUSES
        except Exception as e:
            log.warning("Не удалось проверить подписку на %s (бот — админ канала?): %s", CHANNEL, type(e).__name__)
            return True
        sub_cache[user_id] = (time.time(), ok)
        return ok

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
        ref = parse_referral(data.start_param)
        if ref and await casino.db.set_referrer(data.user.id, ref):
            await notify_referrer(bot, ref, data.user.first_name)
        # играть можно только подписчикам канала
        if request.method == "POST" and request.path in SUB_REQUIRED and not await subscribed(data.user.id):
            return web.json_response({"error": f"Чтобы играть, подпишитесь на наш канал {CHANNEL}",
                                      "need_sub": CHANNEL.lstrip("@")}, status=403)
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

    async def to_profile(nft: dict) -> None:
        """Выигранный NFT сразу кладём в «Мои подарки»; не вышло — админ получит уведомление и /nftsend."""
        try:
            nft["in_profile"], _ = await deliver_nft(bot, casino.db, cfg, relayer, nft["win_id"])
        except Exception:
            log.exception("NFT-выигрыш %s не положен в профиль", nft["win_id"])
            nft["in_profile"] = False

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

    images = images or NftImages(Path(cfg.db_path).parent / "nftimg", relayer=relayer, db=casino.db)

    @routes.get("/nftimg")
    async def nft_image(request: web.Request) -> web.Response:
        """Маленькое превью NFT (модель или конкретный номер) — только для коллекций, известных казино."""
        q = request.query
        collection, model, number = q.get("c", ""), q.get("m") or None, q.get("n") or None
        if not collection or len(collection) > 64 or (model and len(model) > 64):
            raise web.HTTPNotFound()
        if number is not None:
            if not number.isdigit() or len(number) > 9:
                raise web.HTTPNotFound()
            number = int(number)
        known = await casino.db.one(
            "SELECT 1 FROM nft_models WHERE collection_name=? UNION ALL "
            "SELECT 1 FROM user_gifts WHERE collection_name=? LIMIT 1", collection, collection)
        if not known:
            raise web.HTTPNotFound()
        img = await images.nft(collection, model, number)
        if not img:
            raise web.HTTPNotFound(headers={"Cache-Control": "no-store"})
        return web.Response(body=img[0], content_type=img[1],
                            headers={"Cache-Control": "public, max-age=2592000, immutable"})

    @routes.get("/giftimg")
    async def gift_image(request: web.Request) -> web.Response:
        """Картинка обычного подарка Telegram (стикер из каталога бота, отрисованный в WebP)."""
        gift = await catalog.get(request.query.get("id", ""))
        if not gift or not gift.get("file_id"):
            raise web.HTTPNotFound()

        async def from_bot() -> bytes | None:
            buf = await bot.download(gift["file_id"])
            data = buf.read() if buf else None
            if data and data[:2] == b"\x1f\x8b":                 # TGS — анимированный стикер
                return await asyncio.to_thread(render_tgs, data)
            return data

        img = await images.get(f"g|{gift['id']}", [("telegram", from_bot)])
        if not img:
            raise web.HTTPNotFound(headers={"Cache-Control": "no-store"})
        return web.Response(body=img[0], content_type=img[1],
                            headers={"Cache-Control": "public, max-age=2592000, immutable"})

    @routes.get("/")
    async def index(_: web.Request) -> web.Response:
        # к app.js/app.css добавляем версию файла — Telegram не отдаст старый закэшированный скрипт после обновления
        page = (WEBAPP_DIR / "index.html").read_text(encoding="utf-8")
        for name in ("app.js", "app.css"):
            ver = int((WEBAPP_DIR / name).stat().st_mtime)
            page = page.replace(f"static/{name}\"", f"static/{name}?v={ver}\"")
        return web.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-cache"})

    @routes.get("/api/me")
    async def me(request: web.Request) -> web.Response:
        uid = request[USER_ID]
        is_admin = uid in cfg.admin_ids          # RTP кейсов видит только админ
        user = await casino.db.get_user(uid)
        return web.json_response({
            "user": {"id": uid, "name": display_name(user)},
            "subscribed": await subscribed(uid),
            "free_case": await casino.free_case_info(uid),
            "balance": user["balance"],
            "ton": user["ton"],
            "history": await casino.db.recent_bets(uid, 15),
            "config": {
                "min_bet": cfg.min_bet,
                "max_bet": cfg.max_bet,
                "ton": {"min_bet": money.TON_MIN_BET, "max_bet": money.TON_MAX_BET, "nano": money.NANO,
                        "rate": await casino.rate(), "min_withdraw": money.TON_MIN_WITHDRAW,
                        "deposits": bool(await ton.wallet(casino.db))},
                "deposit_presets": DEPOSIT_PRESETS,
                "slots": {"symbols": g.SLOT_SYMBOLS, "777": g.SLOT_777, "triple": g.SLOT_TRIPLE,
                          "two_sevens": g.SLOT_TWO_SEVENS, "pair": g.SLOT_PAIR, "jackpot_nft": True},
                "plinko": {"rows": list(g.PLINKO_ROWS), "risks": list(g.PLINKO_RISKS),
                           "tables": {f"{r}:{k}": t for (r, k), t in g.PLINKO_TABLES.items()}},
                "pickaxe": {"table": g.PICKAXE_TABLE, "wheel": [list(w) for w in g.PICK_WHEEL], "tiers": g.PICK_TIERS,
                            "g": g.PICK_G, "heal": g.PICKAXE_HEAL, "size": g.PICK_SIZE, "top": g.PICK_TOP},
                "cases": [
                    {**{k: c[k] for k in ("id", "name", "emoji", "price")},
                     **({"rtp": c["rtp"]} if is_admin else {}),
                     "prizes": [{k: p.get(k) for k in ("kind", "emoji", "amount", "chance", "title", "model", "rarity",
                                                       "demo", "gift_id")}
                                for p in c["prizes"]]}
                    for c in await cases.list()
                ],
                "mines_min": g.MINES_MIN,
                "channel": CHANNEL.lstrip("@"),
                "mines_edge": g.MINES_EDGE,
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

    async def check_view(c: dict) -> dict:
        username = await bot_username(bot)
        return {"code": c["code"], "amount": c["amount"], "left": c["left"], "total": c["total"],
                "link": f"https://t.me/{username}?start=c_{c['code']}" if username else None}

    @routes.get("/api/checks")
    async def my_checks(request: web.Request) -> web.Response:
        return web.json_response({"checks": [await check_view(c) for c in await casino.my_checks(request[USER_ID])],
                                  "max_activations": USER_CHECK_MAX_ACTIVATIONS})

    @routes.post("/api/checks/create")
    async def create_check(request: web.Request) -> web.Response:
        data = await body(request)
        uid = request[USER_ID]
        code = await casino.create_check(uid, data.get("amount"), data.get("activations", 1),
                                         paid=uid not in cfg.admin_ids)
        check = next(c for c in await casino.my_checks(uid) if c["code"] == code)
        user = await casino.db.get_user(uid)
        return web.json_response({"check": await check_view(check), "balance": user["balance"]})

    @routes.post("/api/checks/revoke")
    async def revoke_check(request: web.Request) -> web.Response:
        code = (await body(request)).get("code")
        refund = await casino.revoke_check(code, request[USER_ID]) if isinstance(code, str) else None
        if refund is None:
            raise GameError("Чек не найден или уже отозван")
        return web.json_response({"refund": refund, "balance": (await casino.db.get_user(request[USER_ID]))["balance"]})

    @routes.get("/api/sub")
    async def sub_status(request: web.Request) -> web.Response:
        return web.json_response({"subscribed": await subscribed(request[USER_ID], fresh=True),
                                  "channel": CHANNEL.lstrip("@")})

    @routes.post("/api/free_case")
    async def free_case(request: web.Request) -> web.Response:
        result = await casino.open_free_case(request[USER_ID])
        if result.get("nft") and result["nft"].get("win_id"):
            await to_profile(result["nft"])
        return web.json_response(result)

    @routes.post("/api/slots")
    async def slots(request: web.Request) -> web.Response:
        data = await body(request)
        result = await casino.slots(request[USER_ID], data.get("bet"), cur=data.get("cur"))
        if result.get("nft") and result["nft"].get("win_id"):
            await to_profile(result["nft"])
        return web.json_response(result)

    @routes.post("/api/plinko")
    async def plinko(request: web.Request) -> web.Response:
        data = await body(request)
        return web.json_response(await casino.plinko(request[USER_ID], data.get("bet"), data.get("rows", 12),
                                                     data.get("risk", "medium"), data.get("cur")))

    @routes.post("/api/pickaxe")
    async def pickaxe(request: web.Request) -> web.Response:
        data = await body(request)
        return web.json_response(await casino.pickaxe(request[USER_ID], data.get("bet"), data.get("cur")))

    @routes.get("/api/case/drops")
    async def case_drops(_: web.Request) -> web.Response:
        return web.json_response({"drops": await casino.case_drops()})

    @routes.post("/api/case")
    async def open_case(request: web.Request) -> web.Response:
        data = await body(request)
        case_id = data.get("case")
        case = await cases.get(case_id) if isinstance(case_id, str) else None
        if not case:
            raise GameError("Кейс сейчас недоступен")
        result = await casino.open_case(request[USER_ID], case, data.get("count", 1), data.get("cur"))
        for item in result["items"]:
            if item["kind"] == "nft" and item["nft"].get("win_id"):
                await to_profile(item["nft"])
        return web.json_response(result)

    @routes.get("/api/mines")
    async def mines_state(request: web.Request) -> web.Response:
        return web.json_response({"game": await casino.mines_state(request[USER_ID])})

    @routes.post("/api/mines/start")
    async def mines_start(request: web.Request) -> web.Response:
        data = await body(request)
        return web.json_response(await casino.mines_start(request[USER_ID], data.get("bet"), data.get("mines"),
                                                          data.get("cur")))

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
        return web.json_response(await casino.crash_bet(request[USER_ID], data.get("bet"), data.get("auto"),
                                                        data.get("cur"), data.get("gifts")))

    @routes.post("/api/crash/cashout")
    async def crash_cashout(request: web.Request) -> web.Response:
        return web.json_response(await casino.crash_cashout(request[USER_ID]))

    @routes.get("/api/pvp")
    async def pvp_state(request: web.Request) -> web.Response:
        game = request.query.get("game", "roulette")
        return web.json_response(await casino.pvp_state(request[USER_ID], game, request.query.get("cur", "stars")))

    @routes.post("/api/pvp/bet")
    async def pvp_bet(request: web.Request) -> web.Response:
        data = await body(request)
        return web.json_response(await casino.pvp_bet(request[USER_ID], data.get("amount"), data.get("game", "roulette"),
                                                      data.get("gifts"), data.get("cur")))

    @routes.get("/api/ton")
    async def ton_info(request: web.Request) -> web.Response:
        uid = request[USER_ID]
        address = await ton.wallet(casino.db)
        comment = ton.deposit_comment(uid)
        return web.json_response({
            "wallet": address, "comment": comment,
            "link": ton.transfer_link(address, comment) if address else None,
            "min_withdraw": money.TON_MIN_WITHDRAW, "wager": await casino.wager_status(uid, money.TON),
            "history": [{"id": w["id"], "amount": w["amount"], "address": w["address"], "status": w["status"],
                         "created_at": w["created_at"]} for w in await casino.ton_withdrawals(user_id=uid, limit=10)],
        })

    @routes.post("/api/ton/withdraw")
    async def ton_withdraw(request: web.Request) -> web.Response:
        data = await body(request)
        wd = await casino.ton_withdraw_request(request[USER_ID], data.get("amount"), data.get("address"))
        await notify_admins_ton(bot, cfg, casino, wd)
        return web.json_response(wd)

    @routes.get("/api/vip")
    async def vip(request: web.Request) -> web.Response:
        return web.json_response(await casino.vip(request[USER_ID]))

    @routes.post("/api/vip/rakeback")
    async def rakeback(request: web.Request) -> web.Response:
        return web.json_response(await casino.claim_rakeback(request[USER_ID]))

    @routes.post("/api/bonus")
    async def daily_bonus(request: web.Request) -> web.Response:
        return web.json_response(await casino.daily_bonus(request[USER_ID]))

    @routes.get("/api/leaders")
    async def leaders(request: web.Request) -> web.Response:
        return web.json_response(await casino.leaders(request[USER_ID]))

    @routes.get("/api/profile")
    async def profile(request: web.Request) -> web.Response:
        return web.json_response(await casino.profile(request[USER_ID]))

    @routes.get("/api/referrals")
    async def referrals(request: web.Request) -> web.Response:
        uid = request[USER_ID]
        info = await casino.referrals(uid)
        info["link"] = referral_link(await bot_username(bot), uid)
        return web.json_response(info)

    @routes.get("/api/upgrade")
    async def upgrade_info(request: web.Request) -> web.Response:
        return web.json_response({
            "targets": await casino.upgrade_targets(request[USER_ID]),
            "gifts": [x for x in await casino.gifts(request[USER_ID]) if x["status"] == "owned"],
            "edge": g.UPGRADE_EDGE, "max_chance": g.UPGRADE_MAX_CHANCE, "min_chance": g.UPGRADE_MIN_CHANCE,
        })

    @routes.post("/api/upgrade")
    async def upgrade(request: web.Request) -> web.Response:
        data = await body(request)
        result = await casino.upgrade(request[USER_ID], data.get("gifts"), data.get("target"))
        if result["nft"] and result["nft"]["win_id"]:
            await to_profile(result["nft"])
        return web.json_response(result)

    @routes.get("/api/gifts")
    async def my_gifts(request: web.Request) -> web.Response:
        name = None
        if relayer is not None and relayer.ready:
            try:
                name = await relayer.username()
            except Exception:
                name = None
        return web.json_response({"gifts": await casino.gifts(request[USER_ID]), "relayer": name,
                                  "sell_rate": GIFT_SELL_RATE})

    @routes.post("/api/gifts/sell")
    async def gift_sell(request: web.Request) -> web.Response:
        return web.json_response(await casino.gift_sell(request[USER_ID], (await body(request)).get("id")))

    @routes.post("/api/gifts/withdraw")
    async def gift_withdraw(request: web.Request) -> web.Response:
        if relayer is None:
            raise GameError("Вывод подарков временно недоступен")
        return web.json_response(await withdraw_gift(casino, relayer, request[USER_ID], (await body(request)).get("id")))

    app = web.Application(middlewares=[cors, errors, auth], client_max_size=64 * 1024)
    app.add_routes(routes)
    app.router.add_static("/static/", WEBAPP_DIR, show_index=False)

    async def close_images(_: web.Application) -> None:
        await images.close()

    app.on_cleanup.append(close_images)
    return app
