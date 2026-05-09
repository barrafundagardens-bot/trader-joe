"""
strategies/btc_rsi_momentum_strategy.py — RSI de momentum para BTC 1m.

Diferente do RSIStrategy (que espera oversold/overbought + tendência),
esta strategy detecta mudanças de direção quando RSI cruza o nível 50:
  BUY:  RSI cruza de baixo para cima do pivot (default 50) com magnitude
  SELL: RSI cruza de cima para baixo do pivot com magnitude

Calibrada para candles 1m da Binance (BTC em dólares), sem filtro de
slope em unidade absoluta de preço (que quebra fora de Polymarket).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import ta.momentum

from strategies.base_strategy import BaseStrategy, Signal, SignalType


class BTCRSIMomentumStrategy(BaseStrategy):
    """
    RSI de momentum: detecta cruzamentos da linha de equilíbrio (50).

    Parâmetros:
        rsi_period:     Período do RSI (padrão: 14)
        pivot:          Nível de cruzamento (padrão: 50)
        min_magnitude:  Distância mínima do RSI ao pivot após cruzar (padrão: 3)
        lookback:       Quantos candles olhar atrás pra confirmar o cruzamento (padrão: 2)
        confidence_base: Confiança base do sinal (padrão: 0.58)
    """

    DEFAULT_PARAMS = {
        "rsi_period": 14,
        "pivot": 50.0,
        "min_magnitude": 3.0,
        "lookback": 2,
        "confidence_base": 0.58,
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)

    @property
    def name(self) -> str:
        return "BTCRSIMomentumStrategy"

    @property
    def min_candles(self) -> int:
        return self.get_param("rsi_period") + self.get_param("lookback") + 5

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        if not self.validate_dataframe(df):
            return self.hold_signal("Candles insuficientes")

        close = df["close"]
        timestamp = df.index[-1]
        period = self.get_param("rsi_period")
        pivot = self.get_param("pivot")
        min_mag = self.get_param("min_magnitude")
        lookback = self.get_param("lookback")

        rsi = ta.momentum.RSIIndicator(close=close, window=period, fillna=False).rsi()

        if rsi.isna().iloc[-lookback - 1:].any():
            return self.hold_signal("RSI insuficiente", timestamp)

        rsi_now = float(rsi.iloc[-1])
        rsi_prev = float(rsi.iloc[-lookback - 1])

        crossed_up = rsi_prev < pivot <= rsi_now
        crossed_down = rsi_prev > pivot >= rsi_now

        magnitude = abs(rsi_now - pivot)

        if not (crossed_up or crossed_down):
            return self.hold_signal(
                f"RSI {rsi_now:.1f} — sem cruzamento de {pivot:.0f} nos últimos {lookback}c",
                timestamp,
            )

        if magnitude < min_mag:
            return self.hold_signal(
                f"RSI cruzou {pivot:.0f} mas magnitude {magnitude:.1f} < {min_mag:.1f}",
                timestamp,
            )

        signal_type = SignalType.BUY if crossed_up else SignalType.SELL
        direction = "↑" if crossed_up else "↓"

        # Confiança cresce com magnitude do cruzamento
        confidence = min(
            0.85,
            self.get_param("confidence_base") + min(0.20, (magnitude - min_mag) * 0.015),
        )

        return Signal(
            signal_type=signal_type,
            price=float(close.iloc[-1]),
            confidence=confidence,
            strategy=self.name,
            reason=(
                f"RSI {direction} {pivot:.0f}: {rsi_prev:.1f}→{rsi_now:.1f} "
                f"(mag={magnitude:.1f})"
            ),
            timestamp=timestamp,
            metadata={
                "rsi_now": round(rsi_now, 2),
                "rsi_prev": round(rsi_prev, 2),
                "pivot": pivot,
                "magnitude": round(magnitude, 2),
            },
        )
