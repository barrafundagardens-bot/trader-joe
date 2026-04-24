"""
bot/ — Módulo de execução de trades em produção.

Componentes:
    Trader:       Executa ordens limit via py-clob-client.
    RiskManager:  Controla risco por trade, limite diário e emergência.
"""

from bot.risk_manager import RiskManager, RiskDecision
from bot.trader import Trader
from bot.manual_override import ManualOverrideManager, BlockedToken

__all__ = ["RiskManager", "RiskDecision", "Trader", "ManualOverrideManager", "BlockedToken"]
