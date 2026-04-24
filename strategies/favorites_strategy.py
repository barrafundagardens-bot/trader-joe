"""
favorites_strategy.py — Estratégia "Comprar Favoritos" para Polymarket.

Lógica central:
    Mercados de predição sofrem do "longshot bias" — participantes pagam
    demais por chances remotas e pagam de menos por eventos quase certos.

    Resultado documentado: contratos acima de 0.90 são sistematicamente
    subvalorizados. Comprar e segurar até a resolução gera win rate de 90%+.

Regra operacional:
    BUY:  preço atual >= threshold (padrão: 0.92)
          mercado tem dados suficientes (tendência confirmada de alta)
    HOLD: preço abaixo do threshold ou oscilando

Sinal de saída (para o paper trading):
    O trade fecha quando:
        - Preço sobe para >= exit_profit (0.97) → realiza lucro cedo
        - Preço cai para <= exit_stop (0.85)    → stop loss
        - Mercado resolve (expiração)            → lucro máximo

Diferença vs RSI:
    RSI tenta prever reversões. Esta estratégia não prevê nada —
    apenas captura o prêmio residual de mercados que já decidiram.
"""

import pandas as pd
import numpy as np

from strategies.base_strategy import BaseStrategy, Signal, SignalType


class FavoritesStrategy(BaseStrategy):
    """
    Estratégia de compra de favoritos em mercados de predição.

    Parâmetros configuráveis:
        buy_threshold:    Preço mínimo para considerar favorito (padrão: 0.92)
        exit_profit:      Preço alvo para saída com lucro (padrão: 0.97)
        exit_stop:        Preço de stop loss (padrão: 0.85)
        min_trend_candles: Candles acima do threshold para confirmar (padrão: 3)
        confirmation_pct: % dos últimos candles que devem estar acima do threshold
    """

    DEFAULT_PARAMS = {
        "buy_threshold": 0.92,      # Compra acima deste preço
        "exit_profit": 0.97,        # Realiza lucro se atingir este preço
        "exit_stop": 0.85,          # Stop loss se cair aqui
        "min_trend_candles": 3,     # Mínimo de candles acima do threshold
        "confirmation_pct": 0.80,   # 80% dos últimos candles acima do threshold
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)

    @property
    def name(self) -> str:
        return "FavoritesStrategy"

    @property
    def min_candles(self) -> int:
        return self.get_param("min_trend_candles") + 5

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Gera sinal baseado em preço próximo à resolução.

        Args:
            df: DataFrame OHLCV com DatetimeIndex.

        Returns:
            Signal BUY quando preço está consistentemente alto,
            HOLD caso contrário.
        """
        if not self.validate_dataframe(df):
            return self.hold_signal("DataFrame inválido ou insuficiente")

        close = df["close"]
        timestamp = df.index[-1]

        buy_threshold = self.get_param("buy_threshold")
        exit_profit = self.get_param("exit_profit")
        exit_stop = self.get_param("exit_stop")
        min_trend = self.get_param("min_trend_candles")
        confirm_pct = self.get_param("confirmation_pct")

        price_curr = close.iloc[-1]
        price_prev = close.iloc[-2]

        # ----------------------------------------------------------------
        # Verificar se o preço está na zona de favorito
        # ----------------------------------------------------------------
        if price_curr < buy_threshold:
            return self.hold_signal(
                f"Preço {price_curr:.3f} abaixo do threshold {buy_threshold}",
                timestamp,
            )

        # ----------------------------------------------------------------
        # Confirmação: os últimos candles também estavam acima do threshold?
        # Evita entrar em spikes momentâneos
        # ----------------------------------------------------------------
        lookback = min(min_trend + 5, len(close))
        recent = close.iloc[-lookback:]
        pct_above = (recent >= buy_threshold).mean()

        if pct_above < confirm_pct:
            return self.hold_signal(
                f"Preço {price_curr:.3f} acima do threshold mas instável "
                f"({pct_above*100:.0f}% dos últimos {lookback} candles confirmam)",
                timestamp,
            )

        # ----------------------------------------------------------------
        # Confirmar que o preço está subindo (não em queda livre para 1.0)
        # Um mercado caindo de 0.99 para 0.92 pode ser ruído, não oportunidade
        # ----------------------------------------------------------------
        trend_up = price_curr >= price_prev
        price_5ago = close.iloc[-min(6, len(close))]
        recovering = price_curr > price_5ago  # Está subindo nos últimos candles

        # ----------------------------------------------------------------
        # Calcular potencial de lucro
        # ----------------------------------------------------------------
        upside = exit_profit - price_curr      # Quanto pode ganhar
        downside = price_curr - exit_stop      # Quanto pode perder
        rr = upside / downside if downside > 0 else 99.0

        # ----------------------------------------------------------------
        # Gerar sinal
        # ----------------------------------------------------------------
        # Alta convicção: acima de 0.95
        if price_curr >= 0.95:
            confidence = 0.90
            conviction = "muito alta"
        # Convicção padrão: entre threshold e 0.95
        elif price_curr >= buy_threshold:
            # Escala linear entre 0.65 e 0.85 dependendo do preço
            confidence = 0.65 + ((price_curr - buy_threshold) / (0.95 - buy_threshold)) * 0.20
            conviction = "alta"
        else:
            confidence = 0.55
            conviction = "moderada"

        # Bônus de confiança se preço está subindo
        if trend_up and recovering:
            confidence = min(0.95, confidence + 0.05)

        reason = (
            f"Favorito: preço={price_curr:.3f} (threshold={buy_threshold}) | "
            f"convicção={conviction} | {pct_above*100:.0f}% dos últimos {lookback} "
            f"candles confirmam | upside={upside:.3f} downside={downside:.3f} R/R={rr:.1f}x"
        )

        return Signal(
            signal_type=SignalType.BUY,
            price=price_curr,
            confidence=confidence,
            strategy=self.name,
            reason=reason,
            timestamp=timestamp,
            metadata={
                "price": price_curr,
                "buy_threshold": buy_threshold,
                "exit_profit": exit_profit,
                "exit_stop": exit_stop,
                "pct_above_threshold": round(pct_above, 3),
                "upside": round(upside, 4),
                "downside": round(downside, 4),
                "rr": round(rr, 2),
                "conviction": conviction,
            },
        )
