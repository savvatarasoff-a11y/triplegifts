"""Кейсы из настоящих подарков Telegram с живыми ценами и NFT-кейс из NFT админа."""
from __future__ import annotations

import logging
import time
from typing import Any

from .db import Database
from .games import logic as g
from .nft import PRICE_MAX_AGE
from .withdraw import GiftCatalog

log = logging.getLogger(__name__)


class CaseCatalog:
    def __init__(self, gifts: GiftCatalog, db: Database, nft_case_price: int):
        self.gifts = gifts
        self.db = db
        self.nft_case_price = nft_case_price

    async def _gift_prices(self) -> dict[str, dict[str, Any]]:
        """Самый дешёвый обычный подарок для каждого эмодзи: {эмодзи: {id, stars}}."""
        prices: dict[str, dict[str, Any]] = {}
        for gift in await self.gifts.list():
            known = prices.get(gift["emoji"])
            if known is None or gift["stars"] < known["stars"]:
                prices[gift["emoji"]] = gift
        return prices

    async def list(self) -> list[dict[str, Any]]:
        """Доступные кейсы с призами и вероятностями (в процентах)."""
        try:
            prices = await self._gift_prices()
        except Exception:
            log.warning("Каталог подарков недоступен — кейсы временно выключены")
            return []
        cases = []
        models = await self._models()
        for case in g.CASE_DEFS:
            if any(emoji not in prices for emoji, _ in case.items):
                log.warning("Кейс %s выключен: не все подарки есть в каталоге", case.id)
                continue
            prizes = [
                {"kind": "gift", "emoji": emoji, "gift_id": prices[emoji]["id"], "amount": prices[emoji]["stars"],
                 "weight": w}
                for emoji, w in case.items
            ]
            prizes = self._with_jackpot(case.price, prizes, models)
            if prizes is None:
                continue
            cases.append(self._finish(case.id, case.name, case.emoji, case.price, prizes))
        for nft_def in g.NFT_CASE_DEFS:
            cases.append(await self._nft_case(prices, nft_def))
        return [c for c in cases if c is not None]

    async def _models(self) -> list[dict[str, Any]]:
        return await self.db.all(
            "SELECT * FROM nft_models WHERE enabled=1 AND stock > reserved AND price > 0 AND price_at > ? "
            "AND model GLOB '*[^0-9]*' ORDER BY price", time.time() - PRICE_MAX_AGE)

    @staticmethod
    def _with_jackpot(price: int, prizes: list[dict[str, Any]], models: list[dict[str, Any]]
                      ) -> list[dict[str, Any]] | None:
        """Кейс с подарками получает NFT-джекпот — модели от 3 до 100 цен кейса (без него кейс, в котором
        все подарки не дороже цены, не показываем: выиграть в нём больше цены невозможно)."""
        lo, hi = (k * price for k in g.GIFT_CASE_JACKPOT_RANGE)
        fit = [m for m in models if lo <= m["price"] <= hi]
        if len(fit) > g.GIFT_CASE_JACKPOT_MAX_MODELS:      # равномерно по цене — и дешёвые, и дорогие
            step = (len(fit) - 1) / (g.GIFT_CASE_JACKPOT_MAX_MODELS - 1)
            fit = [fit[round(i * step)] for i in range(g.GIFT_CASE_JACKPOT_MAX_MODELS)]
        res = g.gift_case_with_jackpot(price, [(p["weight"], p["amount"]) for p in prizes],
                                       [m["price"] for m in fit]) if fit else None
        if res is None:
            return prizes if max(p["amount"] for p in prizes) > price else None
        p_nft, gift_w = res
        out = [{**p, "weight": w} for p, w in zip(prizes, gift_w)]
        out += [{"kind": "nft", "model_id": m["id"], "emoji": m["emoji"] or "💎", "title": m["collection_name"],
                 "model": m["model"], "rarity": m["rarity"], "amount": m["price"], "weight": pw,
                 "demo": bool(m["test"])} for m, pw in zip(fit, p_nft)]
        return out

    async def _nft_case(self, prices: dict[str, dict[str, Any]], d: g.NftCaseDef) -> dict[str, Any] | None:
        """NFT-кейс из моделей, которые есть у релейера и чья цена проверена на маркете не позже часа назад."""
        models = await self.db.all(
            "SELECT * FROM nft_models WHERE enabled=1 AND stock > reserved AND price > 0 AND price_at > ? "
            "AND model GLOB '*[^0-9]*' "            # неулучшенные подарки (число вместо модели) не показываем
            "ORDER BY price DESC",
            time.time() - PRICE_MAX_AGE,
        )
        if not models or not prices:
            return None
        regular = sorted(prices.values(), key=lambda x: x["stars"])
        price = d.price or self.nft_case_price
        # в дорогом кейсе — только NFT не дешевле половины его цены (иначе дешёвые NFT «съедают» шансы)
        models = [m for m in models if m["price"] >= price * g.NFT_CASE_MIN_PRICE_SHARE]
        if not models:
            return None
        probs = g.nft_case_weights(price, [m["price"] for m in models], [x["stars"] for x in regular], share=d.share)
        if probs is None:
            return None
        p_nft, p_gift = probs
        prizes = [
            {"kind": "nft", "model_id": m["id"], "emoji": m["emoji"] or "💎", "title": m["collection_name"],
             "model": m["model"], "rarity": m["rarity"], "amount": m["price"], "weight": p, "demo": bool(m["test"])}
            for m, p in zip(models, p_nft)
        ]
        prizes += [
            {"kind": "gift", "emoji": x["emoji"], "gift_id": x["id"], "amount": x["stars"], "weight": p}
            for x, p in zip(regular, p_gift) if p > 0
        ]
        return self._finish(d.id, d.name, d.emoji, price, prizes)

    @staticmethod
    def _finish(case_id: str, name: str, emoji: str, price: int, prizes: list[dict[str, Any]]) -> dict[str, Any] | None:
        total = sum(p["weight"] for p in prizes)
        rtp = g.expected_value([(p["amount"], p["weight"]) for p in prizes]) / price
        if rtp > g.CASE_MAX_RTP:
            log.warning("Кейс %s выключен: с текущими ценами RTP %.1f%% выше допустимого", case_id, rtp * 100)
            return None
        if rtp < g.CASE_MIN_RTP:
            log.info("Кейс %s скрыт: с текущими ценами RTP %.1f%% — слишком невыгоден игрокам", case_id, rtp * 100)
            return None
        for p in prizes:
            p["chance"] = round(p["weight"] / total * 100, 3)
        return {"id": case_id, "name": name, "emoji": emoji, "price": price, "rtp": round(rtp, 4), "prizes": prizes}

    async def get(self, case_id: str) -> dict[str, Any] | None:
        return next((c for c in await self.list() if c["id"] == case_id), None)
