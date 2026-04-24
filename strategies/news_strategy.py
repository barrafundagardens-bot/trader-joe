"""
strategies/news_strategy.py — News Edge: sinais baseados em notícias.

Detecta quando notícias relevantes criam um desalinhamento entre o
sentimento do mercado de mídia e o preço atual no Polymarket.

Lógica central:
    1. Buscar artigos recentes via NewsFetcher (NewsAPI + RSS)
    2. Calcular score de sentimento agregado (-1.0 a +1.0)
    3. Comparar sentimento com posição atual do preço
    4. Sinal BUY se: sentimento bullish + preço < 0.65 (mercado sub-precificou)
    5. Sinal SELL se: sentimento bearish + preço > 0.35 (mercado super-precificou)

Modos:
    live:       Chamadas reais a NewsAPI + RSS a cada `fetch_interval` candles
    paper:      Usa momentum de preço como proxy de impacto de notícias (offline)

Parâmetros:
    market_keywords:      Termos de busca (ex: ["bitcoin", "BTC"])
    sentiment_threshold:  Score mínimo para gerar sinal (padrão: 0.30)
    price_ceiling_buy:    Preço máximo para sinal BUY (padrão: 0.75)
    price_floor_sell:     Preço mínimo para sinal SELL (padrão: 0.25)
    fetch_interval:       A cada N candles, buscar notícias novas (padrão: 5)
    order_size:           Tamanho da ordem em USDC (padrão: 5.0)
    simulation:           Se True, modo paper offline (padrão: True)
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

import pandas as pd
from dotenv import load_dotenv

from strategies.base_strategy import BaseStrategy, Signal, SignalType
from bot.news_fetcher import NewsFetcher

load_dotenv()
logger = logging.getLogger(__name__)


class NewsStrategy(BaseStrategy):
    """
    Estratégia de trading baseada em sentimento de notícias.

    Em paper mode (simulation=True): usa momentum de preço + volume
    como proxy do impacto de notícias, sem chamadas HTTP. Permite
    backtesting e paper trading 100% offline.

    Em live mode (simulation=False): busca notícias reais via
    NewsFetcher a cada `fetch_interval` candles.

    Args:
        params:           Dicionário de configuração (veja parâmetros acima).
        market_keywords:  Keywords do mercado para filtragem de notícias.
                          Sobrescreve params['market_keywords'] se fornecido.
    """

    def __init__(
        self,
        params: Optional[dict] = None,
        market_keywords: Optional[List[str]] = None,
    ):
        super().__init__(params)

        keywords = market_keywords or self.get_param(
            "market_keywords", ["polymarket", "prediction", "market"]
        )
        simulation = self.get_param("simulation", True)

        self._fetcher: Optional[NewsFetcher] = None
        if not simulation:
            self._fetcher = NewsFetcher(
                market_keywords=keywords,
                news_api_key=os.getenv("NEWS_API_KEY", ""),
                max_age_hours=self.get_param("max_news_age_hours", 4.0),
                cache_ttl_sec=self.get_param("news_cache_ttl_sec", 300),
            )

        # Controle de quando fazer o próximo fetch
        self._candle_counter: int = 0
        self._last_sentiment: float = 0.0
        self._last_fetch_candle: int = -999

    @property
    def name(self) -> str:
        return "NewsEdge"

    @property
    def min_candles(self) -> int:
        return 20

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Gera sinal baseado em sentimento de notícias ou proxy de momentum.

        Args:
            df: DataFrame OHLCV com DatetimeIndex.

        Returns:
            Signal com BUY, SELL ou HOLD.
        """
        if not self.validate_dataframe(df):
            return self.hold_signal("Dados insuficientes para NewsEdge")

        self._candle_counter += 1
        candle = df.iloc[-1]
        ts = df.index[-1]
        simulation = self.get_param("simulation", True)

        if simulation:
            return self._signal_from_momentum(df, candle, ts)
        else:
            return self._signal_from_news(candle, ts)

    # ------------------------------------------------------------------
    # Modo paper: momentum como proxy de notícia
    # ------------------------------------------------------------------

    def _signal_from_momentum(
        self, df: pd.DataFrame, candle: pd.Series, ts: pd.Timestamp
    ) -> Signal:
        """
        Simula impacto de notícias via momentum de preço sustentado.

        Heurística: uma notícia relevante se manifesta como uma série de
        candles com momentum consistente (3+ candles na mesma direção)
        e volume acima da média. Isso diferencia do ruído de mercado.
        """
        sentiment_threshold = self.get_param("sentiment_threshold", 0.30)
        price_ceiling_buy = self.get_param("price_ceiling_buy", 0.75)
        price_floor_sell = self.get_param("price_floor_sell", 0.25)
        order_size = self.get_param("order_size", 5.0)
        momentum_candles = self.get_param("momentum_candles", 3)

        current_price = float(candle["close"])
        lookback = df.tail(momentum_candles + 1)

        # Calcular direção dos últimos N candles
        closes = lookback["close"].values
        directions = [1 if closes[i] > closes[i - 1] else -1 for i in range(1, len(closes))]

        # Consenso direcional (todos na mesma direção = sinal mais forte)
        if len(directions) == 0:
            return self.hold_signal("Sem candles suficientes para momentum", timestamp=ts)

        consensus = sum(directions) / len(directions)  # -1.0 a +1.0

        # Volume médio para confirmar que o movimento tem força
        avg_volume = df["volume"].iloc[-20:-1].mean()
        current_volume = float(candle["volume"])
        volume_ok = avg_volume > 0 and current_volume >= avg_volume * 1.2

        # Score sintético de "notícia" baseado em consenso + volume
        if volume_ok:
            synthetic_sentiment = consensus  # -1.0 a +1.0
        else:
            synthetic_sentiment = consensus * 0.5  # Penalizar sem volume

        if abs(synthetic_sentiment) < sentiment_threshold:
            return self.hold_signal(
                f"Momentum insuficiente: sentimento sintético={synthetic_sentiment:+.2f} "
                f"(threshold={sentiment_threshold})",
                timestamp=ts,
            )

        # Verificar se o preço ainda não refletiu o "sentimento"
        if synthetic_sentiment > 0 and current_price > price_ceiling_buy:
            return self.hold_signal(
                f"Sentimento bullish mas preço já alto ({current_price:.4f} > {price_ceiling_buy})",
                timestamp=ts,
            )

        if synthetic_sentiment < 0 and current_price < price_floor_sell:
            return self.hold_signal(
                f"Sentimento bearish mas preço já baixo ({current_price:.4f} < {price_floor_sell})",
                timestamp=ts,
            )

        signal_type = SignalType.BUY if synthetic_sentiment > 0 else SignalType.SELL
        confidence = min(0.85, 0.50 + abs(synthetic_sentiment) * 0.35)

        return Signal(
            signal_type=signal_type,
            price=current_price,
            size=order_size,
            confidence=confidence,
            strategy=self.name,
            reason=(
                f"News proxy: sentimento={synthetic_sentiment:+.2f} "
                f"momentum={consensus:+.2f} ({momentum_candles} candles) "
                f"vol_ratio={current_volume / avg_volume:.1f}x "
                f"price={current_price:.4f}"
            ),
            timestamp=ts,
            metadata={
                "synthetic_sentiment": round(synthetic_sentiment, 3),
                "momentum_consensus": round(consensus, 3),
                "volume_ratio": round(current_volume / avg_volume, 2) if avg_volume > 0 else 0,
                "volume_confirmed": volume_ok,
                "simulation": True,
                "source": "momentum_proxy",
            },
        )

    # ------------------------------------------------------------------
    # Modo live: sentimento real via NewsFetcher
    # ------------------------------------------------------------------

    def _signal_from_news(
        self, candle: pd.Series, ts: pd.Timestamp
    ) -> Signal:
        """
        Gera sinal baseado em sentimento real de notícias.

        Atualiza o score de sentimento a cada `fetch_interval` candles
        para evitar exceder rate limits das APIs.
        """
        fetch_interval = self.get_param("fetch_interval", 5)
        sentiment_threshold = self.get_param("sentiment_threshold", 0.30)
        price_ceiling_buy = self.get_param("price_ceiling_buy", 0.75)
        price_floor_sell = self.get_param("price_floor_sell", 0.25)
        order_size = self.get_param("order_size", 5.0)

        current_price = float(candle["close"])

        # Atualizar sentimento apenas a cada N candles
        if self._candle_counter - self._last_fetch_candle >= fetch_interval:
            self._last_sentiment = self._fetcher.get_sentiment_score()
            self._last_fetch_candle = self._candle_counter
            logger.info(
                f"[{self.name}] Sentimento atualizado: {self._last_sentiment:+.3f}"
            )

        sentiment = self._last_sentiment

        if abs(sentiment) < sentiment_threshold:
            return self.hold_signal(
                f"Sentimento neutro: {sentiment:+.3f} (threshold=±{sentiment_threshold})",
                timestamp=ts,
            )

        if sentiment > 0 and current_price > price_ceiling_buy:
            return self.hold_signal(
                f"Bullish mas preço já precificado ({current_price:.4f} > {price_ceiling_buy})",
                timestamp=ts,
            )

        if sentiment < 0 and current_price < price_floor_sell:
            return self.hold_signal(
                f"Bearish mas preço já precificado ({current_price:.4f} < {price_floor_sell})",
                timestamp=ts,
            )

        # Buscar artigos para enriquecer o log
        recent_articles = self._fetcher.get_recent_articles()
        top_titles = [a.title[:50] for a in recent_articles[:3]]

        signal_type = SignalType.BUY if sentiment > 0 else SignalType.SELL
        direction = "bullish" if sentiment > 0 else "bearish"
        confidence = min(0.88, 0.55 + abs(sentiment) * 0.33)

        return Signal(
            signal_type=signal_type,
            price=current_price,
            size=order_size,
            confidence=confidence,
            strategy=self.name,
            reason=(
                f"News {direction}: sentimento={sentiment:+.3f} "
                f"price={current_price:.4f} "
                f"artigos={len(recent_articles)}"
            ),
            timestamp=ts,
            metadata={
                "sentiment_score": round(sentiment, 3),
                "article_count": len(recent_articles),
                "top_headlines": top_titles,
                "simulation": False,
                "source": "newsapi+rss",
            },
        )
