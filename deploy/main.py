"""
deploy/main.py — Ponto de entrada principal do Polymarket Trading Bot.

Centraliza o acesso a todos os modos de operação:
    --mode backtest:     Executa backtesting nos dados históricos
    --mode paper:        Paper trading 100% offline
    --mode live:         Trading ao vivo (capital real)

Uso:
    python deploy/main.py --mode backtest --strategy fvg_multitf
    python deploy/main.py --mode paper --strategy macd --capital 100
    python deploy/main.py --mode live --strategy fvg_multitf --confirm
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

# Configurar logging antes de qualquer import
os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)
os.makedirs("results", exist_ok=True)

log_level = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            f"logs/main_{datetime.now().strftime('%Y%m%d')}.log", mode="a"
        ),
    ],
)
logger = logging.getLogger(__name__)


# ============================================================
# Handlers de cada modo
# ============================================================

def run_backtest(args: argparse.Namespace) -> None:
    """Executa backtesting completo com métricas."""
    from backtesting.engine import BacktestEngine
    from backtesting.metrics import BacktestMetrics
    from strategies import (
        FVGMultiTFStrategy, MACDStrategy, RSIStrategy, CVDStrategy
    )

    from strategies.spread_strategy import SpreadFarmingStrategy
    from strategies.copytrade_strategy import CopytradeStrategy
    from strategies.news_strategy import NewsStrategy
    from strategies.ensemble_strategy import EnsembleStrategy
    from strategies.favorites_strategy import FavoritesStrategy
    from strategies.whale_following_strategy import WhaleFollowingStrategy
    from strategies.arbitrage_strategy import ArbitrageStrategy

    strategy_map = {
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

    strategy_cls = strategy_map.get(args.strategy)
    if not strategy_cls:
        logger.error(f"Estratégia desconhecida: {args.strategy}")
        sys.exit(1)

    import pandas as pd

    # Carregar dados
    data_path = args.data or os.getenv("POLY_DATA_PATH", "./data/poly_data.csv")
    if not os.path.exists(data_path):
        logger.error(
            f"Arquivo de dados não encontrado: {data_path}\n"
            "Baixe os dados em: https://github.com/warproxxx/poly_data\n"
            "e configure POLY_DATA_PATH no .env"
        )
        sys.exit(1)

    logger.info(f"Carregando dados: {data_path}")
    df = pd.read_csv(data_path)
    df.columns = [c.lower().strip() for c in df.columns]

    # Detectar coluna de timestamp
    ts_col = next(
        (c for c in df.columns if c in ("timestamp", "time", "date", "datetime", "t")),
        None
    )
    if ts_col:
        df[ts_col] = pd.to_datetime(df[ts_col])
        df = df.set_index(ts_col)
    df = df.sort_index()

    # Preencher volume se ausente
    if "volume" not in df.columns:
        df["volume"] = 1.0

    logger.info(
        f"Dados carregados: {len(df)} candles | "
        f"{df.index[0]} → {df.index[-1]}"
    )

    # Inicializar e executar engine
    strategy = strategy_cls()
    engine = BacktestEngine(
        strategy=strategy,
        initial_capital=args.capital,
        trade_size=args.trade_size,
        stop_loss_pct=args.stop_loss,
        warmup_candles=strategy.min_candles,
    )

    logger.info(f"Iniciando backtest: {strategy.name}")
    result = engine.run(df)

    # Exibir métricas
    metrics = BacktestMetrics(result)
    metrics.print_summary()

    # Salvar resultados
    import json
    results_path = (
        f"results/backtest_{strategy.name}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    with open(results_path, "w") as f:
        json.dump(
            {
                "mode": "backtest",
                "strategy": strategy.name,
                "timestamp": datetime.now().isoformat(),
                "metrics": metrics.to_dict(),
                "trades": [
                    {
                        "id": t.trade_id,
                        "type": t.signal_type,
                        "entry": t.entry_price,
                        "exit": t.exit_price,
                        "pnl": t.pnl,
                        "reason": t.exit_reason,
                    }
                    for t in result.closed_trades
                ],
            },
            f,
            indent=2,
            default=str,
        )
    logger.info(f"Resultados salvos em: {results_path}")


def run_paper(args: argparse.Namespace) -> None:
    """Executa paper trading offline."""
    from deploy.paper_trading import PaperTrader

    trader = PaperTrader(
        strategy_name=args.strategy,
        initial_capital=args.capital,
        trade_size=args.trade_size,
        stop_loss_pct=args.stop_loss,
        max_trades_per_day=20,  # Aumentado para backtesting — limite de 5/dia é para live
    )

    data_path = args.data or os.getenv("POLY_DATA_PATH", "./data/poly_data.csv")
    df = trader.load_data(data_path)
    trader.run(df)


def run_live(args: argparse.Namespace) -> None:
    """Executa trading ao vivo."""
    if not args.confirm:
        print("=" * 60)
        print("⚠️  AVISO CRÍTICO: Modo LIVE usa CAPITAL REAL.")
        print("")
        print("Antes de continuar, verifique:")
        print("  1. Completou security_checklist.md?")
        print("  2. Testou paper trading por 2+ semanas?")
        print("  3. Está usando carteira DEDICADA?")
        print("  4. Capital máximo de $100-300?")
        print("")
        print("Execute com --confirm para confirmar.")
        print("=" * 60)
        sys.exit(0)

    from deploy.live_trading import LiveTradingBot

    bot = LiveTradingBot(
        strategy_name=args.strategy,
        poll_interval_sec=args.interval,
    )
    bot.start()


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Trading Bot — Modo multi-estratégia",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  Backtest FVG MultiTF:
    python deploy/main.py --mode backtest --strategy fvg_multitf

  Paper trading MACD:
    python deploy/main.py --mode paper --strategy macd --capital 100

  Live trading (com confirmação):
    python deploy/main.py --mode live --strategy fvg_multitf --confirm

Estratégias disponíveis:
  fvg_multitf  Fair Value Gap Multi-Timeframe 4H+15M (recomendada)
  macd         MACD(12,26,9) com filtro de tendência
  rsi          RSI(14) com filtro de volume
  cvd          Cumulative Volume Delta

⚠️  AVISO: Mais de 90% dos traders de predição perdem dinheiro.
   Comece SEMPRE com paper trading. Nunca invista mais do que pode perder.
        """,
    )

    parser.add_argument(
        "--mode",
        required=True,
        choices=["backtest", "paper", "live"],
        help="Modo de operação",
    )
    parser.add_argument(
        "--strategy",
        default="fvg_multitf",
        choices=["fvg_multitf", "macd", "rsi", "cvd", "spread", "copytrade", "news", "ensemble", "favorites", "whale", "arbitrage"],
        help="Estratégia de trading (padrão: fvg_multitf)",
    )
    parser.add_argument(
        "--data",
        default=None,
        help="Caminho para CSV de dados (padrão: POLY_DATA_PATH do .env)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=float(os.getenv("INITIAL_CAPITAL", "100.0")),
        help="Capital inicial em USDC (padrão: 100.0)",
    )
    parser.add_argument(
        "--trade-size",
        type=float,
        dest="trade_size",
        default=float(os.getenv("MAX_TRADE_SIZE", "10.0")),
        help="Tamanho por trade em USDC (padrão: 10.0)",
    )
    parser.add_argument(
        "--stop-loss",
        type=float,
        dest="stop_loss",
        default=float(os.getenv("MAX_LOSS_PER_TRADE", "0.10")),
        help="Stop-loss em porcentagem (padrao: 0.10 = 10 porcento)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Intervalo do ciclo live em segundos (padrão: 60)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirmar uso de capital real no modo live",
    )

    args = parser.parse_args()

    logger.info(
        f"Iniciando: mode={args.mode} strategy={args.strategy} "
        f"capital=${args.capital} trade_size=${args.trade_size}"
    )

    mode_handlers = {
        "backtest": run_backtest,
        "paper": run_paper,
        "live": run_live,
    }

    handler = mode_handlers[args.mode]
    handler(args)


if __name__ == "__main__":
    main()
