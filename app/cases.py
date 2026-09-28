"""Кейсы из настоящих подарков Telegram с живыми ценами и NFT-кейс из NFT админа."""
from __future__ import annotations

import logging
from typing import Any

from .db import Database
from .games import logic as g
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
        for case in g.CASE_DEFS:
            if any(emoji not in prices for emoji, _ in case.items):
                log.warning("Кейс %s выключен: не все подарки есть в каталоге", case.id)
                continue
            prizes = [
                {"kind": "gift", "emoji": emoji, "gift_id": prices[emoji]["id"], "amount": prices[emoji]["stars"],
                 "weight": w}
                for emoji, w in case.items
            ]
            cases.append(self._finish(case.id, case.name, case.emoji, case.price, prizes))
        nft_case = await self._nft_case(prices)
        if nft_case:
            cases.append(nft_case)
        return [c for c in cases if c is not None]

    async def _nft_case(self, prices: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        nfts = await self.db.all(
            "SELECT * FROM nft_prizes WHERE status='available' AND price IS NOT NULL AND price > 0 ORDER BY price DESC"
        )
        if not nfts or not prices:
            return None
        regular = sorted(prices.values(), key=lambda x: x["stars"])
        probs = g.nft_case_weights(self.nft_case_price, [n["price"] for n in nfts], [x["stars"] for x in regular])
        if probs is None:
            return None
        p_nft, p_gift = probs
        prizes = [
            {"kind": "nft", "nft_id": n["id"], "emoji": n["emoji"] or "💎", "title": n["title"], "model": n["model"],
             "rarity": n["rarity"], "amount": n["price"], "weight": p}
            for n, p in zip(nfts, p_nft)
        ]
        prizes += [
            {"kind": "gift", "emoji": x["emoji"], "gift_id": x["id"], "amount": x["stars"], "weight": p}
            for x, p in zip(regular, p_gift) if p > 0
        ]
        return self._finish(g.NFT_CASE_ID, "NFT-кейс", "💎", self.nft_case_price, prizes)

    @staticmethod
    def _finish(case_id: str, name: str, emoji: str, price: int, prizes: list[dict[str, Any]]) -> dict[str, Any] | None:
        total = sum(p["weight"] for p in prizes)
        rtp = g.expected_value([(p["amount"], p["weight"]) for p in prizes]) / price
        if rtp > g.CASE_MAX_RTP:
            log.warning("Кейс %s выключен: с текущими ценами RTP %.1f%% выше допустимого", case_id, rtp * 100)
            return None
        for p in prizes:
            p["chance"] = round(p["weight"] / total * 100, 3)
        return {"id": case_id, "name": name, "emoji": emoji, "price": price, "rtp": round(rtp, 4), "prizes": prizes}

    async def get(self, case_id: str) -> dict[str, Any] | None:
        return next((c for c in await self.list() if c["id"] == case_id), None)
