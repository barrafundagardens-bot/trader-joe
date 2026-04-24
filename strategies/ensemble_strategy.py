"""
strategies/ensemble_strategy.py — Combina múltiplas estratégias.

Roda as 3 estratégias novas em paralelo (spread, copytrade, news)
e gera um sinal consolidado baseado em votação/consenso.

Lógica:
    1. Cada estratégia gera seu próprio sinal
    2. Contar votos: BUY vs SELL vs HOLD
    3. Se >= 2 estratégias concordam (BUY ou SELL), executar esse sinal
    4. Confidence é a média das estratégias que votaram

Exemplo:
    - Spread: BUY (conf=0.75)
    - Copytrade: BUY (conf=0.65)
    - News: HOLD (conf=0.0)
    → Resultado: BUY com confidence=0.70 (média de 2 votos)

Parâmetros:
    min_consensus: Mínimo de estratégias que devem concordar (padrão: 1)
    weighting: Como ponderar os votos ('equal' ou 'confidence')
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from strategies.base_strategy import BaseStrategy, Signal, SignalType
from strategies.spread_strategy import SpreadFarmingStrategy
from strategies.copytrade_strategy import CopytradeStrategy
from strategies.news_strategy import NewsStrategy

logger = logging.getLogger(__name__)


class EnsembleStrategy(BaseStrategy):
    """
    Estratégia ensemble que combina Spread, Copytrade e News.

    Cada sub-estratégia roda independentemente, e o ensemble
    toma uma decisão baseada em votação simples.

    Args:
        params: Dicionário de configuração.
    """

    def __init__(self, params: Optional[dict] = None):
        super().__init__(params)

        # Instanciar as 3 estratégias
        self._spread = SpreadFarmingStrategy(
            params=self.get_param("spread_params", {})
        )
        self._copytrade = CopytradeStrategy(
            params=self.get_param("copytrade_params", {})
        )
        self._news = NewsStrategy(
            params=self.get_param("news_params", {})
        )

    @property
    def name(self) -> str:
        return "Ensemble"

    @property
    def min_candles(self) -> int:
        # Máximo dos min_candles das sub-estratégias
        return max(
            self._spread.min_candles,
            self._copytrade.min_candles,
            self._news.min_candles,
        )

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Gera sinal consolidado das 3 estratégias via votação.

        Args:
            df: DataFrame OHLCV com DatetimeIndex.

        Returns:
            Signal com BUY, SELL ou HOLD baseado em consenso.
        """
        if not self.validate_dataframe(df):
            return self.hold_signal("Dados insuficientes para Ensemble")

        ts = df.index[-1]
        min_consensus = self.get_param("min_consensus", 1)
        weighting = self.get_param("weighting", "equal")

        # Gerar sinais de cada estratégia
        signal_spread = self._spread.generate_signal(df)
        signal_copytrade = self._copytrade.generate_signal(df)
        signal_news = self._news.generate_signal(df)

        signals = [signal_spread, signal_copytrade, signal_news]
        signal_names = ["Spread", "Copytrade", "News"]

        # Filtrar sinais acionáveis (não HOLD)
        actionable = [
            (s, name)
            for s, name in zip(signals, signal_names)
            if s.signal_type != SignalType.HOLD
        ]

        if not actionable:
            reasons = "; ".join(
                [f"{name}: {s.reason}" for s, name in zip(signals, signal_names)]
            )
            return self.hold_signal(
                f"Sem consenso. {reasons}",
                timestamp=ts,
            )

        # Contar votos por direção
        buy_votes = [s for s, _ in actionable if s.signal_type == SignalType.BUY]
        sell_votes = [s for s, _ in actionable if s.signal_type == SignalType.SELL]

        # Determinar direção dominante
        if len(buy_votes) >= min_consensus:
            dominant_direction = SignalType.BUY
            consensus_signals = buy_votes
            consensus_names = [
                name for s, name in actionable if s.signal_type == SignalType.BUY
            ]
        elif len(sell_votes) >= min_consensus:
            dominant_direction = SignalType.SELL
            consensus_signals = sell_votes
            consensus_names = [
                name for s, name in actionable if s.signal_type == SignalType.SELL
            ]
        else:
            reasons = "; ".join(
                [f"{name}: {s.reason}" for s, name in zip(signals, signal_names)]
            )
            return self.hold_signal(
                f"Consenso insuficiente: "
                f"{len(buy_votes)} BUY, {len(sell_votes)} SELL "
                f"(mínimo: {min_consensus}). {reasons}",
                timestamp=ts,
            )

        # Calcular confidence agregada
        if weighting == "confidence":
            avg_confidence = (
                sum(s.confidence for s in consensus_signals) / len(consensus_signals)
            )
        else:  # equal weighting
            avg_confidence = min(0.90, 0.5 + len(consensus_signals) * 0.15)

        # Preço médio das estratégias que votaram
        avg_price = sum(s.price for s in consensus_signals) / len(consensus_signals)

        # Size médio
        avg_size = sum(s.size for s in consensus_signals) / len(consensus_signals)

        direction_str = "BUY" if dominant_direction == SignalType.BUY else "SELL"

        return Signal(
            signal_type=dominant_direction,
            price=avg_price,
            size=avg_size,
            confidence=avg_confidence,
            strategy=self.name,
            reason=(
                f"Ensemble consenso: {direction_str} "
                f"({len(consensus_signals)}/{len(actionable)} estratégias) "
                f"| {', '.join(consensus_names)} "
                f"| price={avg_price:.4f} "
                f"| conf={avg_confidence:.2f}"
            ),
            timestamp=ts,
            metadata={
                "consensus_count": len(consensus_signals),
                "total_actionable": len(actionable),
                "buy_count": len(buy_votes),
                "sell_count": len(sell_votes),
                "consensus_strategies": consensus_names,
                "spread_signal": signal_spread.signal_type.value,
                "spread_confidence": signal_spread.confidence,
                "spread_reason": signal_spread.reason,
                "copytrade_signal": signal_copytrade.signal_type.value,
                "copytrade_confidence": signal_copytrade.confidence,
                "copytrade_reason": signal_copytrade.reason,
                "news_signal": signal_news.signal_type.value,
                "news_confidence": signal_news.confidence,
                "news_reason": signal_news.reason,
            },
        )
