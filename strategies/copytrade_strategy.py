"""
strategies/copytrade_strategy.py — Copytrade de carteiras top do Polymarket.

Monitora as carteiras com melhor histórico de PnL via API pública e
espelha suas posições com sizing proporcional ao capital disponível.

Lógica central:
    1. Buscar top wallets via WalletTracker
    2. Detectar novas posições abertas por essas carteiras
    3. Calcular consenso: quantas carteiras estão indo na mesma direção
    4. Gerar sinal se consenso >= min_consensus
    5. Sizing proporcional ao tamanho médio das posições copiadas

Modos:
    live:   Detecta moves reais via HTTP (data-api.polymarket.com)
    paper:  Usa volume anormal como proxy de atividade de whales (offline)

Parâmetros:
    min_consensus:        Mínimo de carteiras na mesma direção (padrão: 2)
    min_confidence:       Confiança mínima média para agir (padrão: 0.50)
    volume_lookback:      Candles para calcular volume médio (padrão: 20)
    volume_spike_ratio:   Razão volume/média para detectar spike (padrão: 1.5)
    price_change_threshold: Variação mínima de preço para sinal (padrão: 0.01)
    order_size:           Tamanho base da ordem em USDC (padrão: 5.0)
    simulation:           Se True, usa modo paper offline (padrão: True)
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import List, Optional

import pandas as pd

from strategies.base_strategy import BaseStrategy, Signal, SignalType
from bot.wallet_tracker import WalletMove, WalletTracker

logger = logging.getLogger(__name__)


class CopytradeStrategy(BaseStrategy):
    """
    Estratégia de copytrade baseada em atividade de top wallets.

    Em modo paper (simulation=True), detecta spikes de volume + price
    change como proxy de movimentação de carteiras grandes, permitindo
    testes 100% offline sem chamadas de API.

    Em modo live (simulation=False), conecta ao WalletTracker que faz
    chamadas reais à data-api.polymarket.com e detecta transações novas
    das carteiras monitoradas.

    Args:
        params:     Dicionário de configuração (veja parâmetros acima).
        market_id:  Condition ID do mercado para o WalletTracker.
        token_id:   Token ID do mercado para o WalletTracker.
    """

    def __init__(
        self,
        params: Optional[dict] = None,
        market_id: str = "",
        token_id: str = "",
    ):
        super().__init__(params)
        simulation = self.get_param("simulation", True)

        self._tracker = WalletTracker(
            market_id=market_id,
            token_id=token_id,
            simulation=simulation,
            min_pnl=self.get_param("min_wallet_pnl", 500.0),
            min_move_size=self.get_param("min_move_size", 50.0),
            top_n=self.get_param("top_n_wallets", 10),
        )
        # Histórico de moves para detectar consenso
        self._pending_moves: List[WalletMove] = []

    @property
    def name(self) -> str:
        return "Copytrade"

    @property
    def min_candles(self) -> int:
        return 25  # Precisa de histórico para calcular volume médio

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Gera sinal baseado em atividade de carteiras top.

        Em paper mode: analisa volume e price change do candle atual.
        Em live mode: consulta WalletTracker por novas transações.

        Args:
            df: DataFrame OHLCV com DatetimeIndex.

        Returns:
            Signal com BUY, SELL ou HOLD.
        """
        if not self.validate_dataframe(df):
            return self.hold_signal("Dados insuficientes para Copytrade")

        candle = df.iloc[-1]
        ts = df.index[-1]
        simulation = self.get_param("simulation", True)

        if simulation:
            return self._signal_from_volume(df, candle, ts)
        else:
            return self._signal_from_live_wallets(candle, ts)

    # ------------------------------------------------------------------
    # Modo paper: volume spike como proxy de whale activity
    # ------------------------------------------------------------------

    def _signal_from_volume(
        self, df: pd.DataFrame, candle: pd.Series, ts: pd.Timestamp
    ) -> Signal:
        """
        Detecta whales via volume anormal + price change direcional.

        Heurística: se o volume do candle é >= `volume_spike_ratio` vezes
        a média dos últimos N candles e o preço se moveu >= threshold,
        é provável que uma carteira grande entrou no mercado.
        """
        lookback = self.get_param("volume_lookback", 20)
        spike_ratio = self.get_param("volume_spike_ratio", 1.5)
        price_change_threshold = self.get_param("price_change_threshold", 0.01)
        min_confidence = self.get_param("min_confidence", 0.50)
        order_size = self.get_param("order_size", 5.0)

        # Volume médio dos N candles anteriores (excluindo o atual)
        avg_volume = df["volume"].iloc[-lookback - 1 : -1].mean()
        current_volume = float(candle["volume"])
        current_price = float(candle["close"])
        prev_price = float(df["close"].iloc[-2])

        if prev_price <= 0:
            return self.hold_signal("Preço anterior inválido", timestamp=ts)

        price_change_pct = (current_price - prev_price) / prev_price

        move = self._tracker.generate_synthetic_move(
            price=current_price,
            volume=current_volume,
            avg_volume=avg_volume,
            price_change_pct=price_change_pct,
        )

        if move is None or move.confidence < min_confidence:
            return self.hold_signal(
                f"Sem spike de volume (vol_ratio={current_volume / avg_volume:.2f} "
                f"price_chg={price_change_pct:.3f})",
                timestamp=ts,
            )

        signal_type = SignalType.BUY if move.direction == "BUY" else SignalType.SELL
        vol_ratio = current_volume / avg_volume if avg_volume > 0 else 0

        return Signal(
            signal_type=signal_type,
            price=current_price,
            size=order_size,
            confidence=move.confidence,
            strategy=self.name,
            reason=(
                f"Whale sintético detectado: {move.direction} "
                f"vol_ratio={vol_ratio:.1f}x avg "
                f"price_chg={price_change_pct:+.3f} "
                f"conf={move.confidence:.2f}"
            ),
            timestamp=ts,
            metadata={
                "volume_ratio": round(vol_ratio, 2),
                "price_change_pct": round(price_change_pct, 4),
                "avg_volume": round(avg_volume, 2),
                "current_volume": round(current_volume, 2),
                "synthetic_move": True,
                "whale_direction": move.direction,
                "whale_confidence": move.confidence,
            },
        )

    # ------------------------------------------------------------------
    # Modo live: detecta transações reais via WalletTracker
    # ------------------------------------------------------------------

    def _signal_from_live_wallets(
        self, candle: pd.Series, ts: pd.Timestamp
    ) -> Signal:
        """
        Detecta novas posições de carteiras top via API pública.

        Calcula consenso: se >= min_consensus carteiras foram na mesma
        direção, gera um sinal nessa direção com confiança média.
        """
        min_consensus = self.get_param("min_consensus", 2)
        min_confidence = self.get_param("min_confidence", 0.50)
        order_size = self.get_param("order_size", 5.0)

        new_moves = self._tracker.detect_new_moves()
        if not new_moves:
            return self.hold_signal(
                "Sem novas movimentações nas carteiras monitoradas",
                timestamp=ts,
            )

        # Calcular consenso direcional
        direction_count = Counter(m.direction for m in new_moves)
        dominant_direction = direction_count.most_common(1)[0][0]
        dominant_count = direction_count[dominant_direction]

        if dominant_count < min_consensus:
            return self.hold_signal(
                f"Consenso insuficiente: {dominant_count} de {len(new_moves)} "
                f"carteiras em {dominant_direction} (mínimo: {min_consensus})",
                timestamp=ts,
            )

        # Filtrar moves na direção dominante
        aligned_moves = [m for m in new_moves if m.direction == dominant_direction]
        avg_confidence = sum(m.confidence for m in aligned_moves) / len(aligned_moves)
        avg_price = sum(m.price for m in aligned_moves) / len(aligned_moves)
        total_size = sum(m.size_usdc for m in aligned_moves)

        if avg_confidence < min_confidence:
            return self.hold_signal(
                f"Confiança média insuficiente: {avg_confidence:.2f} < {min_confidence:.2f}",
                timestamp=ts,
            )

        signal_type = SignalType.BUY if dominant_direction == "BUY" else SignalType.SELL
        wallet_addresses = [m.wallet[:8] + "…" for m in aligned_moves]

        return Signal(
            signal_type=signal_type,
            price=avg_price,
            size=order_size,
            confidence=avg_confidence,
            strategy=self.name,
            reason=(
                f"Copytrade: {dominant_count} carteiras em {dominant_direction} "
                f"| avg_price={avg_price:.4f} "
                f"| total_size=${total_size:.0f} "
                f"| conf={avg_confidence:.2f} "
                f"| wallets={wallet_addresses}"
            ),
            timestamp=ts,
            metadata={
                "dominant_direction": dominant_direction,
                "wallet_count": dominant_count,
                "avg_confidence": round(avg_confidence, 3),
                "avg_price": round(avg_price, 4),
                "total_whale_size": round(total_size, 2),
                "wallets": [m.wallet for m in aligned_moves],
                "synthetic_move": False,
            },
        )
