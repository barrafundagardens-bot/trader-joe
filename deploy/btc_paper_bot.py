"""
deploy/btc_paper_bot.py — BTC 15-min UP/DOWN paper trading bot (dry-run).

Estratégia: ensemble por votação (MACD + RSI + Spike sobre candles BTC 1m
da Binance) decide entrada YES (UP) ou NO (DOWN) no mercado BTC 15-min
ativo do Polymarket. Posições simuladas, sem capital real, sem ordens reais.

Capital: $50 simulado | Tamanho/trade: $5 | Limite: 5 trades/dia
Filtro de odds: 0.40 ≤ yes_price ≤ 0.65 (zona de incerteza)

Uso:
    python deploy/btc_paper_bot.py                  # loop contínuo (60s)
    python deploy/btc_paper_bot.py --once           # roda 1 ciclo e sai
    python deploy/btc_paper_bot.py --interval 30    # custom poll interval
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal as os_signal
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from bot.binance_feed import BinanceFeed
from bot.polymarket_btc_feed import BTCMarket, PolymarketBTCFeed
from bot.risk_manager import RiskManager
from strategies.base_strategy import Signal, SignalType
from strategies.btc_spike_strategy import BTCSpikeStrategy
from strategies.macd_strategy import MACDStrategy
from strategies.rsi_strategy import RSIStrategy

load_dotenv()

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "btc_paper.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)

DEFAULT_CAPITAL = 50.0
DEFAULT_TRADE_SIZE = 5.0
DEFAULT_MAX_TRADES_DAY = 5
DEFAULT_MIN_ODDS = 0.40
DEFAULT_MAX_ODDS = 0.65
DEFAULT_MIN_CONSENSUS = 2
DEFAULT_POLL_SECONDS = 60

TRADES_FILE = LOG_DIR / "btc_paper_trades.jsonl"


@dataclass
class PaperPosition:
    market_slug: str
    market_question: str
    side: str                       # "YES" (UP) ou "NO" (DOWN)
    entry_odd: float                # preço Polymarket no momento da entrada
    size_usd: float
    shares: float
    entry_time: str                 # ISO UTC
    market_end_time: str            # ISO UTC
    btc_at_entry: float             # preço BTC quando abrimos
    consensus_strategies: List[str] = field(default_factory=list)
    status: str = "open"            # open | won | lost | error
    btc_at_close: Optional[float] = None
    exit_value: Optional[float] = None
    pnl: Optional[float] = None


class BTCPaperBot:
    """Orquestrador dry-run do bot BTC 15-min."""

    def __init__(
        self,
        capital: float = DEFAULT_CAPITAL,
        trade_size: float = DEFAULT_TRADE_SIZE,
        max_trades_per_day: int = DEFAULT_MAX_TRADES_DAY,
        min_odds: float = DEFAULT_MIN_ODDS,
        max_odds: float = DEFAULT_MAX_ODDS,
        min_consensus: int = DEFAULT_MIN_CONSENSUS,
    ):
        self.min_odds = min_odds
        self.max_odds = max_odds
        self.min_consensus = min_consensus

        self.binance = BinanceFeed()
        self.polymarket = PolymarketBTCFeed()

        self.risk = RiskManager(
            initial_capital=capital,
            max_trade_size=trade_size,
            max_loss_per_trade=0.30,
            max_trades_per_day=max_trades_per_day,
            emergency_drawdown=0.20,
        )

        self.strategies = [
            MACDStrategy(),
            RSIStrategy(),
            BTCSpikeStrategy(),
        ]

        self.open_positions: List[PaperPosition] = []
        self._running = True

    def run(self, interval: int = DEFAULT_POLL_SECONDS, once: bool = False) -> None:
        os_signal.signal(os_signal.SIGINT, self._stop)
        os_signal.signal(os_signal.SIGTERM, self._stop)

        logger.info("=" * 60)
        logger.info("  BTC 15-min PAPER BOT — DRY RUN")
        logger.info("=" * 60)
        logger.info(
            f"Capital: ${self.risk.current_capital:.2f} | "
            f"Trade size: ${self.risk.max_trade_size:.2f} | "
            f"Max/dia: {self.risk.max_trades_per_day} | "
            f"Odds: [{self.min_odds:.2f}, {self.max_odds:.2f}]"
        )
        logger.info(f"Estratégias: {[s.name for s in self.strategies]}")
        logger.info(f"Poll: {interval}s | Modo: {'ONCE' if once else 'LOOP'}")
        logger.info("=" * 60)

        while self._running:
            try:
                self._tick()
            except Exception as e:
                logger.exception(f"Erro no ciclo: {e}")

            if once:
                break

            for _ in range(interval):
                if not self._running:
                    break
                time.sleep(1)

        self._log_summary()

    def _stop(self, *_args) -> None:
        logger.info("Sinal de parada recebido. Encerrando após este ciclo...")
        self._running = False

    def _tick(self) -> None:
        klines = self.binance.fetch_klines(interval="1m", limit=200)
        if klines.empty:
            logger.warning("Sem dados Binance — pulando ciclo")
            return

        btc_price = float(klines["close"].iloc[-1])
        logger.info(f"[tick] BTC = ${btc_price:,.2f}")

        self._settle_expired_positions()

        market = self.polymarket.find_active_market()
        if market is None:
            logger.info("[tick] Nenhum mercado BTC 15-min ativo agora")
            return

        if any(p.market_slug == market.slug for p in self.open_positions):
            logger.info(f"[tick] Já tenho posição em '{market.slug}' — aguardando expirar")
            return

        if self.risk.is_emergency_mode:
            logger.warning("[tick] RiskManager em modo de emergência — sem novas entradas")
            return

        if self.risk.trades_today >= self.risk.max_trades_per_day:
            logger.info(
                f"[tick] Limite diário atingido ({self.risk.trades_today}/"
                f"{self.risk.max_trades_per_day}) — aguardando reset"
            )
            return

        signals = self._collect_signals(klines)
        decision = self._consensus(signals)

        if decision is None:
            return

        target_side, agreeing = decision
        target_odd = market.yes_price if target_side == "YES" else market.no_price

        if not (self.min_odds <= target_odd <= self.max_odds):
            logger.info(
                f"[tick] {target_side} @ {target_odd:.3f} fora de "
                f"[{self.min_odds:.2f}, {self.max_odds:.2f}] — skip"
            )
            return

        self._open_paper_position(market, target_side, target_odd, btc_price, agreeing)

    def _collect_signals(self, klines) -> List[Signal]:
        signals = []
        for strat in self.strategies:
            try:
                sig = strat.generate_signal(klines)
                signals.append(sig)
                logger.info(f"  → {strat.name}: {sig.signal_type.value} ({sig.reason})")
            except Exception as e:
                logger.warning(f"  → {strat.name}: erro {e}")
        return signals

    def _consensus(self, signals: List[Signal]) -> Optional[tuple[str, List[str]]]:
        votes = Counter(s.signal_type for s in signals if s.is_actionable)
        if not votes:
            return None

        top, count = votes.most_common(1)[0]
        if count < self.min_consensus:
            logger.info(f"[tick] Sem consenso ({count}/{self.min_consensus}) — HOLD")
            return None

        side = "YES" if top == SignalType.BUY else "NO"
        agreeing = [s.strategy for s in signals if s.signal_type == top]
        logger.info(f"[tick] CONSENSO: {side} ({count} votos: {agreeing})")
        return side, agreeing

    def _open_paper_position(
        self,
        market: BTCMarket,
        side: str,
        odd: float,
        btc_price: float,
        agreeing: List[str],
    ) -> None:
        pseudo = Signal(
            signal_type=SignalType.BUY,
            price=odd,
            confidence=0.7,
            strategy="btc_paper_ensemble",
        )
        decision = self.risk.evaluate(pseudo)
        if not decision.approved:
            logger.info(f"[risk] Rejeitado: {decision.reason}")
            return

        size = decision.adjusted_size
        shares = size / odd if odd > 0 else 0.0
        self.risk.register_trade_open(pseudo, size)

        pos = PaperPosition(
            market_slug=market.slug,
            market_question=market.question,
            side=side,
            entry_odd=odd,
            size_usd=size,
            shares=shares,
            entry_time=datetime.now(timezone.utc).isoformat(),
            market_end_time=market.end_time.isoformat(),
            btc_at_entry=btc_price,
            consensus_strategies=agreeing,
        )
        self.open_positions.append(pos)
        self._append_trade(pos, event="open")

        logger.info(
            f"📥 [PAPER OPEN] {side} '{market.slug}' "
            f"@ {odd:.3f} | size=${size:.2f} | shares={shares:.2f} | "
            f"BTC=${btc_price:,.2f} | fecha em {market.seconds_to_close:.0f}s"
        )

    def _settle_expired_positions(self) -> None:
        if not self.open_positions:
            return

        now = datetime.now(timezone.utc)
        still_open: List[PaperPosition] = []

        for pos in self.open_positions:
            end = datetime.fromisoformat(pos.market_end_time)
            if now < end:
                still_open.append(pos)
                continue

            btc_close = self.binance.fetch_price_at(end)
            if btc_close is None:
                logger.warning(
                    f"[settle] Não consegui preço BTC em {end.isoformat()} "
                    f"para '{pos.market_slug}' — adiando settle"
                )
                still_open.append(pos)
                continue

            up_won = btc_close > pos.btc_at_entry
            yes_paid = up_won
            pos.btc_at_close = btc_close

            if (pos.side == "YES" and yes_paid) or (pos.side == "NO" and not yes_paid):
                pos.exit_value = pos.shares * 1.0
                pos.status = "won"
            else:
                pos.exit_value = 0.0
                pos.status = "lost"

            pos.pnl = pos.exit_value - pos.size_usd
            self.risk.register_trade_close(pnl=pos.pnl, size=pos.size_usd)
            self._append_trade(pos, event="close")

            outcome = "✅ WIN " if pos.status == "won" else "❌ LOSS"
            logger.info(
                f"📤 [PAPER CLOSE] {outcome} '{pos.market_slug}' {pos.side} "
                f"@ {pos.entry_odd:.3f} | BTC ${pos.btc_at_entry:,.2f} → "
                f"${btc_close:,.2f} | PnL ${pos.pnl:+.3f} | "
                f"capital ${self.risk.current_capital:.2f}"
            )

        self.open_positions = still_open

    def _append_trade(self, pos: PaperPosition, event: str) -> None:
        record = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **asdict(pos),
        }
        with open(TRADES_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")

    def _log_summary(self) -> None:
        status = self.risk.get_status()
        logger.info("=" * 60)
        logger.info("  RESUMO")
        logger.info("=" * 60)
        logger.info(f"Capital final:    ${status['current_capital']:.2f}")
        logger.info(f"Pico:             ${status['peak_capital']:.2f}")
        logger.info(f"Drawdown:         {status['drawdown_pct']:.1f}%")
        logger.info(f"Total trades:     {status['total_trades']}")
        logger.info(f"Trades hoje:      {status['trades_today']}/{status['max_trades_per_day']}")
        logger.info(f"Posições abertas: {len(self.open_positions)}")
        logger.info(f"Histórico:        {TRADES_FILE}")
        logger.info("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC 15-min paper trading bot (dry-run)")
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--trade-size", type=float, default=DEFAULT_TRADE_SIZE)
    parser.add_argument("--max-trades", type=int, default=DEFAULT_MAX_TRADES_DAY)
    parser.add_argument("--min-odds", type=float, default=DEFAULT_MIN_ODDS)
    parser.add_argument("--max-odds", type=float, default=DEFAULT_MAX_ODDS)
    parser.add_argument("--min-consensus", type=int, default=DEFAULT_MIN_CONSENSUS)
    parser.add_argument("--interval", type=int, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true", help="Roda 1 ciclo e sai")
    args = parser.parse_args()

    bot = BTCPaperBot(
        capital=args.capital,
        trade_size=args.trade_size,
        max_trades_per_day=args.max_trades,
        min_odds=args.min_odds,
        max_odds=args.max_odds,
        min_consensus=args.min_consensus,
    )
    bot.run(interval=args.interval, once=args.once)


if __name__ == "__main__":
    main()
