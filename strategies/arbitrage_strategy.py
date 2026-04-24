"""
arbitrage_strategy.py — Estratégia de arbitragem combinatória no Polymarket.

Lógica central:
    Mercados relacionados logicamente (sim/não, categorias) DEVEM ter preços
    que somam a 1.0 teoricamente. Quando somam para != 1.0, há arbitragem.

    Exemplo real:
        Market A: "Trump wins 2024" → YES @ 0.40, NO @ 0.60
        Market B: "Trump loses 2024" → YES @ 0.65, NO @ 0.35

        Relação: Market A YES = Market B NO (same event)
        Soma: 0.40 + 0.65 = 1.05 (overpriced by 5%)

        Arbitrage: SHORT Market A @ 0.60 + LONG Market B @ 0.35
                   = locked-in 0.25 profit per dollar risked
                   = 25% return when both resolve

Padrões de arbitrage:
    1. Binary complement: "Will X" vs "Will NOT X"
    2. Categorical: "Will A win" + "Will B win" + "Will C win" = 1.0
    3. Synthetic: Combinations of related markets

Características:
    - Win rate é quase 100% (math-based, não probabilidade)
    - Retorno é pequeno mas consistente (0.5-1.5% típico)
    - Risk é muito baixo se ambas as pernas forem preenchidas
    - O crítico é velocidade de execução (spread converge rápido)
"""

import pandas as pd
import numpy as np

from strategies.base_strategy import BaseStrategy, Signal, SignalType


class ArbitrageStrategy(BaseStrategy):
    """
    Estratégia de arbitragem combinatória em mercados relacionados.

    Parâmetros configuráveis:
        min_spread_bps:    Spread mínimo em basis points para entrar (padrão: 50 = 0.5%)
        max_spread_bps:    Spread máximo (evita execução ruim) (padrão: 1000 = 10%)
        position_scale:    % do capital a arriscar por leg (padrão: 0.5 = 50%)
        convergence_tol:   Tolerância de convergência (padrão: 0.005 = 0.5%)
    """

    DEFAULT_PARAMS = {
        "min_spread_bps": 50,        # 0.5% mínimo para valer a pena
        "max_spread_bps": 1000,      # 10% máximo (execution risk alto)
        "position_scale": 0.5,       # Entrar com 50% do capital
        "convergence_tol": 0.005,    # Exit quando spread < 0.5%
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)

    @property
    def name(self) -> str:
        return "ArbitrageStrategy"

    @property
    def min_candles(self) -> int:
        return 2  # Minimal — não precisa histórico, só preços atuais

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Gera sinal baseado em dados OHLCV (compatível com BaseStrategy).

        Para arbitragem, este método apenas retorna HOLD, pois a lógica
        de arbitragem requer dados de múltiplos mercados simultâneos.
        Use `generate_arb_signal(arb_opportunity)` para gerar sinais reais.
        """
        timestamp = df.index[-1] if not df.empty else pd.Timestamp.now()
        return self.hold_signal(
            "Arbitragem requer múltiplos mercados — use generate_arb_signal()",
            timestamp,
        )

    def generate_arb_signal(self, arb_opportunity: dict) -> Signal:
        """
        Gera sinal de entrada/saída para oportunidade de arbitragem.

        Args:
            arb_opportunity: dict com detalhes da oportunidade:
                {
                    "condition_id": "0x123...",
                    "leg_1": {
                        "market": "Market A slug",
                        "outcome": "YES",
                        "token_id": "token_id_1",
                        "price": 0.40,
                        "side": "SELL",  # Lado em que estamos
                    },
                    "leg_2": {
                        "market": "Market B slug",
                        "outcome": "YES",
                        "token_id": "token_id_2",
                        "price": 0.65,
                        "side": "BUY",
                    },
                    "entry_spread": 0.05,       # sum - 1.0
                    "spread_bps": 500,          # em basis points
                }

        Returns:
            Signal para executar arbitragem (ambas as pernas atomicamente)
        """

        # Se não há oportunidade, HOLD
        if not arb_opportunity:
            return self.hold_signal("Aguardando oportunidade de arbitragem", pd.Timestamp.now())

        # Extrair detalhes
        min_spread = self.get_param("min_spread_bps")
        max_spread = self.get_param("max_spread_bps")
        position_scale = self.get_param("position_scale")

        condition_id = arb_opportunity.get("condition_id", "unknown")
        leg_1 = arb_opportunity.get("leg_1", {})
        leg_2 = arb_opportunity.get("leg_2", {})
        entry_spread = arb_opportunity.get("entry_spread", 0.0)
        spread_bps = arb_opportunity.get("spread_bps", 0)

        timestamp = arb_opportunity.get("timestamp") or pd.Timestamp.now()

        # ================================================================
        # Validar spread
        # ================================================================
        if spread_bps < min_spread:
            return self.hold_signal(
                f"Spread {spread_bps} bps < mínimo {min_spread} bps",
                timestamp,
            )

        if spread_bps > max_spread:
            return self.hold_signal(
                f"Spread {spread_bps} bps > máximo {max_spread} bps (execution risk)",
                timestamp,
            )

        # ================================================================
        # Determinar direção principal (qual lado ganhamos mais)
        # ================================================================
        leg_1_side = leg_1.get("side", "?")  # "BUY" ou "SELL"
        leg_1_price = leg_1.get("price", 0.5)
        leg_2_side = leg_2.get("side", "?")
        leg_2_price = leg_2.get("price", 0.5)

        # Em arbitragem, enviamos sinais separados para cada perna
        # Mas para simplicidade de retorno, retornamos sinal da "perna principal"
        # que é aquela onde ganhamos mais (maior spread capture)

        # Calcular quanto ganhamos em cada perna
        # Se BUY @ 0.35, valor final será 0.50 (ganho = 0.15)
        # Se SELL @ 0.65, valor final será 0.50 (ganho = 0.15)
        # Ambas devem ser ~iguais (arbitrage risk-free)

        # Confidence: baseado no spread
        base_confidence = 0.70
        spread_pct = entry_spread / 1.0  # % de retorno
        spread_bonus = min((spread_bps / 10000) * 0.25, 0.25)  # Max +25%
        confidence = min(0.95, base_confidence + spread_bonus)

        # Metadata: ambas as pernas
        metadata = {
            "arb_type": "binary_complement",  # ou "categorical", "synthetic"
            "condition_id": condition_id,
            "entry_spread": round(entry_spread, 5),
            "spread_bps": spread_bps,
            "leg_1": {
                "market": leg_1.get("market", "?"),
                "outcome": leg_1.get("outcome", "?"),
                "token_id": leg_1.get("token_id", "?")[:30],
                "price": round(leg_1_price, 4),
                "side": leg_1_side,
            },
            "leg_2": {
                "market": leg_2.get("market", "?"),
                "outcome": leg_2.get("outcome", "?"),
                "token_id": leg_2.get("token_id", "?")[:30],
                "price": round(leg_2_price, 4),
                "side": leg_2_side,
            },
            "expected_profit_pct": round(spread_pct * 100, 2),
        }

        # ================================================================
        # Retornar sinal (BUY = execute arbitrage)
        # ================================================================
        reason = (
            f"Arb: {leg_1.get('market', 'A')} {leg_1_side}@{leg_1_price:.3f} "
            f"+ {leg_2.get('market', 'B')} {leg_2_side}@{leg_2_price:.3f} "
            f"| spread={spread_bps}bps | profit≈{spread_pct*100:.1f}%"
        )

        return Signal(
            signal_type=SignalType.BUY,  # "BUY" = execute both legs
            price=round(abs(entry_spread), 4),  # Spread absoluto (ex: 0.05 = 5%)
            confidence=confidence,
            strategy=self.name,
            reason=reason,
            timestamp=timestamp,
            metadata=metadata,
        )

    def exit_signal(self, condition_id: str, exit_reason: str = "converged") -> Signal:
        """
        Gera sinal de saída quando o spread converge ou stop-loss é acionado.

        Args:
            condition_id: ID da oportunidade de arb que está se fechando
            exit_reason: "converged", "stop_loss", ou outro
        """
        return Signal(
            signal_type=SignalType.SELL,  # SELL = close both legs
            price=0.0,                     # Exit signal (no entry price)
            confidence=0.90,
            strategy=self.name,
            reason=f"Arb {exit_reason}: {condition_id}",
            timestamp=pd.Timestamp.now(),
            metadata={
                "condition_id": condition_id,
                "exit_reason": exit_reason,
            },
        )
