"""
deploy/btc_paper_bot.py — BTC UP/DOWN paper trading bot (dry-run).

Ensemble de 3 estratégias sobre candles BTC 1m da Binance decide
entrada YES (UP) ou NO (DOWN) no mercado BTC 5-min ativo do Polymarket.
Posições simuladas, sem capital real, sem ordens reais.

Capital: $50 simulado | Tamanho/trade: $5 | Limite: 5 trades/dia
Filtro de odds: 0.40 ≤ yes_price ≤ 0.65 (zona de incerteza)

Uso:
    python3 deploy/btc_paper_bot.py                  # loop contínuo (60s)
    python3 deploy/btc_paper_bot.py --once           # roda 1 ciclo e sai
    python3 deploy/btc_paper_bot.py --interval 30    # custom poll interval
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
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from bot.binance_feed import BinanceFeed
from bot.polymarket_btc_feed import BTCMarket, PolymarketBTCFeed
from bot.risk_manager import RiskManager
from strategies.base_strategy import Signal, SignalType
from strategies.btc_spike_strategy import BTCSpikeStrategy
from strategies.btc_rsi_momentum_strategy import BTCRSIMomentumStrategy
from strategies.macd_strategy import MACDStrategy

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

DEFAULT_CAPITAL      = 50.0
DEFAULT_TRADE_SIZE   = 5.0
DEFAULT_MAX_TRADES   = 5
DEFAULT_MIN_ODDS     = 0.40
DEFAULT_MAX_ODDS     = 0.65
DEFAULT_MIN_CONSENSUS = 2
DEFAULT_POLL_SECONDS = 60

TRADES_FILE = LOG_DIR / "btc_paper_trades.jsonl"
W = 68  # largura do painel


@dataclass
class PaperPosition:
    market_slug: str
    market_question: str
    side: str
    entry_odd: float
    size_usd: float
    shares: float
    entry_time: str
    market_end_time: str
    btc_at_entry: float
    consensus_strategies: List[str] = field(default_factory=list)
    status: str = "open"
    btc_at_close: Optional[float] = None
    exit_value: Optional[float] = None
    pnl: Optional[float] = None


class BTCPaperBot:
    """Orquestrador dry-run do bot BTC UP/DOWN."""

    def __init__(
        self,
        capital: float = DEFAULT_CAPITAL,
        trade_size: float = DEFAULT_TRADE_SIZE,
        max_trades_per_day: int = DEFAULT_MAX_TRADES,
        min_odds: float = DEFAULT_MIN_ODDS,
        max_odds: float = DEFAULT_MAX_ODDS,
        min_consensus: int = DEFAULT_MIN_CONSENSUS,
    ):
        self.min_odds = min_odds
        self.max_odds = max_odds
        self.min_consensus = min_consensus

        self.binance   = BinanceFeed()
        self.polymarket = PolymarketBTCFeed()

        self.risk = RiskManager(
            initial_capital=capital,
            max_trade_size=trade_size,
            max_loss_per_trade=0.30,
            max_trades_per_day=max_trades_per_day,
            emergency_drawdown=0.20,
        )

        self.strategies = [
            MACDStrategy(params={"min_histogram": 5.0, "ema_tolerance": 0.005}),
            BTCRSIMomentumStrategy(params={"pivot": 50.0, "min_magnitude": 3.0}),
            BTCSpikeStrategy(params={"volume_factor": 1.2, "zscore_threshold": 1.8}),
        ]

        self.open_positions:   List[PaperPosition] = []
        self.closed_positions: List[PaperPosition] = []
        self.wins       = 0
        self.losses     = 0
        self.total_pnl  = 0.0
        self._running   = True

        self._load_history()

    # ----------------------------------------------------------------
    # Histórico persistido
    # ----------------------------------------------------------------

    def _load_history(self) -> None:
        """Carrega wins/losses/pnl de sessões anteriores a partir do JSONL."""
        if not TRADES_FILE.exists():
            return
        try:
            with open(TRADES_FILE) as f:
                for line in f:
                    rec = json.loads(line)
                    if rec.get("event") != "close":
                        continue
                    status = rec.get("status", "")
                    pnl    = rec.get("pnl") or 0.0
                    if status == "won":
                        self.wins += 1
                    elif status == "lost":
                        self.losses += 1
                    self.total_pnl += pnl
                    self.closed_positions.append(_dict_to_pos(rec))
            logger.info(
                f"[history] {self.wins}W / {self.losses}L carregados do JSONL | "
                f"PnL acumulado ${self.total_pnl:+.2f}"
            )
        except Exception as e:
            logger.warning(f"[history] Erro ao carregar histórico: {e}")

    # ----------------------------------------------------------------
    # Loop principal
    # ----------------------------------------------------------------

    def run(self, interval: int = DEFAULT_POLL_SECONDS, once: bool = False) -> None:
        os_signal.signal(os_signal.SIGINT, self._stop)
        os_signal.signal(os_signal.SIGTERM, self._stop)

        logger.info("=" * W)
        logger.info("  ₿  BTC UP/DOWN PAPER BOT — DRY RUN")
        logger.info("=" * W)
        logger.info(
            f"Capital: ${self.risk.current_capital:.2f} | "
            f"Trade: ${self.risk.max_trade_size:.2f} | "
            f"Max/dia: {self.risk.max_trades_per_day} | "
            f"Odds: [{self.min_odds:.2f}–{self.max_odds:.2f}]"
        )
        logger.info(f"Estratégias: {[s.name for s in self.strategies]}")
        logger.info(f"Poll: {interval}s | {'ONCE' if once else 'LOOP'}")
        logger.info("=" * W)

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

    def _stop(self, *_args) -> None:
        logger.info("Parando após este ciclo…")
        self._running = False

    # ----------------------------------------------------------------
    # Ciclo
    # ----------------------------------------------------------------

    def _tick(self) -> None:
        cycle_start = datetime.now(timezone.utc)

        klines = self.binance.fetch_klines(interval="1m", limit=200)
        if klines.empty:
            logger.warning("Sem dados Binance — pulando ciclo")
            return

        btc_price = float(klines["close"].iloc[-1])
        logger.info(f"[tick] BTC = ${btc_price:,.2f}")

        self._settle_expired_positions()

        market = self.polymarket.find_active_market()

        # Coleta sinais sempre (para exibir no painel)
        signals: List[Signal] = []
        decision: Optional[Tuple[str, List[str]]] = None

        if market is not None:
            if not any(p.market_slug == market.slug for p in self.open_positions):
                if not self.risk.is_emergency_mode:
                    if self.risk.trades_today < self.risk.max_trades_per_day:
                        signals  = self._collect_signals(klines)
                        decision = self._consensus(signals)

                        if decision is not None:
                            side, agreeing = decision
                            odd = market.yes_price if side == "YES" else market.no_price
                            if self.min_odds <= odd <= self.max_odds:
                                self._open_paper_position(market, side, odd, btc_price, agreeing)
                            else:
                                logger.info(
                                    f"[tick] {side} @ {odd:.3f} fora de "
                                    f"[{self.min_odds:.2f}, {self.max_odds:.2f}] — skip"
                                )
                    else:
                        logger.info(
                            f"[tick] Limite diário ({self.risk.trades_today}/"
                            f"{self.risk.max_trades_per_day}) — aguardando reset"
                        )
                else:
                    logger.warning("[tick] Modo emergência — sem novas entradas")
            else:
                logger.info(f"[tick] Posição aberta em '{market.slug}' — aguardando")

            # Coleta sinais mesmo que não vá operar (para exibir no painel)
            if not signals:
                signals = self._collect_signals(klines)
        else:
            logger.info("[tick] Nenhum mercado BTC ativo agora")

        self._print_status(cycle_start, btc_price, market, signals)

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

    def _consensus(self, signals: List[Signal]) -> Optional[Tuple[str, List[str]]]:
        votes = Counter(s.signal_type for s in signals if s.is_actionable)
        if not votes:
            return None
        top, count = votes.most_common(1)[0]
        if count < self.min_consensus:
            logger.info(f"[tick] Sem consenso ({count}/{self.min_consensus}) — HOLD")
            return None
        side     = "YES" if top == SignalType.BUY else "NO"
        agreeing = [s.strategy for s in signals if s.signal_type == top]
        logger.info(f"[tick] CONSENSO: {side} ({count} votos: {agreeing})")
        return side, agreeing

    # ----------------------------------------------------------------
    # Execução paper
    # ----------------------------------------------------------------

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
        dec = self.risk.evaluate(pseudo)
        if not dec.approved:
            logger.info(f"[risk] Rejeitado: {dec.reason}")
            return

        size   = dec.adjusted_size
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
        self._append_trade(pos, "open")

        logger.info(
            f"📥 [PAPER OPEN] {side} '{market.slug}' "
            f"@ {odd:.3f} | size=${size:.2f} | shares={shares:.2f} | "
            f"BTC=${btc_price:,.2f} | fecha em {market.seconds_to_close:.0f}s"
        )

    def _settle_expired_positions(self) -> None:
        if not self.open_positions:
            return
        now        = datetime.now(timezone.utc)
        still_open: List[PaperPosition] = []

        for pos in self.open_positions:
            end = datetime.fromisoformat(pos.market_end_time)
            if now < end:
                still_open.append(pos)
                continue

            btc_close = self.binance.fetch_price_at(end)
            if btc_close is None:
                logger.warning(f"[settle] Sem preço BTC em {end.isoformat()} — adiando")
                still_open.append(pos)
                continue

            up_won        = btc_close > pos.btc_at_entry
            pos.btc_at_close = btc_close

            if (pos.side == "YES" and up_won) or (pos.side == "NO" and not up_won):
                pos.exit_value = pos.shares * 1.0
                pos.status     = "won"
                self.wins     += 1
            else:
                pos.exit_value = 0.0
                pos.status     = "lost"
                self.losses   += 1

            pos.pnl       = pos.exit_value - pos.size_usd
            self.total_pnl += pos.pnl
            self.risk.register_trade_close(pnl=pos.pnl, size=pos.size_usd)
            self._append_trade(pos, "close")
            self.closed_positions.append(pos)

            icon = "✅ WIN" if pos.status == "won" else "❌ LOSS"
            logger.info(
                f"📤 [PAPER CLOSE] {icon} '{pos.market_slug}' {pos.side} "
                f"@ {pos.entry_odd:.3f} | BTC ${pos.btc_at_entry:,.0f}→"
                f"${btc_close:,.0f} | PnL ${pos.pnl:+.2f} | "
                f"capital ${self.risk.current_capital:.2f}"
            )

        self.open_positions = still_open

    def _append_trade(self, pos: PaperPosition, event: str) -> None:
        rec = {"event": event, "timestamp": datetime.now(timezone.utc).isoformat(), **asdict(pos)}
        with open(TRADES_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")

    # ----------------------------------------------------------------
    # Painel de status (impresso no terminal a cada ciclo)
    # ----------------------------------------------------------------

    def _print_status(
        self,
        cycle_start: datetime,
        btc_price: float,
        market: Optional[BTCMarket],
        signals: List[Signal],
    ) -> None:
        now_str  = cycle_start.strftime("%H:%M:%S UTC")
        total    = self.wins + self.losses
        wr_str   = f"{self.wins}W / {self.losses}L ({self.wins/total*100:.0f}%)" if total else "— (sem histórico)"
        pnl_sign = "+" if self.total_pnl >= 0 else ""
        capital  = self.risk.current_capital
        status   = self.risk.get_status()

        print(f"\n{'='*W}")
        print(f"  ₿  BTC UP/DOWN BOT | DRY RUN | {now_str}")
        print(f"  Trade: ${self.risk.max_trade_size:.0f} | "
              f"Odds: [{self.min_odds:.2f}–{self.max_odds:.2f}] | "
              f"Consenso: {self.min_consensus}/3")
        print(f"  {'─'*64}")

        # Win Rate + Capital
        print(f"  📈 Win Rate:  {wr_str}")
        print(f"  💰 Capital:  ${capital:.2f}  |  "
              f"PnL acumulado: {pnl_sign}${self.total_pnl:.2f}")

        # Posições e trades de hoje
        n_open   = len(self.open_positions)
        n_closed = len(self.closed_positions)
        print(f"  📂 Posições: {n_open} abertas / {n_closed} fechadas  |  "
              f"Trades hoje: {status['trades_today']}/{status['max_trades_per_day']}")

        # Avg win / avg loss
        if self.closed_positions:
            wins_pnl  = [p.pnl for p in self.closed_positions if p.status == "won"  and p.pnl is not None]
            loses_pnl = [p.pnl for p in self.closed_positions if p.status == "lost" and p.pnl is not None]
            avg_w = sum(wins_pnl)  / len(wins_pnl)  if wins_pnl  else 0.0
            avg_l = sum(loses_pnl) / len(loses_pnl) if loses_pnl else 0.0
            print(f"  📊 Avg WIN: +${avg_w:.2f}  |  Avg LOSS: -${abs(avg_l):.2f}")

        print(f"  {'─'*64}")

        # Votos do ciclo
        if signals:
            votes_str = "  |  ".join(
                f"{s.strategy.replace('Strategy','').replace('BTC','')}: "
                f"{'🟢' if s.signal_type.value=='BUY' else '🔴' if s.signal_type.value=='SELL' else '⚪'}"
                f" {s.signal_type.value}"
                for s in signals
            )
            print(f"  SINAIS:  {votes_str}")
        else:
            print(f"  SINAIS:  — (sem mercado ativo)")

        # Mercado atual
        if market:
            secs = market.seconds_to_close
            mins = int(secs // 60)
            secs_rem = int(secs % 60)
            q_short = market.question.replace("Bitcoin Up or Down - ", "")[:35]
            print(
                f"  MERCADO: {q_short}  "
                f"(fecha em {mins}m{secs_rem:02d}s)  "
                f"YES={market.yes_price:.3f}  NO={market.no_price:.3f}  "
                f"liq=${market.liquidity:,.0f}"
            )
            print(f"  BTC:     ${btc_price:,.2f}")
        else:
            print(f"  BTC:     ${btc_price:,.2f}  |  Sem mercado ativo no momento")

        print(f"  {'─'*64}")

        # Últimos 5 trades
        recent = (self.closed_positions + self.open_positions)[-5:]
        recent.reverse()
        if recent:
            print(f"  ÚLTIMOS TRADES:")
            for p in recent:
                icon    = "✅" if p.status == "won" else "❌" if p.status == "lost" else "🔄"
                ts      = p.entry_time[:16].replace("T", " ").replace("+00:00", "")
                btc_mv  = f"${p.btc_at_entry:,.0f}→${p.btc_at_close:,.0f}" if p.btc_at_close else f"${p.btc_at_entry:,.0f}→?"
                pnl_str = f"PnL {p.pnl:+.2f}" if p.pnl is not None else "aberta"
                q_mini  = p.market_question.replace("Bitcoin Up or Down - ", "")[:22]
                print(f"    {icon} {p.side:<3} @ {p.entry_odd:.3f}  {q_mini:<22}  {btc_mv}  {pnl_str}  {ts}")
        else:
            print(f"  ÚLTIMOS TRADES: — (nenhum ainda)")

        print(f"{'='*W}\n")


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------

def _dict_to_pos(d: dict) -> PaperPosition:
    return PaperPosition(
        market_slug=d.get("market_slug", ""),
        market_question=d.get("market_question", ""),
        side=d.get("side", ""),
        entry_odd=d.get("entry_odd", 0.0),
        size_usd=d.get("size_usd", 0.0),
        shares=d.get("shares", 0.0),
        entry_time=d.get("entry_time", ""),
        market_end_time=d.get("market_end_time", ""),
        btc_at_entry=d.get("btc_at_entry", 0.0),
        consensus_strategies=d.get("consensus_strategies", []),
        status=d.get("status", ""),
        btc_at_close=d.get("btc_at_close"),
        exit_value=d.get("exit_value"),
        pnl=d.get("pnl"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC UP/DOWN paper trading bot (dry-run)")
    parser.add_argument("--capital",      type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--trade-size",   type=float, default=DEFAULT_TRADE_SIZE)
    parser.add_argument("--max-trades",   type=int,   default=DEFAULT_MAX_TRADES)
    parser.add_argument("--min-odds",     type=float, default=DEFAULT_MIN_ODDS)
    parser.add_argument("--max-odds",     type=float, default=DEFAULT_MAX_ODDS)
    parser.add_argument("--min-consensus",type=int,   default=DEFAULT_MIN_CONSENSUS)
    parser.add_argument("--interval",     type=int,   default=DEFAULT_POLL_SECONDS)
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
