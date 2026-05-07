"""
bot/binance_feed.py — Feed REST de preço/klines da Binance para o BTC bot.

REST simples (sem WebSocket): para dry-run em loop de 60s, polling REST é
suficiente e elimina dependências de aiohttp/websockets.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

BINANCE_REST = "https://api.binance.com"


class BinanceFeed:
    """Cliente REST minimalista da Binance para BTCUSDT."""

    def __init__(self, symbol: str = "BTCUSDT", timeout: int = 5):
        self.symbol = symbol
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "trader-joe-btc-bot/0.1"})

    def fetch_klines(self, interval: str = "1m", limit: int = 200) -> pd.DataFrame:
        """
        Últimos `limit` candles OHLCV de BTCUSDT.

        Returns:
            DataFrame com colunas open/high/low/close/volume e DatetimeIndex (UTC).
            DataFrame vazio em caso de erro.
        """
        url = f"{BINANCE_REST}/api/v3/klines"
        params = {"symbol": self.symbol, "interval": interval, "limit": limit}

        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            raw = resp.json()
        except requests.RequestException as e:
            logger.warning(f"[BinanceFeed] Erro ao buscar klines: {e}")
            return pd.DataFrame()

        if not raw:
            return pd.DataFrame()

        df = pd.DataFrame(
            raw,
            columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "qav", "n_trades", "tbb", "tbq", "ignore",
            ],
        )
        df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]].astype(float)
        return df

    def fetch_price(self) -> Optional[float]:
        """Preço spot atual. Returns None em erro."""
        url = f"{BINANCE_REST}/api/v3/ticker/price"
        try:
            resp = self._session.get(url, params={"symbol": self.symbol}, timeout=self.timeout)
            resp.raise_for_status()
            return float(resp.json()["price"])
        except (requests.RequestException, KeyError, ValueError) as e:
            logger.warning(f"[BinanceFeed] Erro ao buscar preço: {e}")
            return None

    def fetch_price_at(self, when: datetime) -> Optional[float]:
        """
        Preço de fechamento do candle 1m que contém `when` (UTC).

        Útil para reconstruir o preço BTC no início ou fim de uma janela
        de mercado Polymarket que já passou.
        """
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        start_ms = int(when.timestamp() * 1000)
        end_ms = start_ms + 60_000

        url = f"{BINANCE_REST}/api/v3/klines"
        params = {
            "symbol": self.symbol,
            "interval": "1m",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1,
        }
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            raw = resp.json()
            if not raw:
                return None
            return float(raw[0][4])  # close
        except (requests.RequestException, KeyError, ValueError, IndexError) as e:
            logger.warning(f"[BinanceFeed] Erro em fetch_price_at({when}): {e}")
            return None
