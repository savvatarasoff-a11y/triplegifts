"""Релейер — аккаунт казино с NFT-подарками.

Через MTProto (Telethon) под этим аккаунтом бот передаёт NFT и дарит подарки,
а цену модели берёт с маркетплейса MRKT (mrkt.py): вход в MRKT — тоже от имени релейера.
Сессия аккаунта хранится в базе в зашифрованном виде (ключ — из токена бота).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .db import Database

log = logging.getLogger(__name__)



class RelayerError(Exception):
    pass


def _ts(value: Any) -> float:
    """Время из MTProto: где-то это datetime (date), где-то unix-число (can_transfer_at)."""
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return value.timestamp()


def _fernet(bot_token: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("relayer:" + bot_token).encode()).digest()))


class Relayer:
    def __init__(self, db: Database, bot_token: str):
        self.db = db
        self._f = _fernet(bot_token)
        self.client: Any = None
        self._login: dict[str, Any] = {}
        from .mrkt import Mrkt
        self.market = Mrkt(self)

    # ---------- хранение ----------

    async def _get(self, key: str) -> str | None:
        raw = await self.db.kv_get(f"relayer:{key}")
        if raw is None:
            return None
        try:
            return self._f.decrypt(raw.encode()).decode()
        except InvalidToken:
            log.warning("Не удалось расшифровать настройки релейера (сменился токен бота?)")
            return None

    async def _set(self, key: str, value: str | None) -> None:
        await self.db.kv_set(f"relayer:{key}", None if value is None else self._f.encrypt(value.encode()).decode())

    # ---------- подключение ----------

    def _make_client(self, api_id: int, api_hash: str, session: str | None) -> Any:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        return TelegramClient(StringSession(session or ""), api_id, api_hash, device_model="Svag Gifts relayer")

    async def start(self) -> bool:
        """Подключается сохранённой сессией. False — если релейер ещё не настроен."""
        api_id, api_hash, session = await self._get("api_id"), await self._get("api_hash"), await self._get("session")
        if not (api_id and api_hash and session):
            return False
        try:
            self.client = self._make_client(int(api_id), api_hash, session)
            await self.client.connect()
            if not await self.client.is_user_authorized():
                log.warning("Сессия релейера недействительна — нужен повторный вход (/relayer)")
                await self.client.disconnect()
                self.client = None
                return False
        except Exception:
            log.exception("Не удалось подключить релейер")
            self.client = None
            return False
        log.info("Релейер подключён")
        return True

    async def stop(self) -> None:
        await self.market.close()
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

    @property
    def ready(self) -> bool:
        return self.client is not None

    async def me(self) -> str | None:
        if not self.ready:
            return None
        user = await self.client.get_me()
        return f"{user.first_name or ''} @{user.username}" if user.username else (user.first_name or str(user.id))

    # ---------- вход (админ пишет боту) ----------

    async def set_api(self, api_id: int, api_hash: str) -> None:
        await self._set("api_id", str(api_id))
        await self._set("api_hash", api_hash)

    async def send_code(self, phone: str) -> None:
        api_id, api_hash = await self._get("api_id"), await self._get("api_hash")
        if not (api_id and api_hash):
            raise RelayerError("Сначала задайте api_id и api_hash: /relayer_api")
        await self.stop()
        client = self._make_client(int(api_id), api_hash, None)
        await client.connect()
        sent = await client.send_code_request(phone)
        self._login = {"client": client, "phone": phone, "hash": sent.phone_code_hash}

    async def sign_in(self, code: str) -> bool:
        """True — вход выполнен; False — нужен пароль двухэтапной проверки."""
        from telethon.errors import SessionPasswordNeededError
        if not self._login:
            raise RelayerError("Сначала отправьте номер: /relayer_phone")
        client = self._login["client"]
        try:
            await client.sign_in(phone=self._login["phone"], code=code, phone_code_hash=self._login["hash"])
        except SessionPasswordNeededError:
            return False
        await self._finish_login()
        return True

    async def sign_in_password(self, password: str) -> None:
        if not self._login:
            raise RelayerError("Сначала отправьте номер: /relayer_phone")
        await self._login["client"].sign_in(password=password)
        await self._finish_login()

    async def _finish_login(self) -> None:
        client = self._login.pop("client")
        self._login = {}
        await self._set("session", client.session.save())
        self.client = client

    async def logout(self) -> None:
        await self._set("session", None)
        await self.stop()

    # ---------- цены (MRKT) ----------

    async def floor_price(self, collection_id: Any, model: str, collection_name: str | None = None) -> int | None:
        """Флор модели в звёздах — самый дешёвый лот этой коллекции и модели на MRKT."""
        if not collection_name:
            return None
        return await self.market.floor_stars(collection_name, model)

    # ---------- картинки моделей (из самого Telegram) ----------

    async def _gifts_by_title(self) -> dict[str, Any]:
        """Коллекции подарков Telegram: нормализованное название → StarGift (id и стикер; кэш на 6 часов)."""
        cached = getattr(self, "_gifts_cache", None)
        if cached and time.time() - cached[0] < 6 * 3600:
            return cached[1]
        from telethon.tl.functions.payments import GetStarGiftsRequest
        from .nftimg import norm
        res = await self.client(GetStarGiftsRequest(hash=0))
        gifts = {norm(g.title): g for g in getattr(res, "gifts", []) or [] if getattr(g, "title", None)}
        self._gifts_cache = (time.time(), gifts)
        return gifts

    async def model_documents(self, collection: str) -> dict[str, Any]:
        """Все модели коллекции с их стикерами: нормализованное имя модели → Document (кэш на час)."""
        from .nftimg import norm
        cache = self.__dict__.setdefault("_models_cache", {})
        key = norm(collection)
        hit = cache.get(key)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        if not self.ready:
            return {}
        from telethon.tl.functions.payments import GetResaleStarGiftsRequest, GetStarGiftUpgradePreviewRequest
        from telethon.tl.types import StarGiftAttributeModel
        gift = (await self._gifts_by_title()).get(key)
        docs: dict[str, Any] = {}
        if gift is not None:
            # все модели коллекции — из фильтров маркета Telegram; если маркета ещё нет — из превью улучшения
            try:
                res = await self.client(GetResaleStarGiftsRequest(gift_id=gift.id, offset="", limit=1,
                                                                  attributes_hash=0))
                attrs = getattr(res, "attributes", None) or []
            except Exception as e:
                log.info("Маркет Telegram для %r недоступен: %s", collection, type(e).__name__)
                attrs = []
            if not attrs:
                try:
                    attrs = (await self.client(GetStarGiftUpgradePreviewRequest(gift_id=gift.id))).sample_attributes
                except Exception as e:
                    log.info("Превью улучшения %r недоступно: %s", collection, type(e).__name__)
            docs = {norm(a.name): a.document for a in attrs or [] if isinstance(a, StarGiftAttributeModel)}
            if getattr(gift, "sticker", None) is not None:
                docs[""] = gift.sticker        # сам подарок — если модели ещё нет (улучшения не вышли)
        else:
            log.info("Коллекция %r не найдена среди подарков Telegram", collection)
        cache[key] = (time.time(), docs)
        return docs

    async def has_model(self, collection: str, model: str) -> bool | None:
        """Есть ли у коллекции такая модель в Telegram (улучшения вышли). None — проверить нельзя."""
        if not self.ready:
            return None
        from .nftimg import norm
        docs = await self.model_documents(collection)
        if not docs:
            return None if norm(collection) not in (await self._gifts_by_title()) else False
        return norm(model) in docs

    async def model_image(self, collection: str, model: str | None) -> bytes | None:
        """Картинка модели: стикер модели из Telegram (или самого подарка, если улучшений ещё нет) в PNG."""
        if not self.ready or not collection:
            return None
        from .nftimg import norm, render_sticker
        docs = await self.model_documents(collection)
        doc = docs.get(norm(model or "")) or docs.get("")
        return await render_sticker(self.client, doc) if doc is not None else None

    # ---------- инвентарь и передача (MTProto) ----------

    async def _saved(self) -> list[dict[str, Any]]:
        """Все подарки на аккаунте релейера: NFT и обычные, с отправителем и датой."""
        if not self.ready:
            raise RelayerError("Релейер не подключён")
        from telethon.tl.functions.payments import GetSavedStarGiftsRequest
        from telethon.tl.types import (DocumentAttributeCustomEmoji, DocumentAttributeSticker, InputPeerSelf, PeerUser,
                                       StarGift, StarGiftAttributeModel, StarGiftAttributeRarity, StarGiftUnique)
        items: list[dict[str, Any]] = []
        offset = ""
        while True:
            res = await self.client(GetSavedStarGiftsRequest(peer=InputPeerSelf(), offset=offset, limit=100))
            for saved in res.gifts:
                gift = saved.gift
                if not saved.msg_id:
                    continue
                base = {
                    "ref": saved.msg_id,
                    "from_user": saved.from_id.user_id if isinstance(saved.from_id, PeerUser) else None,
                    "date": _ts(saved.date),
                    "transfer_at": _ts(saved.can_transfer_at),
                    "transfer_stars": saved.transfer_stars or 0,
                }
                if isinstance(gift, StarGiftUnique):
                    model = next((a for a in gift.attributes if isinstance(a, StarGiftAttributeModel)), None)
                    if model is None:
                        continue
                    rarity = model.rarity.permille / 10 if isinstance(model.rarity, StarGiftAttributeRarity) else None
                    emoji = next((a.alt for a in getattr(model.document, "attributes", []) or []
                                  if isinstance(a, DocumentAttributeCustomEmoji)), None)
                    items.append({**base, "kind": "nft", "collection_id": str(gift.gift_id),
                                  "collection_name": gift.title, "number": gift.num, "model": model.name,
                                  "rarity": rarity, "emoji": emoji})
                elif isinstance(gift, StarGift):
                    emoji = next((a.alt for a in getattr(gift.sticker, "attributes", []) or []
                                  if isinstance(a, DocumentAttributeSticker)), None)
                    items.append({**base, "kind": "gift", "collection_id": str(gift.id),
                                  "collection_name": gift.title or "Подарок", "number": None, "model": None,
                                  "rarity": None, "emoji": emoji,
                                  "convert_stars": saved.convert_stars or gift.convert_stars or 0})
            offset = res.next_offset
            if not offset:
                return items

    async def received(self) -> list[dict[str, Any]]:
        """Все подарки релейера (для зачисления подарков, которые прислали игроки)."""
        return await self._saved()

    async def inventory(self) -> list[dict[str, Any]]:
        """NFT-подарки на аккаунте релейера, которые можно передать прямо сейчас."""
        now = time.time()
        # передача подарка с can_transfer_at в будущем пока заблокирована Telegram
        return [i for i in await self._saved() if i["kind"] == "nft" and i["transfer_at"] <= now]

    async def _peer(self, user_id: int, username: str | None) -> Any:
        """InputPeer игрока. RelayerError('NEED_CONTACT') — релейер не может его найти (нет @username и переписки)."""
        for target in ([username] if username else []) + [user_id]:
            try:
                return await self.client.get_input_entity(target)
            except (ValueError, TypeError):
                continue
            except Exception as e:
                log.warning("Не удалось найти игрока %s: %s", user_id, type(e).__name__)
        raise RelayerError("NEED_CONTACT")

    async def send_gift(self, gift_id: str, user_id: int, username: str | None, text: str = "") -> None:
        """Дарит игроку обычный подарок Telegram за звёзды с баланса релейера (вывод звёзд)."""
        if not self.ready:
            raise RelayerError("Релейер не подключён")
        from telethon.tl.functions.payments import GetPaymentFormRequest, SendStarsFormRequest
        from telethon.tl.types import InputInvoiceStarGift, TextWithEntities
        peer = await self._peer(user_id, username)
        invoice = InputInvoiceStarGift(peer=peer, gift_id=int(gift_id),
                                       message=TextWithEntities(text=text, entities=[]) if text else None)
        form = await self.client(GetPaymentFormRequest(invoice=invoice))
        await self.client(SendStarsFormRequest(form_id=form.form_id, invoice=invoice))

    async def stars_balance(self) -> int | None:
        """Сколько звёзд на аккаунте релейера — из них оплачиваются выводы и платные передачи NFT."""
        if not self.ready:
            return None
        from telethon.tl.functions.payments import GetStarsStatusRequest
        from telethon.tl.types import InputPeerSelf
        status = await self.client(GetStarsStatusRequest(peer=InputPeerSelf()))
        balance = status.balance
        return getattr(balance, "amount", balance)

    async def transfer(self, item: dict[str, Any], user_id: int, username: str | None) -> None:
        """Передаёт подарок игроку. RelayerError('NEED_CONTACT') — релейер не может найти игрока."""
        if not self.ready:
            raise RelayerError("Релейер не подключён")
        from telethon.tl.functions.payments import (GetPaymentFormRequest, SendStarsFormRequest,
                                                    TransferStarGiftRequest)
        from telethon.tl.types import InputInvoiceStarGiftTransfer, InputSavedStarGiftUser
        peer = await self._peer(user_id, username)
        stargift = InputSavedStarGiftUser(msg_id=item["ref"])
        if item.get("transfer_stars"):
            # Платная передача: оплачиваем звёздами с баланса релейера
            invoice = InputInvoiceStarGiftTransfer(stargift=stargift, to_id=peer)
            form = await self.client(GetPaymentFormRequest(invoice=invoice))
            await self.client(SendStarsFormRequest(form_id=form.form_id, invoice=invoice))
        else:
            await self.client(TransferStarGiftRequest(stargift=stargift, to_id=peer))

    def on_private_message(self, callback: Any) -> None:
        """callback(user_id) — игрок написал релейеру или прислал ему подарок."""
        if not self.ready:
            return
        from telethon import events
        from telethon.tl.types import (MessageActionStarGift, MessageActionStarGiftUnique, MessageService, PeerUser,
                                       UpdateNewMessage)

        async def handler(event: Any) -> None:
            if event.is_private and event.sender_id:
                await callback(event.sender_id)

        async def gift_handler(update: Any) -> None:
            # подарок приходит служебным сообщением, которое NewMessage не ловит
            msg = getattr(update, "message", None)
            if (isinstance(update, UpdateNewMessage) and isinstance(msg, MessageService) and not msg.out
                    and isinstance(msg.action, (MessageActionStarGift, MessageActionStarGiftUnique))
                    and isinstance(msg.peer_id, PeerUser)):
                await callback(msg.peer_id.user_id)

        self.client.add_event_handler(handler, events.NewMessage(incoming=True))
        self.client.add_event_handler(gift_handler, events.Raw(UpdateNewMessage))

    async def username(self) -> str | None:
        if not self.ready:
            return None
        user = await self.client.get_me()
        return user.username
