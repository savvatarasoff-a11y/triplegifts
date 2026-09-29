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

    async def list(self) -> list[dict[str, Any]]:
        """Доступные кейсы с призами и вероятностями (в процентах). Только NFT-кейсы: NFT + звёзды."""
        cases = [await self._nft_case(d) for d in g.NFT_CASE_DEFS]
        return [c for c in cases if c is not None]

    async def _nft_case(self, d: g.NftCaseDef) -> dict[str, Any] | None:
        """NFT-кейс из моделей, которые есть у релейера и чья цена проверена на маркете не позже часа назад."""
        models = await self.db.all(
            "SELECT * FROM nft_models WHERE enabled=1 AND stock > reserved AND price > 0 AND price_at > ? "
            "ORDER BY price DESC",
            time.time() - PRICE_MAX_AGE,
        )
        if not models:
            return None
        price = d.price or self.nft_case_price
        # утешительные призы — звёзды (обычных подарков в кейсах нет)
        fillers = sorted({max(1, round(price * k)) for k in g.CASE_STAR_FILLERS})
        # в дорогом кейсе — только NFT не дешевле половины его цены (иначе дешёвые NFT «съедают» шансы)
        models = [m for m in models if m["price"] >= price * g.NFT_CASE_MIN_PRICE_SHARE]
        if not models:
            return None
        probs = g.nft_case_weights(price, [m["price"] for m in models], fillers, share=d.share)
        if probs is None:
            return None
        p_nft, p_gift = probs
        prizes = [
            {"kind": "nft", "model_id": m["id"], "emoji": m["emoji"] or "💎", "title": m["collection_name"],
             "model": m["model"], "rarity": m["rarity"], "amount": m["price"], "weight": p, "demo": bool(m["test"])}
            for m, p in zip(models, p_nft)
        ]
        prizes += [
            {"kind": "stars", "emoji": "⭐", "amount": amount, "weight": p}
            for amount, p in zip(fillers, p_gift) if p > 0
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
