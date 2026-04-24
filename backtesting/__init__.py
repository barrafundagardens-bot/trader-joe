"""
backtesting/ — Motor de backtesting com suporte a multi-timeframe.

Uso rápido:
    from backtesting.engine import BacktestEngine
    from backtesting.metrics import BacktestMetrics
    from strategies import FVGMultiTFStrategy

    engine = BacktestEngine(
        strategy=FVGMultiTFStrategy(),
        initial_capital=100.0,
        trade_size=10.0,
    )
    results = engine.run(df)
    metrics = BacktestMetrics(results)
    metrics.print_summary()
"""

from backtesting.engine import BacktestEngine, BacktestTrade, BacktestResult
from backtesting.metrics import BacktestMetrics

__all__ = [
    "BacktestEngine",
    "BacktestTrade",
    "BacktestResult",
    "BacktestMetrics",
]
