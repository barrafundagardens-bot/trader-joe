"""
rsi_strategy.py — Estratégia RSI com Filtro de Tendência para Polymarket.

Usa a biblioteca `ta` para calcular RSI(14) + EMA para detecção de tendência.

Lógica base:
    BUY:  RSI sai da região oversold (< 30) cruzando de volta acima de 30
          + candle de alta confirmando reversão
    SELL: RSI sai da região overbought (> 70) cruzando de volta abaixo de 70
          + candle de baixa confirmando reversão

Filtro de Tendência (adicionado após análise multi-mercado):
    Descoberta empírica: RSI gera edge APENAS em mercados com tendência clara.
    Em mercados laterais, o RSI opera em ruído e tem win rate de ~6-40%.
    Em mercados tendendo, win rate sobe para 51%+ (ex: Edmonton Oilers).

    Regra:
        EMA-30 subindo  → só aceita sinais BUY  (pullback em uptrend)
        EMA-30 caindo   → só aceita sinais SELL (pullback em downtrend)
        EMA-30 lateral  → HOLD (mercado sem direção, não entrar)

    O slope da EMA é medido como variação % por candle nos últimos N períodos.
    Um threshold mínimo filtra mercados que estão essentially flat.
"""

import pandas as pd
import numpy as np
import ta.momentum
import ta.trend

from strategies.base_strategy import BaseStrategy, Signal, SignalType


class RSIStrategy(BaseStrategy):
    """
    Estratégia baseada em RSI com filtro de volume.

    Parâmetros configuráveis:
        rsi_period:     Período do RSI (padrão: 14)
        oversold:       Limite de oversold (padrão: 30)
        overbought:     Limite de overbought (padrão: 70)
        volume_period:  Período da média de volume para filtro (padrão: 20)
        volume_factor:  Multiplicador mínimo do volume vs média (padrão: 1.2)
        extreme_rsi:    RSI mínimo para sinal de alta convicção (padrão: 20 para buy, 80 para sell)
    """

    DEFAULT_PARAMS = {
        "rsi_period": 14,
        "oversold": 30,
        "overbought": 70,
        "volume_period": 20,
        "volume_factor": 1.0,       # Ajustado para Polymarket (volume irregular)
        "extreme_oversold": 20,
        "extreme_overbought": 80,
        # --- Filtro de Tendência ---
        "trend_ema_period": 30,     # EMA para detectar direção do mercado
        "trend_slope_period": 20,   # Candles para medir inclinação (20 candles = 5h)
        "trend_slope_min": 0.00005, # Inclinação mínima por candle (0.005%/candle)
                                    # Reduzido de 0.00015 — mercados de predição são lentos
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)

    @property
    def name(self) -> str:
        return "RSIStrategy"

    @property
    def min_candles(self) -> int:
        return max(
            self.get_param("rsi_period"),
            self.get_param("volume_period"),
            self.get_param("trend_ema_period"),
        ) + 10

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Gera sinal de trading baseado em RSI.

        Args:
            df: DataFrame com colunas OHLCV e DatetimeIndex.

        Returns:
            Signal com BUY, SELL ou HOLD.
        """
        if not self.validate_dataframe(df):
            return self.hold_signal("DataFrame inválido ou insuficiente")

        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]
        timestamp = df.index[-1]

        # ----------------------------------------------------------------
        # Calcular RSI via biblioteca `ta`
        # ----------------------------------------------------------------
        rsi_period = self.get_param("rsi_period")
        oversold = self.get_param("oversold")
        overbought = self.get_param("overbought")
        vol_period = self.get_param("volume_period")
        vol_factor = self.get_param("volume_factor")
        extreme_os = self.get_param("extreme_oversold")
        extreme_ob = self.get_param("extreme_overbought")
        trend_period = self.get_param("trend_ema_period")
        slope_period = self.get_param("trend_slope_period")
        slope_min = self.get_param("trend_slope_min")

        rsi_indicator = ta.momentum.RSIIndicator(
            close=close,
            window=rsi_period,
            fillna=False,
        )
        rsi = rsi_indicator.rsi()

        if rsi.isna().iloc[-1] or rsi.isna().iloc[-2]:
            return self.hold_signal("RSI insuficiente", timestamp)

        # ----------------------------------------------------------------
        # Filtro de Tendência via EMA-30
        # Só opera na direção da tendência — elimina mercados laterais
        # ----------------------------------------------------------------
        ema = ta.trend.EMAIndicator(
            close=close, window=trend_period, fillna=False
        ).ema_indicator()

        if ema.isna().iloc[-1] or ema.isna().iloc[-slope_period]:
            return self.hold_signal("EMA de tendência insuficiente", timestamp)

        ema_now = ema.iloc[-1]
        ema_past = ema.iloc[-slope_period]

        # Slope normalizado: variação % por candle
        slope = (ema_now - ema_past) / (ema_past * slope_period) if ema_past > 0 else 0.0

        uptrend = slope > slope_min        # EMA subindo com força suficiente
        downtrend = slope < -slope_min    # EMA caindo com força suficiente
        not_accelerating_down = slope > -0.05   # Queda não é freefall (< -5%/candle normalizado)
        not_accelerating_up = slope < 0.05      # Alta não é parabólica
        # lateral = abs(slope) <= slope_min → não entra pelo caso 1, mas pode entrar pelo caso 2

        # ----------------------------------------------------------------
        # Filtro de volume
        # ----------------------------------------------------------------
        vol_ma = volume.rolling(window=vol_period).mean()
        volume_curr = volume.iloc[-1]
        vol_ma_curr = vol_ma.iloc[-1]

        # Volume acima da média × fator mínimo
        volume_confirmed = (
            not pd.isna(vol_ma_curr) and
            vol_ma_curr > 0 and
            volume_curr >= vol_ma_curr * vol_factor
        )

        # ----------------------------------------------------------------
        # Valores atuais
        # ----------------------------------------------------------------
        rsi_curr = rsi.iloc[-1]
        rsi_prev = rsi.iloc[-2]
        price_curr = close.iloc[-1]
        price_prev = close.iloc[-2]

        # Candle de alta/baixa (confirmação direcional)
        bullish_candle = price_curr > price_prev
        bearish_candle = price_curr < price_prev

        # ----------------------------------------------------------------
        # Cruzamentos RSI
        # ----------------------------------------------------------------
        # BUY: RSI estava oversold e cruza de volta acima do limite
        exiting_oversold = (rsi_prev <= oversold) and (rsi_curr > oversold)
        # SELL: RSI estava overbought e cruza de volta abaixo do limite
        exiting_overbought = (rsi_prev >= overbought) and (rsi_curr < overbought)

        # Sinais de alta convicção (RSI extremo nos últimos 5 candles)
        # Janela de 5 captura casos onde RSI tocou fundo até 4 candles antes do crossover
        rsi_window = rsi.iloc[-6:-1]  # 5 candles anteriores ao atual
        deep_oversold = bool(rsi_window.min() <= extreme_os)
        deep_overbought = bool(rsi_window.max() >= extreme_ob)

        # ----------------------------------------------------------------
        # Gerar sinal
        # Caso 1: Pullback em tendência — RSI oversold/overbought durante
        #         uma tendência clara. Alta convicção.
        # Caso 2: Reversão de fundo — RSI em nível extremo (deep) saindo
        #         de oversold/overbought sem tendência acelerada contra.
        #         Convicção menor (mercado sem direção definida).
        # ----------------------------------------------------------------

        # --- Caso 1: Pullback em uptrend ---
        if exiting_oversold and bullish_candle and uptrend:
            base_confidence = 0.75 if deep_oversold else 0.55
            vol_bonus = 0.10 if volume_confirmed else 0.0
            confidence = min(0.95, base_confidence + vol_bonus)
            reason_parts = [
                f"RSI saindo de oversold: {rsi_prev:.1f} → {rsi_curr:.1f}",
                f"(limite={oversold})",
            ]
            if deep_oversold:
                reason_parts.append(f"[nível extremo <{extreme_os}]")
            if volume_confirmed:
                reason_parts.append(f"[volume={volume_curr:.0f} > {vol_factor}x média]")
            reason_parts.append(f"[pullback uptrend slope={slope:+.5f}]")
            return Signal(
                signal_type=SignalType.BUY,
                price=price_curr,
                confidence=confidence,
                strategy=self.name,
                reason=" ".join(reason_parts),
                timestamp=timestamp,
                metadata={
                    "rsi": rsi_curr, "rsi_prev": rsi_prev,
                    "volume_ratio": (volume_curr / vol_ma_curr) if vol_ma_curr > 0 else 0,
                    "ema_slope": round(slope, 6), "trend": "up", "signal_case": 1,
                },
            )

        # --- Caso 2: Reversão de fundo (RSI extremo + queda não acelerando) ---
        if exiting_oversold and bullish_candle and deep_oversold and not_accelerating_down:
            vol_bonus = 0.10 if volume_confirmed else 0.0
            confidence = min(0.85, 0.55 + vol_bonus)  # Convicção menor que caso 1
            reason_parts = [
                f"RSI reversão de fundo: {rsi_prev:.1f} → {rsi_curr:.1f}",
                f"[nível extremo <{extreme_os}]",
                f"[slope={slope:+.5f} não acelerando]",
            ]
            if volume_confirmed:
                reason_parts.append(f"[volume={volume_curr:.0f} > {vol_factor}x média]")
            return Signal(
                signal_type=SignalType.BUY,
                price=price_curr,
                confidence=confidence,
                strategy=self.name,
                reason=" ".join(reason_parts),
                timestamp=timestamp,
                metadata={
                    "rsi": rsi_curr, "rsi_prev": rsi_prev,
                    "volume_ratio": (volume_curr / vol_ma_curr) if vol_ma_curr > 0 else 0,
                    "ema_slope": round(slope, 6), "trend": "reversal", "signal_case": 2,
                },
            )

        # --- Caso 1: Pullback em downtrend ---
        if exiting_overbought and bearish_candle and downtrend:
            base_confidence = 0.75 if deep_overbought else 0.55
            vol_bonus = 0.10 if volume_confirmed else 0.0
            confidence = min(0.95, base_confidence + vol_bonus)
            reason_parts = [
                f"RSI saindo de overbought: {rsi_prev:.1f} → {rsi_curr:.1f}",
                f"(limite={overbought})",
            ]
            if deep_overbought:
                reason_parts.append(f"[nível extremo >{extreme_ob}]")
            if volume_confirmed:
                reason_parts.append(f"[volume={volume_curr:.0f} > {vol_factor}x média]")
            reason_parts.append(f"[pullback downtrend slope={slope:+.5f}]")
            return Signal(
                signal_type=SignalType.SELL,
                price=price_curr,
                confidence=confidence,
                strategy=self.name,
                reason=" ".join(reason_parts),
                timestamp=timestamp,
                metadata={
                    "rsi": rsi_curr, "rsi_prev": rsi_prev,
                    "volume_ratio": (volume_curr / vol_ma_curr) if vol_ma_curr > 0 else 0,
                    "ema_slope": round(slope, 6), "trend": "down", "signal_case": 1,
                },
            )

        # --- Caso 2: Topo de reversão (RSI extremo + alta não acelerando) ---
        if exiting_overbought and bearish_candle and deep_overbought and not_accelerating_up:
            vol_bonus = 0.10 if volume_confirmed else 0.0
            confidence = min(0.85, 0.55 + vol_bonus)
            reason_parts = [
                f"RSI reversão de topo: {rsi_prev:.1f} → {rsi_curr:.1f}",
                f"[nível extremo >{extreme_ob}]",
                f"[slope={slope:+.5f} não acelerando]",
            ]
            if volume_confirmed:
                reason_parts.append(f"[volume={volume_curr:.0f} > {vol_factor}x média]")
            return Signal(
                signal_type=SignalType.SELL,
                price=price_curr,
                confidence=confidence,
                strategy=self.name,
                reason=" ".join(reason_parts),
                timestamp=timestamp,
                metadata={
                    "rsi": rsi_curr, "rsi_prev": rsi_prev,
                    "volume_ratio": (volume_curr / vol_ma_curr) if vol_ma_curr > 0 else 0,
                    "ema_slope": round(slope, 6), "trend": "reversal", "signal_case": 2,
                },
            )

        # Aguardando condição
        if rsi_curr <= oversold:
            zone = f"oversold (RSI={rsi_curr:.1f})"
        elif rsi_curr >= overbought:
            zone = f"overbought (RSI={rsi_curr:.1f})"
        else:
            zone = f"neutro (RSI={rsi_curr:.1f})"

        if uptrend:
            trend_desc = f"uptrend (slope={slope:+.5f})"
        elif downtrend:
            trend_desc = f"downtrend (slope={slope:+.5f})"
        else:
            trend_desc = f"lateral (slope={slope:+.5f}, min={slope_min})"

        return self.hold_signal(f"RSI em zona {zone} | trend: {trend_desc}", timestamp)
