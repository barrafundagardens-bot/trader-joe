"""
strategies/ — Módulo de estratégias de trading para Polymarket.

Estratégias disponíveis:
- MACDStrategy:        MACD clássico via biblioteca `ta`
- RSIStrategy:         RSI com filtro de volume via biblioteca `ta`
- CVDStrategy:         Cumulative Volume Delta (lógica custom em pandas)
- FVGMultiTFStrategy:  Fair Value Gap multi-timeframe 4H + 15M (nome original)
- FVGMultiTF:          Fair Value Gap multi-timeframe 4H + 15M (standalone)
"""

from strategies.base_strategy import BaseStrategy, Signal
from strategies.macd_strategy import MACDStrategy
from strategies.rsi_strategy import RSIStrategy
from strategies.cvd_strategy import CVDStrategy
from strategies.fvg_multitf_strategy import FVGMultiTFStrategy
from strategies.fvg_multitf import FVGMultiTF

__all__ = [
    "BaseStrategy",
    "Signal",
    "MACDStrategy",
    "RSIStrategy",
    "CVDStrategy",
    "FVGMultiTFStrategy",
    "FVGMultiTF",
]
