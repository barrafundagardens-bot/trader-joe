"""
deploy/live_trading.py — Trading ao vivo via py-clob-client.

⚠️  AVISO CRÍTICO ⚠️
Este script usa CAPITAL REAL. Só execute após:
    1. Completar o security_checklist.md
    2. Testar paper trading por >= 2 semanas
    3. Usar uma carteira DEDICADA com no máximo $100-300
    4. Entender completamente cada linha deste código

Nunca execute como root ou em ambiente não seguro.

Funcionamento:
    1. Conectar ao CLOB via py-clob-client
    2. Obter dados históricos de preço (polling da API)
    3. A cada ciclo, gerar sinal via estratégia
    4. Avaliar via RiskManager
    5. Submeter limit order GTC se aprovado
    6. Monitorar e cancelar ordens antigas
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from typing import Optional

import pandas as pd
import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.base_strategy import Signal, SignalType
from strategies.fvg_multitf_strategy import FVGMultiTFStrategy
from strategies.macd_strategy import MACDStrategy
from strategies.rsi_strategy import RSIStrategy
from strategies.cvd_strategy import CVDStrategy
from bot.risk_manager import RiskManager
from bot.trader import Trader
from bot.manual_override import ManualOverrideManager

load_dotenv()

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/live_trading.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)


# ============================================================
# Live Trading Bot
# ============================================================

class LiveTradingBot:
    """
    Bot de trading ao vivo para Polymarket.

    Ciclo principal:
        1. Coletar preços recentes via API
        2. Gerar sinal
        3. Avaliar risco
        4. Submeter limit order
        5. Aguardar próximo ciclo

    Args:
        strategy_name:      Estratégia a usar.
        token_id:           Token ID do mercado (YES ou NO).
        condition_id:       Condition ID do mercado.
        poll_interval_sec:  Intervalo entre ciclos em segundos.
        min_data_points:    Número mínimo de preços para gerar sinal.
    """

    STRATEGY_MAP = {
        "fvg_multitf": FVGMultiTFStrategy,
        "macd": MACDStrategy,
        "rsi": RSIStrategy,
        "cvd": CVDStrategy,
    }

    def __init__(
        self,
        strategy_name: str = "fvg_multitf",
        token_id: Optional[str] = None,
        condition_id: Optional[str] = None,
        poll_interval_sec: int = 60,
        min_data_points: int = 200,
    ):
        self.token_id = token_id or os.getenv("TOKEN_ID_YES", "")
        self.condition_id = condition_id or os.getenv("MARKET_CONDITION_ID", "")
        self.poll_interval_sec = poll_interval_sec
        self.min_data_points = min_data_points
        self._running = False
        self._price_buffer: list = []
        self._current_order_id: Optional[str] = None
        self._bot_cancelled_last: bool = False  # True quando o bot mesmo cancelou
        self._cycle_count = 0

        # Controle de intervenções manuais
        self.override_manager = ManualOverrideManager()

        if not self.token_id:
            raise ValueError("TOKEN_ID_YES não configurado no .env")

        # Estratégia
        strategy_cls = self.STRATEGY_MAP.get(strategy_name)
        if not strategy_cls:
            raise ValueError(f"Estratégia desconhecida: {strategy_name}")
        self.strategy = strategy_cls()

        # Risk Manager
        self.risk_manager = RiskManager(
            initial_capital=float(os.getenv("INITIAL_CAPITAL", "100.0")),
            max_trade_size=float(os.getenv("MAX_TRADE_SIZE", "10.0")),
            max_loss_per_trade=float(os.getenv("MAX_LOSS_PER_TRADE", "0.10")),
            max_trades_per_day=int(os.getenv("MAX_TRADES_PER_DAY", "5")),
            emergency_drawdown=float(os.getenv("EMERGENCY_DRAWDOWN", "0.20")),
        )

        # Trader (live — não dry_run)
        self.trader = Trader(token_id=self.token_id, dry_run=False)

        logger.warning(
            "=" * 60 + "\n"
            "⚠️  MODO LIVE ATIVADO — CAPITAL REAL EM RISCO  ⚠️\n"
            f"Estratégia: {self.strategy.name}\n"
            f"Token: {self.token_id}\n"
            f"Capital: ${os.getenv('INITIAL_CAPITAL', '?')}\n"
            "CTRL+C para parar com segurança.\n"
            + "=" * 60
        )

        # Registrar handler de shutdown seguro
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def start(self) -> None:
        """Inicia o loop de trading."""
        logger.info("Conectando ao CLOB...")
        if not self.trader.connect():
            logger.critical("Falha na conexão com CLOB. Abortando.")
            return

        # Verificar saldo inicial
        balance = self.trader.get_balance()
        if balance is not None:
            logger.info(f"Saldo USDC na carteira: ${balance:.2f}")
        else:
            logger.warning("Não foi possível verificar saldo. Continuando...")

        self._running = True
        logger.info(f"Bot iniciado. Ciclo a cada {self.poll_interval_sec}s.")

        while self._running:
            try:
                self._cycle()
                self._cycle_count += 1
                time.sleep(self.poll_interval_sec)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Erro no ciclo {self._cycle_count}: {e}", exc_info=True)
                logger.info("Aguardando 30s antes de tentar novamente...")
                time.sleep(30)

        self._shutdown()

    def _cycle(self) -> None:
        """Executa um ciclo completo do bot."""
        # 1. Obter preço atual
        price = self.trader.get_current_price()
        if price is None:
            logger.warning("Preço indisponível — pulando ciclo.")
            return

        # 2. Detectar cancelamento/venda manual
        #    Se o bot registrou uma ordem mas ela sumiu sem que o bot
        #    a tenha cancelado, o usuário interveio manualmente.
        self._detect_manual_intervention()

        # 3. Verificar se o token atual está bloqueado por intervenção manual
        blocked, block_reason = self.override_manager.is_blocked(self.token_id)
        if blocked:
            logger.info(f"Ciclo {self._cycle_count} ignorado — {block_reason}")
            return

        # 4. Adicionar ao buffer de preços
        self._price_buffer.append({
            "timestamp": datetime.now(),
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1.0,
        })

        # Manter apenas os últimos min_data_points pontos
        if len(self._price_buffer) > self.min_data_points * 2:
            self._price_buffer = self._price_buffer[-self.min_data_points * 2:]

        # 5. Construir DataFrame
        if len(self._price_buffer) < self.strategy.min_candles:
            logger.debug(
                f"Buffer insuficiente: {len(self._price_buffer)}/{self.strategy.min_candles}"
            )
            return

        df = pd.DataFrame(self._price_buffer)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp").sort_index()

        # 6. Gerar sinal
        signal = self.strategy.generate_signal(df)

        if not signal.is_actionable:
            logger.debug(f"Ciclo {self._cycle_count}: {signal.reason}")
            return

        logger.info(f"Sinal gerado: {signal}")

        # 7. Avaliar risco
        decision = self.risk_manager.evaluate(signal)

        if not decision.approved:
            logger.info(f"Sinal rejeitado pelo RiskManager: {decision.reason}")
            return

        # 8. Cancelar ordem anterior se existir (cancelamento pelo bot)
        if self._current_order_id:
            logger.info(f"Cancelando ordem anterior: {self._current_order_id}")
            self._bot_cancelled_last = True
            self.trader.cancel_order(self._current_order_id)
            self._current_order_id = None

        # 9. Submeter nova limit order
        self._bot_cancelled_last = False
        order = self.trader.place_limit_order(
            signal=signal,
            size=decision.adjusted_size,
        )

        if order:
            self._current_order_id = order.get("order_id")
            self.risk_manager.register_trade_open(signal, decision.adjusted_size)
            logger.info(
                f"✅ Ordem submetida: {order} | "
                f"stop_price={decision.stop_price:.4f}"
            )

            # Salvar estado
            self._save_state(signal, order, decision)
        else:
            logger.error("Falha ao submeter ordem.")

    def _detect_manual_intervention(self) -> None:
        """
        Detecta se o usuário cancelou ou vendeu uma posição manualmente.

        Lógica:
            - O bot tem um order_id registrado (_current_order_id)
            - O bot NÃO foi quem cancelou (_bot_cancelled_last = False)
            - A ordem não existe mais na exchange
            → Intervenção manual: bloqueia o token até o mercado resolver.
        """
        if not self._current_order_id:
            return
        if self._bot_cancelled_last:
            # O bot mesmo cancelou no ciclo anterior — não é intervenção manual
            self._bot_cancelled_last = False
            return

        still_open = self.trader.order_exists(self._current_order_id)
        if not still_open:
            logger.warning(
                f"⚠️  Intervenção manual detectada! "
                f"Ordem {self._current_order_id[:24]}... sumiu sem ação do bot. "
                f"Bloqueando token para evitar reentrada."
            )
            self.override_manager.add_block(
                token_id=self.token_id,
                reason="cancelamento/venda manual detectado pelo bot",
                condition_id=self.condition_id or None,
            )
            self._current_order_id = None

    def _save_state(self, signal: Signal, order: dict, decision) -> None:
        """Salva o estado atual do bot em arquivo JSON."""
        state = {
            "timestamp": datetime.now().isoformat(),
            "cycle": self._cycle_count,
            "signal": {
                "type": signal.signal_type.value,
                "price": signal.price,
                "reason": signal.reason,
            },
            "order": order,
            "risk_status": self.risk_manager.get_status(),
        }
        with open("live_trades.json", "w") as f:
            json.dump(state, f, indent=2, default=str)

    def _handle_shutdown(self, signum, frame) -> None:
        """Handler de shutdown seguro via CTRL+C ou SIGTERM."""
        logger.info("\nSinal de shutdown recebido. Encerrando com segurança...")
        self._running = False

    def _shutdown(self) -> None:
        """Encerra o bot de forma segura."""
        logger.info("Cancelando todas as ordens abertas...")
        self.trader.cancel_all_orders()

        self.risk_manager.print_status()
        logger.info("Bot encerrado com segurança.")


# ============================================================
# Entry Point
# ============================================================

def main():
    """Ponto de entrada para o live trading."""
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket Live Trading Bot")
    parser.add_argument(
        "--strategy",
        default=os.getenv("STRATEGY", "fvg_multitf"),
        choices=["fvg_multitf", "macd", "rsi", "cvd"],
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Intervalo entre ciclos em segundos",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirmar explicitamente que você quer usar capital real",
    )
    args = parser.parse_args()

    if not args.confirm:
        print("=" * 60)
        print("⚠️  AVISO: Este script usa CAPITAL REAL.")
        print("Execute com --confirm para confirmar.")
        print("Execute paper_trading.py primeiro para testar.")
        print("=" * 60)
        sys.exit(0)

    bot = LiveTradingBot(
        strategy_name=args.strategy,
        poll_interval_sec=args.interval,
    )
    bot.start()


if __name__ == "__main__":
    main()
