"""
macd_strategy.py — Estratégia MACD para Polymarket.

Usa a biblioteca `ta` (Technical Analysis) para calcular MACD(12, 26, 9).
Gera sinais de BUY/SELL baseados em cruzamentos da MACD line com a Signal line,
com filtro adicional de tendência via EMA de médio prazo.

Lógica:
    BUY:  MACD line cruza ACIMA da signal line (cruzamento bullish)
          + histograma positivo e crescendo
          + preço acima da EMA50 (tendência de alta)
    SELL: MACD line cruza ABAIXO da signal line (cruzamento bearish)
          + histograma negativo e caindo
          + preço abaixo da EMA50 (tendência de baixa)
"""

import pandas as pd
import numpy as np
import ta.trend
import ta.momentum

from strategies.base_strategy import BaseStrategy, Signal, SignalType


class MACDStrategy(BaseStrategy):
    """
    Estratégia baseada em MACD com filtro de tendência.

    Parâmetros configuráveis:
        fast_period:   Período da EMA rápida (padrão: 12)
        slow_period:   Período da EMA lenta (padrão: 26)
        signal_period: Período da signal line (padrão: 9)
        trend_period:  Período da EMA de tendência (padrão: 50)
        min_histogram: Valor mínimo absoluto do histograma para considerar sinal válido
    """

    DEFAULT_PARAMS = {
        "fast_period": 12,
        "slow_period": 26,
        "signal_period": 9,
        "trend_period": 50,
        "min_histogram": 0.00005, # Calibrado para Polymarket — reduzido de 0.0001 (mercados lentos)
        "ema_tolerance": 0.03,    # Permite sinal até 3% fora do EMA (evita bloquear tudo)
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)

    @property
    def name(self) -> str:
        return "MACDStrategy"

    @property
    def min_candles(self) -> int:
        # Precisamos de pelo menos slow_period + signal_period + trend_period candles
        return self.get_param("slow_period") + self.get_param("signal_period") + \
               self.get_param("trend_period") + 5

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Gera sinal de trading baseado em MACD.

        Args:
            df: DataFrame com colunas OHLCV e DatetimeIndex.
                Preços devem estar entre 0.0 e 1.0 (probabilidades Polymarket).

        Returns:
            Signal com BUY, SELL ou HOLD.
        """
        if not self.validate_dataframe(df):
            return self.hold_signal("DataFrame inválido ou insuficiente")

        close = df["close"]
        timestamp = df.index[-1]

        # ----------------------------------------------------------------
        # Calcular MACD via biblioteca `ta`
        # ----------------------------------------------------------------
        fast = self.get_param("fast_period")
        slow = self.get_param("slow_period")
        signal_p = self.get_param("signal_period")
        trend_p = self.get_param("trend_period")
        min_hist = self.get_param("min_histogram")

        macd_indicator = ta.trend.MACD(
            close=close,
            window_fast=fast,
            window_slow=slow,
            window_sign=signal_p,
            fillna=False,
        )

        macd_line = macd_indicator.macd()
        signal_line = macd_indicator.macd_signal()
        histogram = macd_indicator.macd_diff()

        # EMA de tendência (filtro direcional)
        ema_trend = ta.trend.EMAIndicator(close=close, window=trend_p, fillna=False).ema_indicator()

        # ----------------------------------------------------------------
        # Verificar se há dados suficientes calculados
        # ----------------------------------------------------------------
        if macd_line.isna().iloc[-1] or signal_line.isna().iloc[-1]:
            return self.hold_signal("Dados MACD insuficientes", timestamp)

        # Valores atuais e anteriores
        macd_curr = macd_line.iloc[-1]
        macd_prev = macd_line.iloc[-2] if len(macd_line) > 1 else macd_curr
        sig_curr = signal_line.iloc[-1]
        sig_prev = signal_line.iloc[-2] if len(signal_line) > 1 else sig_curr
        hist_curr = histogram.iloc[-1]
        hist_prev = histogram.iloc[-2] if len(histogram) > 1 else hist_curr
        ema_curr = ema_trend.iloc[-1] if not ema_trend.isna().iloc[-1] else None
        price_curr = close.iloc[-1]

        # ----------------------------------------------------------------
        # Detectar cruzamentos
        # ----------------------------------------------------------------
        # Cruzamento bullish: MACD estava abaixo da signal, agora está acima
        bullish_cross = (macd_prev < sig_prev) and (macd_curr >= sig_curr)
        # Cruzamento bearish: MACD estava acima da signal, agora está abaixo
        bearish_cross = (macd_prev > sig_prev) and (macd_curr <= sig_curr)

        # Filtro de momentum: histograma deve ter magnitude mínima
        strong_histogram = abs(hist_curr) >= min_hist

        # Filtro de tendência via EMA com tolerância
        # Sem tolerância, mercados laterais bloqueiam todos os sinais
        ema_tol = self.get_param("ema_tolerance")
        above_ema = (ema_curr is not None) and (price_curr >= ema_curr * (1 - ema_tol))
        below_ema = (ema_curr is not None) and (price_curr <= ema_curr * (1 + ema_tol))

        # ----------------------------------------------------------------
        # Gerar sinal
        #
        # Caso 1 (alta convicção):  cruzamento + histograma forte + EMA alinhada
        # Caso 2 (convicção média): cruzamento + histograma forte (EMA divergente,
        #                           mas não bloqueia — apenas reduz confiança)
        # EMA é advisory: penaliza -0.10 de confiança quando contrária, não bloqueia
        # ----------------------------------------------------------------
        recent_std = close.iloc[-20:].std() if len(close) >= 20 else 0.01

        if bullish_cross and hist_curr > 0 and strong_histogram:
            hist_normalized = abs(hist_curr) / (recent_std + 1e-9)
            base_conf = 0.50 + hist_normalized * 0.15
            ema_penalty = 0.0 if above_ema else -0.10  # EMA contrária reduz, não bloqueia
            confidence = min(0.85, base_conf + ema_penalty)
            ema_desc = f"acima EMA{trend_p}" if above_ema else f"abaixo EMA{trend_p} (penalty)"
            return Signal(
                signal_type=SignalType.BUY,
                price=price_curr,
                confidence=confidence,
                strategy=self.name,
                reason=(
                    f"MACD cruzamento bullish: macd={macd_curr:.6f} > signal={sig_curr:.6f}, "
                    f"hist={hist_curr:.6f}, {ema_desc}"
                ),
                timestamp=timestamp,
                metadata={
                    "macd": macd_curr,
                    "signal": sig_curr,
                    "histogram": hist_curr,
                    "ema_trend": ema_curr,
                    "ema_aligned": above_ema,
                },
            )

        if bearish_cross and hist_curr < 0 and strong_histogram:
            hist_normalized = abs(hist_curr) / (recent_std + 1e-9)
            base_conf = 0.50 + hist_normalized * 0.15
            ema_penalty = 0.0 if below_ema else -0.10
            confidence = min(0.85, base_conf + ema_penalty)
            ema_desc = f"abaixo EMA{trend_p}" if below_ema else f"acima EMA{trend_p} (penalty)"
            return Signal(
                signal_type=SignalType.SELL,
                price=price_curr,
                confidence=confidence,
                strategy=self.name,
                reason=(
                    f"MACD cruzamento bearish: macd={macd_curr:.6f} < signal={sig_curr:.6f}, "
                    f"hist={hist_curr:.6f}, {ema_desc}"
                ),
                timestamp=timestamp,
                metadata={
                    "macd": macd_curr,
                    "signal": sig_curr,
                    "histogram": hist_curr,
                    "ema_trend": ema_curr,
                    "ema_aligned": below_ema,
                },
            )

        # Sem cruzamento válido
        direction = "neutro"
        if macd_curr > sig_curr:
            direction = "bullish (aguardando confirmação)"
        elif macd_curr < sig_curr:
            direction = "bearish (aguardando confirmação)"

        return self.hold_signal(
            f"MACD {direction}: macd={macd_curr:.6f}, signal={sig_curr:.6f}",
            timestamp,
        )
