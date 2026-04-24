"""
metrics.py — Métricas de performance para backtesting.

Calcula as métricas mais importantes para avaliar uma estratégia de trading:
    - Total Return, Sharpe Ratio, Max Drawdown
    - Win Rate, Profit Factor, Average Win/Loss
    - Total de trades, trades vencedores/perdedores
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List
import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from backtesting.engine import BacktestResult


class BacktestMetrics:
    """
    Calcula e exibe métricas de performance de um backtest.

    Args:
        result: BacktestResult retornado pelo BacktestEngine.
    """

    def __init__(self, result: "BacktestResult"):
        self.result = result
        self._metrics: dict = {}
        self._calculate()

    def _calculate(self) -> None:
        """Calcula todas as métricas a partir dos resultados do backtest."""
        trades = self.result.trades
        equity_curve = self.result.equity_curve
        initial_capital = self.result.initial_capital

        if not trades:
            self._metrics = self._empty_metrics()
            return

        # ----------------------------------------------------------------
        # Métricas básicas de trades
        # ----------------------------------------------------------------
        pnls = [t.pnl for t in trades]
        winning_trades = [p for p in pnls if p > 0]
        losing_trades = [p for p in pnls if p <= 0]

        total_trades = len(trades)
        win_count = len(winning_trades)
        loss_count = len(losing_trades)
        win_rate = win_count / total_trades if total_trades > 0 else 0.0

        total_profit = sum(winning_trades) if winning_trades else 0.0
        total_loss = abs(sum(losing_trades)) if losing_trades else 0.0
        profit_factor = total_profit / total_loss if total_loss > 0 else float("inf")

        avg_win = np.mean(winning_trades) if winning_trades else 0.0
        avg_loss = np.mean([abs(p) for p in losing_trades]) if losing_trades else 0.0
        risk_reward = avg_win / avg_loss if avg_loss > 0 else float("inf")

        # ----------------------------------------------------------------
        # Retorno total
        # ----------------------------------------------------------------
        final_capital = equity_curve[-1] if equity_curve else initial_capital
        total_return = (final_capital - initial_capital) / initial_capital
        total_pnl = sum(pnls)

        # ----------------------------------------------------------------
        # Drawdown máximo
        # ----------------------------------------------------------------
        max_drawdown, max_drawdown_pct = self._calculate_max_drawdown(equity_curve)

        # ----------------------------------------------------------------
        # Sharpe Ratio (simplificado — sem taxa livre de risco)
        # ----------------------------------------------------------------
        sharpe = self._calculate_sharpe(pnls, initial_capital)

        # ----------------------------------------------------------------
        # Período do backtest
        # ----------------------------------------------------------------
        start_date = trades[0].entry_time if trades else None
        end_date = trades[-1].exit_time if trades else None
        duration_days = None
        if start_date and end_date:
            duration_days = (end_date - start_date).days

        self._metrics = {
            "total_trades": total_trades,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "risk_reward_ratio": risk_reward,
            "total_pnl": total_pnl,
            "total_return_pct": total_return * 100,
            "initial_capital": initial_capital,
            "final_capital": final_capital,
            "max_drawdown": max_drawdown,
            "max_drawdown_pct": max_drawdown_pct * 100,
            "sharpe_ratio": sharpe,
            "start_date": start_date,
            "end_date": end_date,
            "duration_days": duration_days,
        }

    def _empty_metrics(self) -> dict:
        """Retorna métricas zeradas quando não há trades."""
        return {
            "total_trades": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "risk_reward_ratio": 0.0,
            "total_pnl": 0.0,
            "total_return_pct": 0.0,
            "initial_capital": self.result.initial_capital,
            "final_capital": self.result.initial_capital,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0,
            "start_date": None,
            "end_date": None,
            "duration_days": None,
        }

    @staticmethod
    def _calculate_max_drawdown(equity_curve: List[float]) -> tuple:
        """
        Calcula o drawdown máximo da curva de equity.

        Returns:
            Tupla (max_drawdown_absoluto, max_drawdown_percentual)
        """
        if not equity_curve or len(equity_curve) < 2:
            return 0.0, 0.0

        equity = np.array(equity_curve)
        peak = np.maximum.accumulate(equity)
        drawdown = equity - peak
        max_dd_abs = float(np.min(drawdown))
        max_dd_pct = float(np.min(drawdown / np.where(peak > 0, peak, 1)))
        return max_dd_abs, max_dd_pct

    @staticmethod
    def _calculate_sharpe(pnls: List[float], initial_capital: float) -> float:
        """
        Calcula o Sharpe Ratio simplificado dos retornos por trade.

        Sharpe = média(retornos) / std(retornos)
        Sem taxa livre de risco (adequado para períodos curtos).

        Returns:
            Sharpe Ratio ou 0.0 se insuficiente.
        """
        if len(pnls) < 2 or initial_capital <= 0:
            return 0.0

        returns = [p / initial_capital for p in pnls]
        mean_r = np.mean(returns)
        std_r = np.std(returns, ddof=1)

        if std_r == 0:
            return float("inf") if mean_r > 0 else 0.0

        return float(mean_r / std_r)

    def get(self, key: str, default=None):
        """Retorna uma métrica pelo nome."""
        return self._metrics.get(key, default)

    def to_dict(self) -> dict:
        """Retorna todas as métricas como dicionário."""
        return dict(self._metrics)

    def print_summary(self) -> None:
        """Imprime um resumo formatado das métricas."""
        m = self._metrics
        sep = "=" * 55

        print(sep)
        print("  BACKTESTING — RESUMO DE PERFORMANCE")
        print(sep)

        if m.get("start_date") and m.get("end_date"):
            print(f"  Período:        {m['start_date']} → {m['end_date']}")
        if m.get("duration_days") is not None:
            print(f"  Duração:        {m['duration_days']} dias")

        print(sep)
        print(f"  Capital inicial: ${m['initial_capital']:.2f}")
        print(f"  Capital final:   ${m['final_capital']:.2f}")
        pnl = m['total_pnl']
        sign = "+" if pnl >= 0 else ""
        print(f"  PnL Total:       {sign}${pnl:.2f}  ({sign}{m['total_return_pct']:.1f}%)")
        print(sep)
        print(f"  Total de trades: {m['total_trades']}")
        print(f"  Vencedores:      {m['win_count']} ({m['win_rate'] * 100:.1f}%)")
        print(f"  Perdedores:      {m['loss_count']}")
        print(f"  Profit Factor:   {m['profit_factor']:.2f}")
        print(f"  Risk/Reward:     {m['risk_reward_ratio']:.2f}")
        print(f"  Média de ganho:  ${m['avg_win']:.3f}")
        print(f"  Média de perda:  ${m['avg_loss']:.3f}")
        print(sep)
        dd = m['max_drawdown']
        print(f"  Max Drawdown:    ${dd:.3f}  ({m['max_drawdown_pct']:.1f}%)")
        print(f"  Sharpe Ratio:    {m['sharpe_ratio']:.3f}")
        print(sep)

        # Aviso de risco
        if m['total_return_pct'] > 50:
            print("  ⚠️  Retorno alto pode indicar overfitting no backtest.")
            print("     Teste com dados fora da amostra antes de usar ao vivo.")
        elif m['total_return_pct'] < 0:
            print("  ❌  Estratégia com retorno negativo no backtest.")
            print("     Revise os parâmetros antes de usar ao vivo.")
        print(sep)
