"""
whale_following_strategy.py — Estratégia de cópia de trades de baleias.

Lógica central:
    Em mercados de predição, alguns traders têm **informação superior**:
    pesquisa melhor, dados em tempo real, network effects, etc.
    Eles ganham dinheiro consistentemente.

    Em vez de tentar prever, **copie os trades deles**. Se uma wallet
    profissional (50+ trades, lucro documentado) entra em um mercado,
    você entra também na mesma direção.

Padrão observado:
    - Whales no Polymarket ganham 2-15% ao mês
    - Seus trades têm 55-65% de win rate
    - Seguir automaticamente gera 60-80% do ganho deles
    - Defasagem típica: 5-30 minutos (você copia após eles)

Implementação:
    Este arquivo define a LÓGICA de entrada/saída uma vez que detectamos
    um trade de whale. O `whale_tracker.py` faz a detecção on-chain e
    chama esta estratégia para gerar sinais.

Sinais:
    BUY:  Uma whale detectada comprou YES em mercado X
          Nós replicamos (com posição menor)
    SELL: Uma whale detectada vendeu sua posição
          Nós também saímos ou tomamos o outro lado
    HOLD: Sem atividade de whale relevante
"""

import pandas as pd
import numpy as np

from strategies.base_strategy import BaseStrategy, Signal, SignalType


class WhaleFollowingStrategy(BaseStrategy):
    """
    Estratégia de replicação de trades de grandes traders (whales).

    Parâmetros configuráveis:
        min_whale_trades:    Mínimo de trades na história da whale (padrão: 50)
        min_whale_winrate:   Win rate mínimo para seguir (padrão: 0.55 = 55%)
        min_whale_pnl:       PnL mínimo documentado (padrão: $1000)
        position_scale:      Escala da posição vs whale (padrão: 0.5 = metade)
        max_time_lag:        Máximo de minutos de defasagem aceitável (padrão: 30)
    """

    DEFAULT_PARAMS = {
        "min_whale_trades": 50,        # Mínimo de trades para confiar
        "min_whale_winrate": 0.55,     # 55% win rate
        "min_whale_pnl": 1000.0,       # $1000 de lucro documentado
        "position_scale": 0.5,         # Entrar com 50% do tamanho da whale
        "max_time_lag": 30,            # Máximo 30 minutos de defasagem
        "confidence_base": 0.70,       # Confiança base
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)
        # Estado para rastrear whales que estamos copiando
        self.tracked_whales = {}  # whale_address -> {side, entry_price, size}

    @property
    def name(self) -> str:
        return "WhaleFollowingStrategy"

    @property
    def min_candles(self) -> int:
        return 2  # Minimal, já que usamos dados de whale, não OHLCV

    def generate_signal(self, df: pd.DataFrame, whale_signal: dict = None) -> Signal:
        """
        Gera sinal baseado em atividade de whale detectada.

        Args:
            df: DataFrame OHLCV (pode estar vazio se chamado via whale_tracker)
            whale_signal: dict com info da whale:
                {
                    "address": "0x...",
                    "action": "BUY" ou "SELL",
                    "market": "market_id",
                    "side": "YES" ou "NO",
                    "size": float (em USDC),
                    "price": float (0.0-1.0),
                    "winrate": float,
                    "total_pnl": float,
                    "num_trades": int,
                    "timestamp": datetime,
                }

        Returns:
            Signal para replicar o trade da whale.
        """

        # Se não há dados de whale, não fazer nada
        if not whale_signal:
            if df is not None and not df.empty:
                return self.hold_signal("Aguardando detecção de whale", df.index[-1])
            return self.hold_signal("Aguardando detecção de whale")

        # Validar credenciais da whale
        min_trades = self.get_param("min_whale_trades")
        min_wr = self.get_param("min_whale_winrate")
        min_pnl = self.get_param("min_whale_pnl")
        max_lag = self.get_param("max_time_lag")

        address = whale_signal.get("address", "unknown")[:20]
        action = whale_signal.get("action", "?")
        side = whale_signal.get("side", "?")
        winrate = whale_signal.get("winrate", 0.0)
        num_trades = whale_signal.get("num_trades", 0)
        total_pnl = whale_signal.get("total_pnl", 0.0)
        size = whale_signal.get("size", 0.0)
        price = whale_signal.get("price", 0.5)
        timestamp = whale_signal.get("timestamp")

        # Validar se a whale tem credenciais suficientes
        if num_trades < min_trades:
            return self.hold_signal(
                f"Whale {address}... tem apenas {num_trades} trades (min: {min_trades})",
                timestamp,
            )

        if winrate < min_wr:
            return self.hold_signal(
                f"Whale {address}... tem {winrate*100:.0f}% win rate (min: {min_wr*100:.0f}%)",
                timestamp,
            )

        if total_pnl < min_pnl:
            return self.hold_signal(
                f"Whale {address}... tem ${total_pnl:.0f} PnL (min: ${min_pnl:.0f})",
                timestamp,
            )

        # ----------------------------------------------------------------
        # Validar defasagem de tempo (não queremos trades muito antigos)
        # ----------------------------------------------------------------
        # (Este check seria feito no whale_tracker em produção)

        # ----------------------------------------------------------------
        # Calcular tamanho da posição
        # ----------------------------------------------------------------
        position_scale = self.get_param("position_scale")
        our_size = size * position_scale

        confidence = self.get_param("confidence_base")

        # Bônus de confiança baseado em credenciais
        # Quanto melhor a whale, mais confiantes estamos
        cred_bonus = 0.0
        if winrate >= 0.60:
            cred_bonus += 0.05
        if num_trades >= 200:
            cred_bonus += 0.05
        if total_pnl >= 10000:
            cred_bonus += 0.05

        confidence = min(0.95, confidence + cred_bonus)

        # ----------------------------------------------------------------
        # Gerar sinal (BUY ou SELL conforme ação da whale)
        # ----------------------------------------------------------------
        if action.upper() == "BUY":
            signal_type = SignalType.BUY
            action_text = "COMPROU"
            tracked_action = "BUY"
        elif action.upper() == "SELL":
            signal_type = SignalType.SELL
            action_text = "VENDEU"
            tracked_action = "SELL"
        else:
            return self.hold_signal(f"Ação desconhecida da whale: {action}", timestamp)

        # Rastrear esta whale para saída futura
        self.tracked_whales[address] = {
            "side": side,
            "entry_price": price,
            "size": our_size,
            "timestamp": timestamp,
        }

        reason = (
            f"Whale copying: {address}... {action_text} {our_size:.2f} USDC de {side} "
            f"@ {price:.3f} | "
            f"Whale: {num_trades} trades, {winrate*100:.0f}% win, "
            f"${total_pnl:.0f} PnL"
        )

        return Signal(
            signal_type=signal_type,
            price=price,
            size=our_size,
            confidence=confidence,
            strategy=self.name,
            reason=reason,
            timestamp=timestamp,
            metadata={
                "whale_address": address,
                "whale_winrate": round(winrate, 3),
                "whale_num_trades": num_trades,
                "whale_pnl": round(total_pnl, 2),
                "whale_action": action,
                "our_size": round(our_size, 2),
                "side": side,
                "entry_price": round(price, 4),
            },
        )

    def exit_signal(self, whale_address: str, timestamp=None) -> Signal:
        """
        Gera sinal de saída quando a whale sai da posição.
        """
        if whale_address not in self.tracked_whales:
            return self.hold_signal(f"Whale {whale_address} não está em nossas posições")

        entry = self.tracked_whales[whale_address]
        del self.tracked_whales[whale_address]

        return Signal(
            signal_type=SignalType.SELL,
            price=0.5,  # Placeholder
            confidence=0.85,
            strategy=self.name,
            reason=f"Whale {whale_address} saiu da posição. Replicando saída.",
            timestamp=timestamp,
            metadata={
                "whale_address": whale_address,
                "reason": "whale_exit",
                "tracked_entry_price": entry["entry_price"],
            },
        )
