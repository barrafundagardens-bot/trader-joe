"""
deploy/paper_trading.py — Simulação de trading 100% offline.

O paper trading simula execuções completas usando dados históricos locais
(do poly_data) SEM conectar à API real do Polymarket.

Características:
    - Zero conexão com API real
    - Simula limit orders com preenchimento no próximo candle
    - Usa RiskManager real (mesmo código do live trading)
    - Gera logs detalhados de cada sinal e execução
    - Salva resultados em paper_trades.json para análise posterior

Fluxo:
    1. Carregar dados do poly_data (CSV local)
    2. Construir candles 15M e 4H via pandas resample
    3. Iterar candle a candle (simula tempo real)
    4. Gerar sinal via estratégia
    5. Avaliar via RiskManager
    6. "Executar" a limit order simulada
    7. Registrar PnL e atualizar portfólio
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import List, Optional

import pandas as pd
import numpy as np
from dotenv import load_dotenv

# Adicionar raiz do projeto ao path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.base_strategy import Signal, SignalType
from strategies.fvg_multitf_strategy import FVGMultiTFStrategy
from strategies.macd_strategy import MACDStrategy
from strategies.rsi_strategy import RSIStrategy
from strategies.cvd_strategy import CVDStrategy
from strategies.spread_strategy import SpreadFarmingStrategy
from strategies.copytrade_strategy import CopytradeStrategy
from strategies.news_strategy import NewsStrategy
from strategies.ensemble_strategy import EnsembleStrategy
from strategies.favorites_strategy import FavoritesStrategy
from strategies.whale_following_strategy import WhaleFollowingStrategy
from strategies.arbitrage_strategy import ArbitrageStrategy
from bot.risk_manager import RiskManager
from backtesting.metrics import BacktestMetrics
from backtesting.engine import BacktestTrade, BacktestResult

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/paper_trading.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)


# ============================================================
# Data Classes
# ============================================================

class SimulatedOrder:
    """Representa uma limit order simulada no paper trading."""

    def __init__(
        self,
        order_id: str,
        side: str,
        price: float,
        size: float,
        token_id: str,
        timestamp: pd.Timestamp,
    ):
        self.order_id = order_id
        self.side = side
        self.price = price
        self.size = size
        self.token_id = token_id
        self.timestamp = timestamp
        self.is_filled = False
        self.fill_price: Optional[float] = None
        self.fill_timestamp: Optional[pd.Timestamp] = None

    def try_fill(self, candle: pd.Series, candle_time: pd.Timestamp) -> bool:
        """
        Tenta preencher a ordem com o candle fornecido.

        Lógica de preenchimento realista:
            BUY: preenche se o low do candle <= preço da ordem
            SELL: preenche se o high do candle >= preço da ordem

        Args:
            candle:      Candle OHLCV.
            candle_time: Timestamp do candle.

        Returns:
            True se a ordem foi preenchida.
        """
        if self.is_filled:
            return True

        if self.side == "BUY" and candle["low"] <= self.price:
            self.is_filled = True
            self.fill_price = self.price  # Preço garantido (limit)
            self.fill_timestamp = candle_time
            return True

        if self.side == "SELL" and candle["high"] >= self.price:
            self.is_filled = True
            self.fill_price = self.price
            self.fill_timestamp = candle_time
            return True

        return False

    def __repr__(self) -> str:
        status = f"FILLED@{self.fill_price:.4f}" if self.is_filled else "PENDING"
        return f"SimOrder({self.side} {self.size:.2f} @ {self.price:.4f} [{status}])"


# ============================================================
# Paper Trading Engine
# ============================================================

class PaperTrader:
    """
    Simulador de paper trading 100% offline.

    Simula o ciclo completo de trading usando dados históricos locais.
    Não conecta à API real. Ideal para validar estratégias antes de
    colocar capital real.

    Args:
        strategy_name:   Nome da estratégia ('fvg_multitf', 'macd', 'rsi', 'cvd').
        initial_capital: Capital inicial simulado em USDC.
        trade_size:      Tamanho por trade em USDC.
        stop_loss_pct:   Stop-loss em % (0.10 = 10%).
        max_trades_per_day: Limite diário de trades.
        speed:           Velocidade de simulação (0.0 = máxima velocidade).
    """

    STRATEGY_MAP = {
        "fvg_multitf": FVGMultiTFStrategy,
        "macd": MACDStrategy,
        "rsi": RSIStrategy,
        "cvd": CVDStrategy,
        "spread": SpreadFarmingStrategy,
        "copytrade": CopytradeStrategy,
        "news": NewsStrategy,
        "ensemble": EnsembleStrategy,
        "favorites": FavoritesStrategy,
        "whale": WhaleFollowingStrategy,
        "arbitrage": ArbitrageStrategy,
    }

    def __init__(
        self,
        strategy_name: str = "fvg_multitf",
        initial_capital: float = 100.0,
        trade_size: float = 10.0,
        stop_loss_pct: float = 0.10,
        max_trades_per_day: int = 15,
        speed: float = 0.0,
    ):
        self.initial_capital = initial_capital
        self.trade_size = trade_size
        self.stop_loss_pct = stop_loss_pct
        self.speed = speed

        # Carregar estratégia
        strategy_cls = self.STRATEGY_MAP.get(strategy_name)
        if not strategy_cls:
            raise ValueError(
                f"Estratégia '{strategy_name}' não encontrada. "
                f"Opções: {list(self.STRATEGY_MAP.keys())}"
            )
        self.strategy = strategy_cls()
        logger.info(f"Paper trading iniciado com estratégia: {self.strategy.name}")

        # RiskManager real
        self.risk_manager = RiskManager(
            initial_capital=initial_capital,
            max_trade_size=trade_size,
            max_loss_per_trade=stop_loss_pct,
            max_trades_per_day=max_trades_per_day,
            emergency_drawdown=float(os.getenv("EMERGENCY_DRAWDOWN", "0.20")),
        )

        # Estado do portfólio simulado
        self._capital = initial_capital
        self._trades: List[BacktestTrade] = []
        self._equity_curve: List[float] = [initial_capital]
        self._open_order: Optional[SimulatedOrder] = None
        self._open_trade: Optional[BacktestTrade] = None
        self._trade_counter = 0
        self._results_file = "paper_trades.json"

        os.makedirs("logs", exist_ok=True)
        os.makedirs("results", exist_ok=True)

    def load_data(self, csv_path: str) -> pd.DataFrame:
        """
        Carrega dados CSV do poly_data.

        O CSV deve ter colunas: timestamp, open, high, low, close, volume
        ou similar. Adapta automaticamente nomes comuns de colunas.

        Args:
            csv_path: Caminho para o arquivo CSV.

        Returns:
            DataFrame OHLCV com DatetimeIndex.
        """
        logger.info(f"Carregando dados de: {csv_path}")

        if not os.path.exists(csv_path):
            raise FileNotFoundError(
                f"Arquivo não encontrado: {csv_path}\n"
                "Baixe os dados em: https://github.com/warproxxx/poly_data"
            )

        df = pd.read_csv(csv_path)
        logger.info(f"Dados carregados: {len(df)} linhas, colunas={list(df.columns)}")

        # Normalizar nomes de colunas
        df.columns = [c.lower().strip() for c in df.columns]

        col_map = {}
        for col in df.columns:
            if col in ("timestamp", "time", "date", "datetime", "t"):
                col_map[col] = "timestamp"
            elif col in ("open", "o"):
                col_map[col] = "open"
            elif col in ("high", "h"):
                col_map[col] = "high"
            elif col in ("low", "l"):
                col_map[col] = "low"
            elif col in ("close", "c", "price", "last"):
                col_map[col] = "close"
            elif col in ("volume", "vol", "v"):
                col_map[col] = "volume"

        df = df.rename(columns=col_map)

        if "timestamp" not in df.columns:
            raise ValueError(
                "Coluna de timestamp não encontrada. "
                "Esperado: 'timestamp', 'time', 'date', 'datetime' ou 't'"
            )

        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp").sort_index()

        # Preencher volume zerado se não existir
        if "volume" not in df.columns:
            df["volume"] = 1.0
            logger.warning("Coluna 'volume' não encontrada. Usando volume=1.0")

        # Remover linhas com dados essenciais ausentes
        df = df.dropna(subset=["open", "high", "low", "close"])

        logger.info(
            f"Dados processados: {len(df)} candles | "
            f"{df.index[0]} → {df.index[-1]}"
        )
        return df

    def run(self, df: pd.DataFrame, verbose: bool = True) -> dict:
        """
        Executa a simulação de paper trading candle a candle.

        Args:
            df:      DataFrame OHLCV processado pelo load_data().
            verbose: Se True, imprime progresso a cada 100 candles.

        Returns:
            Dicionário com resultados completos do paper trading.
        """
        min_warmup = self.strategy.min_candles
        logger.info(
            f"Iniciando simulação: {len(df)} candles | warm-up={min_warmup}"
        )

        for i in range(min_warmup, len(df)):
            current_candle = df.iloc[i]
            current_time = df.index[i]
            current_price = current_candle["close"]

            # --------------------------------------------------------
            # 1. Tentar preencher ordem pendente
            # --------------------------------------------------------
            if self._open_order and not self._open_order.is_filled:
                filled = self._open_order.try_fill(current_candle, current_time)
                if filled and self._open_trade:
                    logger.info(
                        f"✅ Ordem preenchida: {self._open_order} → "
                        f"Trade aberto @ {self._open_order.fill_price:.4f}"
                    )

            # --------------------------------------------------------
            # 2. Verificar stop-loss do trade aberto
            # --------------------------------------------------------
            if self._open_trade and self._open_order and self._open_order.is_filled:
                if self._check_stop_loss(self._open_trade, current_candle):
                    stop_price = self._get_stop_price(self._open_trade)
                    pnl = self._close_trade(
                        self._open_trade, stop_price, current_time, "stop_loss"
                    )
                    self.risk_manager.register_trade_close(pnl, self._open_trade.size)
                    logger.warning(
                        f"🛑 Stop-loss: trade #{self._open_trade.trade_id} "
                        f"PnL={pnl:+.3f}"
                    )
                    self._open_trade = None
                    self._open_order = None

            # --------------------------------------------------------
            # 3. Gerar sinal da estratégia (janela até candle atual)
            # --------------------------------------------------------
            window = df.iloc[:i + 1]
            signal = self.strategy.generate_signal(window)

            # --------------------------------------------------------
            # 4. Processar sinal se não houver trade aberto
            # --------------------------------------------------------
            if signal.is_actionable and self._open_trade is None:
                decision = self.risk_manager.evaluate(signal)

                if decision.approved:
                    # Cancelar ordem pendente anterior se existir
                    if self._open_order:
                        logger.info(f"Cancelando ordem pendente: {self._open_order}")
                        self._open_order = None

                    # Criar ordem simulada
                    self._trade_counter += 1
                    order = SimulatedOrder(
                        order_id=f"paper_{self._trade_counter}",
                        side=signal.signal_type.value,
                        price=signal.price,
                        size=decision.adjusted_size,
                        token_id=os.getenv("TOKEN_ID_YES", "unknown"),
                        timestamp=current_time,
                    )
                    self._open_order = order

                    # Criar trade associado
                    trade = BacktestTrade(
                        trade_id=self._trade_counter,
                        signal_type=signal.signal_type.value,
                        strategy=signal.strategy,
                        entry_price=signal.price,
                        entry_time=current_time,
                        size=decision.adjusted_size,
                    )
                    self._open_trade = trade
                    self.risk_manager.register_trade_open(signal, decision.adjusted_size)

                    logger.info(
                        f"📋 Ordem criada #{self._trade_counter}: "
                        f"{signal.signal_type.value} @ {signal.price:.4f} "
                        f"size={decision.adjusted_size:.2f} | {signal.reason}"
                    )
                else:
                    if verbose and i % 500 == 0:
                        logger.debug(f"Sinal rejeitado: {decision.reason}")

            # --------------------------------------------------------
            # 5. Sinal oposto fecha trade aberto
            # --------------------------------------------------------
            elif (signal.is_actionable and self._open_trade is not None
                  and self._open_order and self._open_order.is_filled
                  and signal.signal_type.value != self._open_trade.signal_type):
                pnl = self._close_trade(
                    self._open_trade, current_price, current_time, "signal_exit"
                )
                self.risk_manager.register_trade_close(pnl, self._open_trade.size)
                logger.info(
                    f"↔️ Trade fechado por sinal oposto: "
                    f"#{self._open_trade.trade_id} PnL={pnl:+.3f}"
                )
                self._open_trade = None
                self._open_order = None

            # --------------------------------------------------------
            # 6. Atualizar equity curve
            # --------------------------------------------------------
            unrealized = 0.0
            if self._open_trade and self._open_order and self._open_order.is_filled:
                unrealized = self._calculate_unrealized_pnl(
                    self._open_trade, current_price
                )
            self._equity_curve.append(self._capital + unrealized)

            # Progresso
            if verbose and i % 200 == 0:
                progress = (i - min_warmup) / max(len(df) - min_warmup, 1) * 100
                logger.info(
                    f"Progresso: {progress:.0f}% | candle={i}/{len(df)} | "
                    f"capital=${self._capital:.2f} | trades={self._trade_counter}"
                )

            # Velocidade de simulação (0 = máxima velocidade)
            if self.speed > 0:
                time.sleep(self.speed)

        # --------------------------------------------------------
        # 7. Fechar trade aberto no fim dos dados
        # --------------------------------------------------------
        if self._open_trade and self._open_order and self._open_order.is_filled:
            final_price = df["close"].iloc[-1]
            pnl = self._close_trade(
                self._open_trade, final_price, df.index[-1], "end_of_data"
            )
            self.risk_manager.register_trade_close(pnl, self._open_trade.size)
            logger.info(f"Trade fechado no fim dos dados: PnL={pnl:+.3f}")

        return self._build_results(df)

    def _close_trade(
        self,
        trade: BacktestTrade,
        exit_price: float,
        exit_time: pd.Timestamp,
        reason: str,
    ) -> float:
        """Fecha um trade, calcula PnL e atualiza capital."""
        trade.close(exit_price, exit_time, reason)
        self._capital += trade.pnl
        self._trades.append(trade)
        return trade.pnl

    def _check_stop_loss(self, trade: BacktestTrade, candle: pd.Series) -> bool:
        """Verifica stop-loss."""
        if trade.signal_type == "BUY":
            return candle["low"] <= trade.entry_price * (1 - self.stop_loss_pct)
        else:
            return candle["high"] >= trade.entry_price * (1 + self.stop_loss_pct)

    def _get_stop_price(self, trade: BacktestTrade) -> float:
        """Retorna preço de stop."""
        if trade.signal_type == "BUY":
            return trade.entry_price * (1 - self.stop_loss_pct)
        return trade.entry_price * (1 + self.stop_loss_pct)

    @staticmethod
    def _calculate_unrealized_pnl(trade: BacktestTrade, current_price: float) -> float:
        """Calcula PnL não realizado da posição aberta."""
        if trade.signal_type == "BUY":
            return trade.size * (current_price - trade.entry_price) / trade.entry_price
        else:
            return trade.size * (trade.entry_price - current_price) / trade.entry_price

    def _build_results(self, df: pd.DataFrame) -> dict:
        """Constrói o dicionário de resultados finais."""
        result = BacktestResult(
            strategy_name=self.strategy.name,
            initial_capital=self.initial_capital,
            trades=self._trades,
            equity_curve=self._equity_curve,
        )
        metrics = BacktestMetrics(result)
        metrics.print_summary()

        results_dict = {
            "mode": "paper_trading",
            "strategy": self.strategy.name,
            "timestamp": datetime.now().isoformat(),
            "data_start": str(df.index[0]),
            "data_end": str(df.index[-1]),
            "metrics": metrics.to_dict(),
            "trades": [
                {
                    "id": t.trade_id,
                    "type": t.signal_type,
                    "entry": t.entry_price,
                    "exit": t.exit_price,
                    "entry_time": str(t.entry_time),
                    "exit_time": str(t.exit_time),
                    "size": t.size,
                    "pnl": t.pnl,
                    "reason": t.exit_reason,
                }
                for t in self._trades
            ],
        }

        # Salvar resultados
        results_path = f"results/paper_{self.strategy.name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(results_path, "w") as f:
            json.dump(results_dict, f, indent=2, default=str)
        logger.info(f"Resultados salvos em: {results_path}")

        return results_dict


# ============================================================
# Entry Point
# ============================================================

def main():
    """Ponto de entrada para execução direta do paper trading."""
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket Paper Trading Bot")
    parser.add_argument(
        "--strategy",
        default="fvg_multitf",
        choices=["fvg_multitf", "macd", "rsi", "cvd"],
        help="Estratégia de trading",
    )
    parser.add_argument(
        "--data",
        default=os.getenv("POLY_DATA_PATH", "./data/poly_data.csv"),
        help="Caminho para o arquivo CSV de dados",
    )
    parser.add_argument("--capital", type=float, default=100.0)
    parser.add_argument("--trade-size", type=float, default=10.0)
    parser.add_argument("--stop-loss", type=float, default=0.10)
    args = parser.parse_args()

    trader = PaperTrader(
        strategy_name=args.strategy,
        initial_capital=args.capital,
        trade_size=args.trade_size,
        stop_loss_pct=args.stop_loss,
    )

    df = trader.load_data(args.data)
    results = trader.run(df)

    print(f"\nPaper trading concluído: {results['metrics']['total_trades']} trades")
    print(f"PnL total: ${results['metrics']['total_pnl']:.2f}")


if __name__ == "__main__":
    main()
