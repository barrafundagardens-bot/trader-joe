"""
strategies/spread_strategy.py — Spread Farming (Market Making).

Estratégia de market making: coloca bid e ask em torno do mid-price
para colecionar o spread. Gerencia inventário para evitar exposição
direcional excessiva.

Lógica central:
    1. Calcular mid-price do último candle (close)
    2. Verificar se o spread natural do mercado é suficiente
    3. Posicionar bid em mid - half_spread e ask em mid + half_spread
    4. Simular preenchimento via range do candle (high/low)
    5. Fazer skewing do quote quando inventário está desequilibrado

Parâmetros configuráveis:
    half_spread:          Metade do spread cotado (padrão: 0.015 = 1.5%)
    min_market_spread:    Spread mínimo do mercado para cotar (padrão: 0.01)
    order_size:           Tamanho de cada ordem em USDC (padrão: 5.0)
    max_inventory:        Inventário máximo antes de parar de cotar um lado
    skew_factor:          Quanto deslocar o quote por unidade de inventário
    rebalance_threshold:  Variação de preço que força novo quote (padrão: 0.02)

Nota sobre paper trading:
    Sem acesso ao orderbook em tempo real, o preenchimento é simulado:
    - Bid preenche se candle.low <= bid_price
    - Ask preenche se candle.high >= ask_price
    Essa é uma aproximação conservadora — na prática, preenchimentos
    podem ocorrer com mais frequência em mercados líquidos.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from strategies.base_strategy import BaseStrategy, Signal, SignalType

logger = logging.getLogger(__name__)


class SpreadFarmingStrategy(BaseStrategy):
    """
    Estratégia de spread farming (market making) para Polymarket.

    Coloca ordens nos dois lados do orderbook (bid e ask) ao redor do
    mid-price para capturar o spread. Gerencia inventário via skewing:
    quando estamos muito comprados, deslocamos o quote para incentivar
    vendas; quando muito vendidos, o inverso.

    O sinal retornado reflete o lado prioritário baseado no inventário.
    O metadata sempre contém bid e ask para que o executor possa colocar
    ambas as ordens quando o inventário permitir.

    Args:
        params: Dicionário de configuração com as chaves listadas acima.
    """

    def __init__(self, params: Optional[dict] = None):
        super().__init__(params)
        # Estado interno de inventário (em USDC de exposição)
        self._inventory: float = 0.0
        # Último mid-price cotado (para detectar necessidade de requote)
        self._last_quoted_mid: Optional[float] = None
        # Preços das ordens ativas simuladas
        self._active_bid: Optional[float] = None
        self._active_ask: Optional[float] = None
        # Acumulador de PnL do spread coletado
        self._spread_pnl: float = 0.0
        # Contadores de preenchimento
        self._bid_fills: int = 0
        self._ask_fills: int = 0

    @property
    def name(self) -> str:
        return "SpreadFarming"

    @property
    def min_candles(self) -> int:
        return 10

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Gera sinal de market making baseado no candle mais recente.

        Antes de cotar, verifica se as ordens anteriores foram preenchidas
        usando o range (high/low) do candle atual como proxy do orderbook.

        Args:
            df: DataFrame OHLCV com DatetimeIndex.

        Returns:
            Signal com BUY (cotando bid), SELL (cotando ask) ou HOLD
            (spread insuficiente ou modo emergência de inventário).
        """
        if not self.validate_dataframe(df):
            return self.hold_signal("Dados insuficientes para SpreadFarming")

        candle = df.iloc[-1]
        ts = df.index[-1]

        mid = float(candle["close"])
        candle_high = float(candle["high"])
        candle_low = float(candle["low"])

        # --- 1. Checar preenchimento de ordens anteriores ---
        self._check_fills(candle_high, candle_low)

        # --- 2. Calcular spread natural do mercado ---
        # Usa a média do range dos últimos 5 candles como proxy
        lookback = df.tail(5)
        avg_range = (lookback["high"] - lookback["low"]).mean()
        market_spread_pct = avg_range / mid if mid > 0 else 0.0

        min_market_spread = self.get_param("min_market_spread", 0.01)
        if market_spread_pct < min_market_spread:
            return self.hold_signal(
                f"Spread natural do mercado insuficiente: "
                f"{market_spread_pct:.4f} < {min_market_spread:.4f}",
                timestamp=ts,
            )

        # --- 3. Calcular preços de cotação com skewing de inventário ---
        half_spread = self.get_param("half_spread", 0.015)
        skew_factor = self.get_param("skew_factor", 0.005)

        # Skewing: se inventário > 0 (longo), deslocar quotes para baixo
        # incentivando sells; se < 0 (curto), deslocar para cima.
        inventory_skew = self._inventory * skew_factor
        bid = round(mid - half_spread - inventory_skew, 4)
        ask = round(mid + half_spread - inventory_skew, 4)

        # Garantir limites válidos do Polymarket (0.01 a 0.99)
        bid = max(0.01, min(0.98, bid))
        ask = max(0.02, min(0.99, ask))

        # Garantir que bid < ask (sanidade)
        if bid >= ask:
            return self.hold_signal(
                f"Quote inválido após skewing: bid={bid:.4f} >= ask={ask:.4f}",
                timestamp=ts,
            )

        # --- 4. Verificar se é necessário requotar ---
        rebalance_threshold = self.get_param("rebalance_threshold", 0.02)
        if (
            self._last_quoted_mid is not None
            and abs(mid - self._last_quoted_mid) / self._last_quoted_mid
            < rebalance_threshold
            and self._active_bid is not None
            and self._active_ask is not None
        ):
            # Preço não se moveu o suficiente para requotar — HOLD
            return self.hold_signal(
                f"Preço estável (variação < {rebalance_threshold:.1%}): "
                f"mantendo quotes atuais bid={self._active_bid:.4f} "
                f"ask={self._active_ask:.4f}",
                timestamp=ts,
            )

        # --- 5. Decidir side prioritário baseado em inventário ---
        max_inventory = self.get_param("max_inventory", 50.0)
        order_size = self.get_param("order_size", 5.0)

        if self._inventory >= max_inventory:
            # Inventário muito longo: apenas vender
            signal_type = SignalType.SELL
            price = ask
            confidence = 0.65
            reason = (
                f"Inventário longo ({self._inventory:.1f} USDC): "
                f"cotando apenas ask @ {ask:.4f} | mid={mid:.4f}"
            )
        elif self._inventory <= -max_inventory:
            # Inventário muito curto: apenas comprar
            signal_type = SignalType.BUY
            price = bid
            confidence = 0.65
            reason = (
                f"Inventário curto ({self._inventory:.1f} USDC): "
                f"cotando apenas bid @ {bid:.4f} | mid={mid:.4f}"
            )
        else:
            # Inventário neutro: cotar os dois lados
            # Retornamos BUY (bid) como sinal principal;
            # metadata contém o ask para o executor colocar ambas as ordens
            signal_type = SignalType.BUY
            price = bid
            confidence = 0.75
            reason = (
                f"Market making: bid={bid:.4f} ask={ask:.4f} "
                f"mid={mid:.4f} spread={ask - bid:.4f} "
                f"inv={self._inventory:.1f}"
            )

        # Atualizar estado interno
        self._last_quoted_mid = mid
        self._active_bid = bid
        self._active_ask = ask

        return Signal(
            signal_type=signal_type,
            price=price,
            size=order_size,
            confidence=confidence,
            strategy=self.name,
            reason=reason,
            timestamp=ts,
            metadata={
                "mid": mid,
                "bid": bid,
                "ask": ask,
                "half_spread": half_spread,
                "quoted_spread": round(ask - bid, 4),
                "inventory": self._inventory,
                "spread_pnl": round(self._spread_pnl, 4),
                "bid_fills": self._bid_fills,
                "ask_fills": self._ask_fills,
                "market_spread_pct": round(market_spread_pct, 4),
                # Flag para o executor saber que deve colocar dois lados
                "dual_quote": signal_type == SignalType.BUY
                and abs(self._inventory) < max_inventory,
                "ask_price": ask,
            },
        )

    def _check_fills(self, candle_high: float, candle_low: float) -> None:
        """
        Simula preenchimento das ordens ativas com base no range do candle.

        Lógica conservadora:
            - Bid preenche se candle.low toca ou cruza o bid_price
            - Ask preenche se candle.high toca ou cruza o ask_price

        Args:
            candle_high: Máxima do candle atual.
            candle_low:  Mínima do candle atual.
        """
        order_size = self.get_param("order_size", 5.0)

        if self._active_bid is not None and candle_low <= self._active_bid:
            # Bid foi preenchido: ficamos mais longos
            self._inventory += order_size
            self._bid_fills += 1
            logger.debug(
                f"[{self.name}] BID preenchido @ {self._active_bid:.4f} | "
                f"inventário={self._inventory:.1f}"
            )
            self._active_bid = None

        if self._active_ask is not None and candle_high >= self._active_ask:
            # Ask foi preenchido: ficamos mais curtos (ou reduzimos long)
            prev_inv = self._inventory
            self._inventory -= order_size
            self._ask_fills += 1

            # Se tínhamos inventário longo e vendemos, capturamos o spread
            if prev_inv > 0:
                spread_captured = (
                    self._active_ask - (self._active_ask - 2 * self.get_param("half_spread", 0.015))
                ) * order_size
                self._spread_pnl += spread_captured

            logger.debug(
                f"[{self.name}] ASK preenchido @ {self._active_ask:.4f} | "
                f"inventário={self._inventory:.1f} | "
                f"spread_pnl={self._spread_pnl:.4f}"
            )
            self._active_ask = None

    def reset_inventory(self) -> None:
        """Reseta o inventário e estado de ordens. Use ao reiniciar o bot."""
        self._inventory = 0.0
        self._last_quoted_mid = None
        self._active_bid = None
        self._active_ask = None
        logger.info(f"[{self.name}] Inventário resetado.")

    @property
    def stats(self) -> dict:
        """Retorna estatísticas acumuladas da estratégia."""
        total_fills = self._bid_fills + self._ask_fills
        return {
            "inventory": self._inventory,
            "spread_pnl": round(self._spread_pnl, 4),
            "bid_fills": self._bid_fills,
            "ask_fills": self._ask_fills,
            "total_fills": total_fills,
            "fill_balance": self._bid_fills - self._ask_fills,
        }
