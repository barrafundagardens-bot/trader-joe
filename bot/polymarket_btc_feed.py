"""
bot/polymarket_btc_feed.py — Descoberta do mercado BTC 15-min ativo no Polymarket.

A Polymarket lista mercados intradiários do tipo "Bitcoin Up or Down — HHam ET"
que abrem e fecham em janelas curtas. Este módulo encontra o mercado ativo
mais próximo de fechar (menor tempo até endDate, ainda no futuro).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class BTCMarket:
    question: str
    slug: str
    condition_id: str
    token_yes: str
    token_no: str
    yes_price: float
    no_price: float
    end_time: datetime
    liquidity: float
    volume: float

    @property
    def seconds_to_close(self) -> float:
        return (self.end_time - datetime.now(timezone.utc)).total_seconds()


class PolymarketBTCFeed:
    """Encontra o mercado BTC 15-min ativo no Polymarket via gamma-api."""

    def __init__(self, timeout: int = 10, search_limit: int = 100):
        self.timeout = timeout
        self.search_limit = search_limit
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "trader-joe-btc-bot/0.1"})

    def find_active_market(
        self,
        max_seconds_to_close: int = 3600,
        keywords: tuple[str, ...] = ("bitcoin", "btc"),
    ) -> Optional[BTCMarket]:
        """
        Mercado BTC ativo mais próximo de fechar.

        Args:
            max_seconds_to_close: Ignora mercados com endDate > N segundos no futuro.
            keywords: Substrings que a question deve conter (case insensitive).

        Returns:
            BTCMarket ou None se nada relevante for encontrado.
        """
        candidates = self._fetch_candidates()
        now = datetime.now(timezone.utc)

        best: Optional[BTCMarket] = None
        best_dt = float("inf")

        for raw in candidates:
            question = (raw.get("question") or "").lower()
            if not any(k in question for k in keywords):
                continue
            if "up or down" not in question and "up/down" not in question:
                continue

            market = self._parse(raw)
            if market is None:
                continue

            dt = (market.end_time - now).total_seconds()
            if dt <= 0 or dt > max_seconds_to_close:
                continue
            if dt < best_dt:
                best = market
                best_dt = dt

        if best:
            logger.info(
                f"[PolymarketBTC] Mercado ativo: '{best.question}' "
                f"(fecha em {best.seconds_to_close:.0f}s, "
                f"yes={best.yes_price:.3f}, liq=${best.liquidity:.0f})"
            )
        else:
            logger.debug("[PolymarketBTC] Nenhum mercado BTC 15-min ativo encontrado")
        return best

    def _fetch_candidates(self) -> list[dict]:
        try:
            resp = self._session.get(
                f"{GAMMA_API}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "order": "endDate",
                    "ascending": "true",
                    "limit": self.search_limit,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except requests.RequestException as e:
            logger.warning(f"[PolymarketBTC] Erro ao buscar markets: {e}")
            return []

    @staticmethod
    def _parse(m: dict) -> Optional[BTCMarket]:
        try:
            token_ids = m.get("clobTokenIds", [])
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)
            if len(token_ids) < 2:
                return None

            prices = m.get("outcomePrices", [])
            if isinstance(prices, str):
                prices = json.loads(prices)
            if len(prices) < 2:
                return None

            end_str = m.get("endDate") or ""
            end_time = datetime.fromisoformat(end_str.replace("Z", "+00:00"))

            return BTCMarket(
                question=m.get("question", ""),
                slug=m.get("slug", ""),
                condition_id=m.get("conditionId", ""),
                token_yes=str(token_ids[0]),
                token_no=str(token_ids[1]),
                yes_price=float(prices[0]),
                no_price=float(prices[1]),
                end_time=end_time,
                liquidity=float(m.get("liquidityNum", 0) or 0),
                volume=float(m.get("volumeNum", 0) or 0),
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            logger.debug(f"[PolymarketBTC] Falha ao parsear mercado: {e}")
            return None
