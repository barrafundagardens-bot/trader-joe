"""
strategies/btc_spike_strategy.py — Detector de spike de preço BTC.

Detecta movimentos bruscos no preço do BTC nos últimos N candles 1m via
z-score do retorno acumulado. Spike bullish → BUY (entrar em UP); spike
bearish → SELL (entrar em DOWN).

Pensado para rodar sobre candles 1m da Binance, não sobre odds Polymarket
(volume sintético do Polymarket não serve para detectar pressão real).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.base_strategy import BaseStrategy, Signal, SignalType


class BTCSpikeStrategy(BaseStrategy):
    """
    Detecta spikes de preço via z-score de retornos.

    Parâmetros:
        spike_window:    Quantos candles 1m considerar como "agora" (padrão: 3)
        baseline_window: Janela para média/std de retornos (padrão: 30)
        zscore_threshold: |z| mínimo para gerar sinal (padrão: 2.0)
        volume_factor:   Razão volume_atual / vol_medio mínima (padrão: 1.5)
    """

    DEFAULT_PARAMS = {
        "spike_window": 3,
        "baseline_window": 30,
        "zscore_threshold": 2.0,
        "volume_factor": 1.5,
        "confidence_base": 0.55,
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)

    @property
    def name(self) -> str:
        return "BTCSpikeStrategy"

    @property
    def min_candles(self) -> int:
        return self.get_param("baseline_window") + self.get_param("spike_window") + 5

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        if not self.validate_dataframe(df):
            return self.hold_signal("Candles insuficientes")

        spike_w = self.get_param("spike_window")
        base_w = self.get_param("baseline_window")
        z_thr = self.get_param("zscore_threshold")
        vol_factor = self.get_param("volume_factor")

        close = df["close"]
        volume = df["volume"]
        timestamp = df.index[-1]

        returns = close.pct_change().dropna()
        if len(returns) < base_w + spike_w:
            return self.hold_signal("Retornos insuficientes", timestamp)

        baseline = returns.iloc[-(base_w + spike_w):-spike_w]
        recent_returns = returns.iloc[-spike_w:]

        baseline_std = baseline.std()
        if baseline_std == 0 or np.isnan(baseline_std):
            return self.hold_signal("Volatilidade base zero", timestamp)

        recent_cum_return = recent_returns.sum()
        zscore = recent_cum_return / (baseline_std * np.sqrt(spike_w))

        avg_volume = volume.iloc[-(base_w + spike_w):-spike_w].mean()
        recent_volume = volume.iloc[-spike_w:].mean()
        vol_ratio = recent_volume / avg_volume if avg_volume > 0 else 0.0

        if vol_ratio < vol_factor:
            return self.hold_signal(
                f"Volume {vol_ratio:.2f}x < {vol_factor}x exigido", timestamp
            )

        if abs(zscore) < z_thr:
            return self.hold_signal(
                f"Z-score {zscore:+.2f} dentro da banda ±{z_thr}", timestamp
            )

        signal_type = SignalType.BUY if zscore > 0 else SignalType.SELL
        confidence = min(
            0.90,
            self.get_param("confidence_base")
            + min(0.20, (abs(zscore) - z_thr) * 0.05)
            + min(0.15, (vol_ratio - vol_factor) * 0.05),
        )

        last_price = float(close.iloc[-1])
        cum_return_pct = recent_cum_return * 100

        return Signal(
            signal_type=signal_type,
            price=last_price,
            confidence=confidence,
            strategy=self.name,
            reason=(
                f"Spike {'↑' if zscore > 0 else '↓'} z={zscore:+.2f} "
                f"({cum_return_pct:+.2f}% em {spike_w}m, vol×{vol_ratio:.1f})"
            ),
            timestamp=timestamp,
            metadata={
                "zscore": round(float(zscore), 3),
                "cum_return_pct": round(float(cum_return_pct), 4),
                "volume_ratio": round(float(vol_ratio), 3),
                "btc_price": round(last_price, 2),
            },
        )
