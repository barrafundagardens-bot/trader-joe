"""
backtesting/engine.py — Motor de backtesting com suporte a multi-timeframe.

Simula a execução de uma estratégia em dados históricos sem lookahead bias.
Suporta estratégias que usam múltiplos timeframes (ex: FVGMultiTFStrategy).

Funcionamento:
    1. Itera candle a candle no timeframe base (ex: 15M)
    2. A cada candle, passa toda a janela de dados disponíveis até aquele
       momento para a estratégia (sem dados futuros — no lookahead)
    3. Executa sinais de BUY/SELL como limit orders simuladas
    4. Aplica stop-loss baseado no RiskManager
    5. Registra cada trade e a curva de equity

Limit Orders simuladas:
    - BUY:  ordem de compra no preço de fechamento do candle sinal
    - SELL: ordem de venda no preço de fechamento do candle sinal
    - Preenchimento: confirmado no próximo candle (sem slippage por padrão)
    - Stop-loss: saída automática se preço cair X% do preço de entrada
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd
import numpy as np

from strategies.base_strategy import BaseStrategy, Signal, SignalType

logger = logging.getLogger(__name__)


# ============================================================
# Data Classes
# ============================================================

@dataclass
class BacktestTrade:
    """Registro de um trade completo no backtest."""
    trade_id: int
    signal_type: str           # 'BUY' ou 'SELL'
    strategy: str
    entry_price: float
    entry_time: pd.Timestamp
    size: float                # USDC investidos
    exit_price: float = 0.0
    exit_time: Optional[pd.Timestamp] = None
    exit_reason: str = ""      # 'signal_exit', 'stop_loss', 'end_of_data'
    pnl: float = 0.0           # Lucro/prejuízo em USDC
    pnl_pct: float = 0.0       # Lucro/prejuízo em %
    is_closed: bool = False

    def close(
        self,
        exit_price: float,
        exit_time: pd.Timestamp,
        exit_reason: str = "signal_exit",
    ) -> None:
        """Fecha o trade e calcula PnL."""
        self.exit_price = exit_price
        self.exit_time = exit_time
        self.exit_reason = exit_reason
        self.is_closed = True

        if self.signal_type == "BUY":
            # Long: lucro quando preço sobe
            price_change = (exit_price - self.entry_price) / self.entry_price
        else:
            # Short (SELL): lucro quando preço cai
            price_change = (self.entry_price - exit_price) / self.entry_price

        self.pnl = self.size * price_change
        self.pnl_pct = price_change * 100

    def __repr__(self) -> str:
        status = "FECHADO" if self.is_closed else "ABERTO"
        return (
            f"Trade#{self.trade_id} [{status}] {self.signal_type} "
            f"@{self.entry_price:.4f} → {self.exit_price:.4f} "
            f"PnL={self.pnl:+.3f} ({self.pnl_pct:+.1f}%) [{self.exit_reason}]"
        )


@dataclass
class BacktestResult:
    """Resultado completo de um backtest."""
    strategy_name: str
    initial_capital: float
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    signals: List[Signal] = field(default_factory=list)
    total_candles_processed: int = 0
    start_date: Optional[pd.Timestamp] = None
    end_date: Optional[pd.Timestamp] = None

    @property
    def final_capital(self) -> float:
        return self.equity_curve[-1] if self.equity_curve else self.initial_capital

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades if t.is_closed)

    @property
    def closed_trades(self) -> List[BacktestTrade]:
        return [t for t in self.trades if t.is_closed]


# ============================================================
# Engine Principal
# ============================================================

class BacktestEngine:
    """
    Motor de backtesting candle a candle.

    Suporta estratégias single e multi-timeframe. Para estratégias
    multi-timeframe (como FVGMultiTFStrategy), o DataFrame base deve
    estar na menor granularidade (ex: 15M), e a estratégia fará
    o resample internamente.

    Args:
        strategy:       Instância de uma BaseStrategy.
        initial_capital: Capital inicial em USDC.
        trade_size:     Tamanho de cada trade em USDC.
        stop_loss_pct:  Stop-loss em % do tamanho da posição (0.10 = 10%).
        warmup_candles: Número de candles iniciais ignorados (warm-up dos indicadores).
        max_open_trades: Máximo de posições abertas simultaneamente.
        fee_pct:        Taxa por trade em % (padrão: 0.0 — Polymarket varia).
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        initial_capital: float = 100.0,
        trade_size: float = 10.0,
        stop_loss_pct: float = 0.10,
        warmup_candles: int = 100,
        max_open_trades: int = 1,
        fee_pct: float = 0.0,
    ):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.trade_size = trade_size
        self.stop_loss_pct = stop_loss_pct
        self.warmup_candles = warmup_candles
        self.max_open_trades = max_open_trades
        self.fee_pct = fee_pct

    def run(self, df: pd.DataFrame) -> BacktestResult:
        """
        Executa o backtest no DataFrame fornecido.

        Itera candle a candle, passando a janela de dados disponíveis
        para a estratégia sem expor dados futuros (no lookahead bias).

        Args:
            df: DataFrame OHLCV com DatetimeIndex.
                Deve ter colunas: open, high, low, close, volume.
                Recomendado: timeframe de 15M ou menor para maior precisão.

        Returns:
            BacktestResult com todos os trades, equity curve e sinais.
        """
        if df is None or len(df) == 0:
            logger.error("DataFrame vazio fornecido ao BacktestEngine")
            return BacktestResult(
                strategy_name=self.strategy.name,
                initial_capital=self.initial_capital,
            )

        # Garantir DatetimeIndex
        df = df.copy()
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()

        result = BacktestResult(
            strategy_name=self.strategy.name,
            initial_capital=self.initial_capital,
            start_date=df.index[self.warmup_candles] if len(df) > self.warmup_candles else df.index[0],
            end_date=df.index[-1],
        )

        capital = self.initial_capital
        open_trades: List[BacktestTrade] = []
        trade_counter = 0
        result.equity_curve.append(capital)

        min_candles_needed = max(self.strategy.min_candles, self.warmup_candles)

        logger.info(
            f"Iniciando backtest: {self.strategy.name} | "
            f"{len(df)} candles | capital=${capital:.2f} | "
            f"warm-up={min_candles_needed}"
        )

        for i in range(min_candles_needed, len(df)):
            current_candle = df.iloc[i]
            current_time = df.index[i]
            current_price = current_candle["close"]

            # --------------------------------------------------------
            # Verificar stop-loss em posições abertas
            # --------------------------------------------------------
            trades_to_close = []
            for trade in open_trades:
                if self._check_stop_loss(trade, current_candle):
                    stop_price = self._get_stop_price(trade)
                    trade.close(stop_price, current_time, "stop_loss")
                    capital += trade.pnl
                    # Desconto de taxa
                    capital -= abs(trade.size) * self.fee_pct
                    trades_to_close.append(trade)
                    result.trades.append(trade)
                    logger.debug(f"Stop-loss acionado: {trade}")

            for t in trades_to_close:
                open_trades.remove(t)

            # --------------------------------------------------------
            # Gerar sinal da estratégia (janela até o candle atual)
            # --------------------------------------------------------
            window = df.iloc[:i + 1]
            signal = self.strategy.generate_signal(window)
            result.signals.append(signal)

            # --------------------------------------------------------
            # Processar sinal
            # --------------------------------------------------------
            if signal.is_actionable and len(open_trades) < self.max_open_trades:
                # Capital suficiente?
                effective_size = min(self.trade_size, capital * 0.95)
                if effective_size < 1.0:
                    logger.debug("Capital insuficiente para novo trade")
                    result.equity_curve.append(capital)
                    result.total_candles_processed += 1
                    continue

                # Verificar se já existe trade na mesma direção
                same_direction = any(
                    t.signal_type == signal.signal_type.value
                    for t in open_trades
                )
                if same_direction:
                    result.equity_curve.append(capital)
                    result.total_candles_processed += 1
                    continue

                # Fechar trade oposto (inverter posição)
                opposite_direction = [
                    t for t in open_trades
                    if t.signal_type != signal.signal_type.value
                ]
                for t in opposite_direction:
                    t.close(current_price, current_time, "signal_exit")
                    capital += t.pnl
                    capital -= abs(t.size) * self.fee_pct
                    result.trades.append(t)
                    open_trades.remove(t)
                    logger.debug(f"Trade fechado por sinal oposto: {t}")

                # Abrir novo trade
                trade_counter += 1
                new_trade = BacktestTrade(
                    trade_id=trade_counter,
                    signal_type=signal.signal_type.value,
                    strategy=signal.strategy,
                    entry_price=current_price,
                    entry_time=current_time,
                    size=effective_size,
                )
                # Desconto de taxa de entrada
                capital -= effective_size * self.fee_pct
                open_trades.append(new_trade)
                logger.debug(
                    f"Novo trade: #{trade_counter} {signal.signal_type.value} "
                    f"@ {current_price:.4f} size={effective_size:.2f} | {signal.reason}"
                )

            result.equity_curve.append(capital)
            result.total_candles_processed += 1

        # ----------------------------------------------------------------
        # Fechar trades abertos no final dos dados
        # ----------------------------------------------------------------
        if open_trades:
            final_price = df["close"].iloc[-1]
            final_time = df.index[-1]
            for trade in open_trades:
                trade.close(final_price, final_time, "end_of_data")
                capital += trade.pnl
                capital -= abs(trade.size) * self.fee_pct
                result.trades.append(trade)
                logger.debug(f"Trade fechado no final dos dados: {trade}")

        # Atualizar capital final na equity curve
        if result.equity_curve:
            result.equity_curve[-1] = capital

        logger.info(
            f"Backtest concluído: {len(result.closed_trades)} trades | "
            f"PnL=${result.total_pnl:.2f} | "
            f"Capital final=${result.final_capital:.2f}"
        )

        return result

    def _check_stop_loss(self, trade: BacktestTrade, candle: pd.Series) -> bool:
        """
        Verifica se o stop-loss foi acionado para o trade no candle atual.

        Para BUY: stop se low do candle caiu X% abaixo do preço de entrada
        Para SELL: stop se high do candle subiu X% acima do preço de entrada

        Args:
            trade:  Trade aberto.
            candle: Candle atual (Series com open, high, low, close).

        Returns:
            True se stop-loss foi acionado.
        """
        if trade.signal_type == "BUY":
            stop_price = trade.entry_price * (1 - self.stop_loss_pct)
            return candle["low"] <= stop_price
        else:  # SELL
            stop_price = trade.entry_price * (1 + self.stop_loss_pct)
            return candle["high"] >= stop_price

    def _get_stop_price(self, trade: BacktestTrade) -> float:
        """Retorna o preço de stop para o trade."""
        if trade.signal_type == "BUY":
            return trade.entry_price * (1 - self.stop_loss_pct)
        else:
            return trade.entry_price * (1 + self.stop_loss_pct)
