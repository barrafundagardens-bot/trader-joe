"""
cvd_strategy.py — Estratégia CVD (Cumulative Volume Delta) para Polymarket.

Lógica 100% custom em pandas — sem dependência de bibliotecas externas de indicadores.

O CVD mede o fluxo de ordens inteligentes (buy pressure vs sell pressure):
    Volume Delta = volume × +1 (candle de alta) ou volume × -1 (candle de baixa)
    CVD = soma cumulativa dos Volume Deltas

Interpretação:
    CVD subindo  → pressão compradora crescente → tendência de alta
    CVD caindo   → pressão vendedora crescente → tendência de baixa
    Divergência CVD/preço → sinal de reversão potencial

Lógica de sinal:
    BUY:  CVD cruza acima de zero + CVD em tendência de alta (EMA do CVD sobe)
          + preço não divergindo negativamente do CVD
    SELL: CVD cruza abaixo de zero + CVD em tendência de baixa (EMA do CVD cai)
          + preço não divergindo positivamente do CVD
"""

import pandas as pd
import numpy as np

from strategies.base_strategy import BaseStrategy, Signal, SignalType


class CVDStrategy(BaseStrategy):
    """
    Estratégia Cumulative Volume Delta (CVD) — lógica custom em pandas.

    Parâmetros configuráveis:
        cvd_ema_period:    Período da EMA aplicada ao CVD (padrão: 14)
        lookback:          Janela para calcular CVD acumulado (padrão: 50)
        momentum_period:   Período para calcular taxa de variação do CVD (padrão: 5)
        divergence_check:  Se True, verifica divergência CVD/preço (padrão: True)
    """

    DEFAULT_PARAMS = {
        "cvd_ema_period": 14,
        "lookback": 50,
        "momentum_period": 5,
        "divergence_check": True,
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)

    @property
    def name(self) -> str:
        return "CVDStrategy"

    @property
    def min_candles(self) -> int:
        return self.get_param("lookback") + self.get_param("cvd_ema_period") + 10

    @staticmethod
    def _calculate_volume_delta(df: pd.DataFrame) -> pd.Series:
        """
        Calcula o Volume Delta candle a candle.

        Candle de alta (close >= open): volume delta positivo
        Candle de baixa (close < open): volume delta negativo

        Para uma estimativa mais precisa, usa o body ratio:
            delta = volume × (close - open) / (high - low + 1e-10)
        Isso atribui peso maior a candles com body grande em relação ao range.

        Args:
            df: DataFrame OHLCV.

        Returns:
            pd.Series com o volume delta de cada candle.
        """
        body = df["close"] - df["open"]
        candle_range = (df["high"] - df["low"]).replace(0, np.nan)

        # Ratio do body em relação ao range total (0.0 a 1.0)
        body_ratio = body / candle_range

        # Volume delta: volume ponderado pelo body ratio
        # Se candle de alta: positivo; se baixa: negativo
        volume_delta = df["volume"] * body_ratio.fillna(np.sign(body).replace(0, 0))

        return volume_delta

    @staticmethod
    def _calculate_cvd(volume_delta: pd.Series, lookback: int) -> pd.Series:
        """
        Calcula o CVD como soma cumulativa rolling dos Volume Deltas.

        Usa uma janela deslizante de `lookback` candles para evitar
        que dados muito antigos distorçam o sinal atual.

        Args:
            volume_delta: Serie com o volume delta por candle.
            lookback:     Janela de acumulação.

        Returns:
            pd.Series com o CVD normalizado.
        """
        cvd = volume_delta.rolling(window=lookback, min_periods=lookback // 2).sum()
        return cvd

    def _calculate_cvd_ema(self, cvd: pd.Series) -> pd.Series:
        """
        Aplica EMA ao CVD para suavizar o sinal.

        Args:
            cvd: Serie CVD.

        Returns:
            pd.Series com a EMA do CVD.
        """
        period = self.get_param("cvd_ema_period")
        return cvd.ewm(span=period, adjust=False).mean()

    def _detect_divergence(
        self,
        price: pd.Series,
        cvd: pd.Series,
        lookback: int = 5,
    ) -> str:
        """
        Detecta divergência entre preço e CVD.

        Divergência bearish (sinal de venda): preço subindo, CVD caindo
        Divergência bullish (sinal de compra): preço caindo, CVD subindo

        Args:
            price:   Serie de preços (close).
            cvd:     Serie CVD.
            lookback: Número de candles para comparar.

        Returns:
            'bullish', 'bearish' ou 'none'
        """
        if len(price) < lookback + 1 or len(cvd) < lookback + 1:
            return "none"

        price_change = price.iloc[-1] - price.iloc[-(lookback + 1)]
        cvd_change = cvd.iloc[-1] - cvd.iloc[-(lookback + 1)]

        # Exige magnitude mínima para evitar divergências de ruído:
        # Preço deve ter movido >= 1% e CVD deve ter mudado >= 30% do seu range recente
        price_ref = abs(price.iloc[-(lookback + 1)])
        price_pct = abs(price_change) / price_ref if price_ref > 0 else 0
        cvd_range = cvd.iloc[-lookback:].max() - cvd.iloc[-lookback:].min()
        cvd_pct = abs(cvd_change) / (cvd_range + 1e-10)

        if price_pct < 0.01 or cvd_pct < 0.30:
            return "none"

        if price_change < 0 and cvd_change > 0:
            return "bullish"  # Preço caiu mas CVD subiu → força compradora oculta
        elif price_change > 0 and cvd_change < 0:
            return "bearish"  # Preço subiu mas CVD caiu → fraqueza compradora
        return "none"

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Gera sinal de trading baseado em CVD.

        Args:
            df: DataFrame com colunas OHLCV e DatetimeIndex.

        Returns:
            Signal com BUY, SELL ou HOLD.
        """
        if not self.validate_dataframe(df):
            return self.hold_signal("DataFrame inválido ou insuficiente")

        timestamp = df.index[-1]
        lookback = self.get_param("lookback")
        momentum_p = self.get_param("momentum_period")
        check_div = self.get_param("divergence_check")

        # ----------------------------------------------------------------
        # Calcular CVD
        # ----------------------------------------------------------------
        volume_delta = self._calculate_volume_delta(df)
        cvd = self._calculate_cvd(volume_delta, lookback)
        cvd_ema = self._calculate_cvd_ema(cvd)

        if cvd.isna().iloc[-1] or cvd_ema.isna().iloc[-1]:
            return self.hold_signal("CVD insuficiente", timestamp)

        cvd_curr = cvd.iloc[-1]
        cvd_prev = cvd.iloc[-2] if len(cvd) > 1 else cvd_curr
        cvd_ema_curr = cvd_ema.iloc[-1]
        cvd_ema_prev = cvd_ema.iloc[-2] if len(cvd_ema) > 1 else cvd_ema_curr
        price_curr = df["close"].iloc[-1]

        # ----------------------------------------------------------------
        # Cruzamento do CVD acima/abaixo de zero
        # ----------------------------------------------------------------
        cvd_crossed_up = (cvd_prev <= 0) and (cvd_curr > 0)
        cvd_crossed_down = (cvd_prev >= 0) and (cvd_curr < 0)

        # EMA do CVD em tendência (sinal de confirmação)
        cvd_ema_rising = cvd_ema_curr > cvd_ema_prev
        cvd_ema_falling = cvd_ema_curr < cvd_ema_prev

        # Momentum do CVD (taxa de variação)
        if len(cvd) > momentum_p:
            cvd_momentum = cvd.iloc[-1] - cvd.iloc[-(momentum_p + 1)]
        else:
            cvd_momentum = 0.0

        # Divergência CVD/preço
        divergence = "none"
        if check_div and len(df) > momentum_p:
            divergence = self._detect_divergence(df["close"], cvd, momentum_p)

        # ----------------------------------------------------------------
        # Gerar sinal
        #
        # Caso 1 (alta convicção): crossover + EMA confirmando + momentum
        # Caso 2 (convicção média): crossover + momentum (EMA ainda lagging ok)
        # ----------------------------------------------------------------

        # --- BUY Caso 1: todas as 3 confirmações ---
        if cvd_crossed_up and cvd_ema_rising and cvd_momentum > 0:
            base_conf = 0.70 if divergence != "bearish" else 0.45
            div_bonus = 0.15 if divergence == "bullish" else 0.0
            confidence = min(0.90, base_conf + div_bonus)
            return Signal(
                signal_type=SignalType.BUY,
                price=price_curr,
                confidence=confidence,
                strategy=self.name,
                reason=(
                    f"CVD cruzou zero ↑: {cvd_prev:.2f}→{cvd_curr:.2f}, "
                    f"EMA↑ ({cvd_ema_prev:.2f}→{cvd_ema_curr:.2f}), "
                    f"momentum={cvd_momentum:.2f} [caso 1]"
                    + (f", div={divergence}" if divergence != "none" else "")
                ),
                timestamp=timestamp,
                metadata={"cvd": cvd_curr, "cvd_ema": cvd_ema_curr,
                           "cvd_momentum": cvd_momentum, "divergence": divergence, "signal_case": 1},
            )

        # --- BUY Caso 2: crossover + momentum (EMA ainda lagging) ---
        if cvd_crossed_up and cvd_momentum > 0 and divergence != "bearish":
            confidence = min(0.75, 0.55 + (0.10 if divergence == "bullish" else 0.0))
            return Signal(
                signal_type=SignalType.BUY,
                price=price_curr,
                confidence=confidence,
                strategy=self.name,
                reason=(
                    f"CVD cruzou zero ↑: {cvd_prev:.2f}→{cvd_curr:.2f}, "
                    f"momentum={cvd_momentum:.2f} (EMA lagging) [caso 2]"
                    + (f", div={divergence}" if divergence != "none" else "")
                ),
                timestamp=timestamp,
                metadata={"cvd": cvd_curr, "cvd_ema": cvd_ema_curr,
                           "cvd_momentum": cvd_momentum, "divergence": divergence, "signal_case": 2},
            )

        # --- SELL Caso 1: todas as 3 confirmações ---
        if cvd_crossed_down and cvd_ema_falling and cvd_momentum < 0:
            base_conf = 0.70 if divergence != "bullish" else 0.45
            div_bonus = 0.15 if divergence == "bearish" else 0.0
            confidence = min(0.90, base_conf + div_bonus)
            return Signal(
                signal_type=SignalType.SELL,
                price=price_curr,
                confidence=confidence,
                strategy=self.name,
                reason=(
                    f"CVD cruzou zero ↓: {cvd_prev:.2f}→{cvd_curr:.2f}, "
                    f"EMA↓ ({cvd_ema_prev:.2f}→{cvd_ema_curr:.2f}), "
                    f"momentum={cvd_momentum:.2f} [caso 1]"
                    + (f", div={divergence}" if divergence != "none" else "")
                ),
                timestamp=timestamp,
                metadata={"cvd": cvd_curr, "cvd_ema": cvd_ema_curr,
                           "cvd_momentum": cvd_momentum, "divergence": divergence, "signal_case": 1},
            )

        # --- SELL Caso 2: crossover + momentum (EMA ainda lagging) ---
        if cvd_crossed_down and cvd_momentum < 0 and divergence != "bullish":
            confidence = min(0.75, 0.55 + (0.10 if divergence == "bearish" else 0.0))
            return Signal(
                signal_type=SignalType.SELL,
                price=price_curr,
                confidence=confidence,
                strategy=self.name,
                reason=(
                    f"CVD cruzou zero ↓: {cvd_prev:.2f}→{cvd_curr:.2f}, "
                    f"momentum={cvd_momentum:.2f} (EMA lagging) [caso 2]"
                    + (f", div={divergence}" if divergence != "none" else "")
                ),
                timestamp=timestamp,
                metadata={"cvd": cvd_curr, "cvd_ema": cvd_ema_curr,
                           "cvd_momentum": cvd_momentum, "divergence": divergence, "signal_case": 2},
            )

        cvd_zone = "positivo" if cvd_curr > 0 else "negativo"
        return self.hold_signal(
            f"CVD={cvd_curr:.2f} ({cvd_zone}), aguardando cruzamento de zero",
            timestamp,
        )
