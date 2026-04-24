"""
base_strategy.py — Classe abstrata base para todas as estratégias.

Define a interface comum que todas as estratégias devem implementar,
garantindo compatibilidade com o BacktestingEngine e o Trader.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import pandas as pd


class SignalType(Enum):
    """Tipo de sinal gerado pela estratégia."""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class Signal:
    """
    Sinal de trading gerado por uma estratégia.

    Attributes:
        signal_type:  BUY, SELL ou HOLD
        price:        Preço sugerido para a limit order (0.0 a 1.0 no Polymarket)
        size:         Tamanho sugerido em USDC (RiskManager pode sobrescrever)
        confidence:   Confiança no sinal (0.0 a 1.0) — informativo
        strategy:     Nome da estratégia que gerou o sinal
        reason:       Explicação textual do sinal (para logs e debug)
        timestamp:    Timestamp do candle que gerou o sinal
        metadata:     Dados adicionais da estratégia (indicadores, etc.)
    """
    signal_type: SignalType
    price: float
    size: float = 0.0
    confidence: float = 0.5
    strategy: str = ""
    reason: str = ""
    timestamp: Optional[pd.Timestamp] = None
    metadata: dict = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        """Retorna True se o sinal requer uma ação (não é HOLD)."""
        return self.signal_type != SignalType.HOLD

    def __repr__(self) -> str:
        return (
            f"Signal({self.signal_type.value} @ {self.price:.4f} "
            f"size={self.size:.2f} conf={self.confidence:.2f} "
            f"[{self.strategy}]: {self.reason})"
        )


class BaseStrategy(ABC):
    """
    Classe base abstrata para estratégias de trading.

    Todas as estratégias devem herdar desta classe e implementar
    os métodos abstratos `generate_signal` e `name`.

    O método `generate_signal` recebe um DataFrame com candles OHLCV
    e retorna um Signal com BUY, SELL ou HOLD.

    Convenção de dados:
        df deve ter colunas: open, high, low, close, volume
        df.index deve ser DatetimeIndex
        Valores de preço no Polymarket são entre 0.0 e 1.0 (probabilidade)
    """

    def __init__(self, params: Optional[dict] = None):
        """
        Args:
            params: Dicionário de parâmetros da estratégia.
                    Cada subclasse define seus próprios parâmetros padrão.
        """
        self._params = params or {}

    @property
    @abstractmethod
    def name(self) -> str:
        """Nome único da estratégia (usado em logs e resultados)."""
        ...

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Gera um sinal de trading com base nos dados OHLCV fornecidos.

        Args:
            df: DataFrame com colunas [open, high, low, close, volume]
                e DatetimeIndex. Deve ter pelo menos `min_candles` linhas.

        Returns:
            Signal com BUY, SELL ou HOLD e metadados relevantes.

        Notes:
            - Use apenas dados disponíveis até o último candle (sem lookahead)
            - Preços no Polymarket são probabilidades entre 0.0 e 1.0
            - Em caso de dados insuficientes, retorne Signal(HOLD)
        """
        ...

    @property
    def min_candles(self) -> int:
        """
        Número mínimo de candles necessários para gerar um sinal válido.
        Subclasses devem sobrescrever este valor conforme necessário.
        """
        return 50

    def validate_dataframe(self, df: pd.DataFrame) -> bool:
        """
        Valida se o DataFrame tem a estrutura correta.

        Args:
            df: DataFrame a ser validado.

        Returns:
            True se válido, False caso contrário.
        """
        required_columns = {"open", "high", "low", "close", "volume"}
        if not required_columns.issubset(df.columns):
            return False
        if len(df) < self.min_candles:
            return False
        if df.isnull().all().any():
            return False
        return True

    def hold_signal(self, reason: str = "Dados insuficientes",
                    timestamp: Optional[pd.Timestamp] = None) -> Signal:
        """Atalho para criar um sinal de HOLD."""
        return Signal(
            signal_type=SignalType.HOLD,
            price=0.0,
            confidence=0.0,
            strategy=self.name,
            reason=reason,
            timestamp=timestamp,
        )

    def get_param(self, key: str, default=None):
        """Retorna um parâmetro da estratégia com valor padrão."""
        return self._params.get(key, default)
