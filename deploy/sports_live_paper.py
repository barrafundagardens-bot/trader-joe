"""
deploy/sports_live_paper.py — Paper trading AO VIVO de esportes.

Diferente do paper_trading.py (offline/CSV), este script:
    1. Escaneia TODOS os mercados de esportes via API (min $300 liq)
    2. Seleciona automaticamente os melhores por odds/score
    3. Acompanha preços em tempo real via polling
    4. Entra em posições simuladas quando a estratégia sinaliza
    5. Gerencia stop-loss, take-profit e risco — tudo sem capital real

Uso:
    # Rodar scan de esportes + paper trading ao vivo (recomendado)
    python deploy/sports_live_paper.py

    # Scan agressivo ($300 mínimo, mais mercados)
    python deploy/sports_live_paper.py --min-liquidity 300 --max-markets 10

    # Estratégia Favorites (favoritos claros >= 90%)
    python deploy/sports_live_paper.py --strategy favorites --min-liquidity 300

    # Ciclos mais rápidos (poll a cada 30s)
    python deploy/sports_live_paper.py --interval 30 --min-liquidity 300
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.base_strategy import Signal, SignalType
from strategies.fvg_multitf_strategy import FVGMultiTFStrategy
from strategies.macd_strategy import MACDStrategy
from strategies.rsi_strategy import RSIStrategy
from strategies.cvd_strategy import CVDStrategy
from strategies.favorites_strategy import FavoritesStrategy
from bot.risk_manager import RiskManager

os.makedirs("logs", exist_ok=True)
os.makedirs("results", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/sports_live_paper.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)

GAMMA_API  = "https://gamma-api.polymarket.com"
DATA_API   = "https://data-api.polymarket.com"
CLOB_API   = "https://clob.polymarket.com"

SPORTS_KEYWORDS = [
    "nba", "nfl", "mlb", "nhl",
    "soccer", "football", "champions", "premier league", "laliga", "serie a",
    "tennis", "atp", "wta", "wimbledon", "us open", "french open", "australian open",
    "golf", "pga", "masters", "open championship",
    "boxing", "mma", "ufc",
    "formula 1", "f1", "motogp", "nascar",
    "rugby", "cricket", "olympic", "world cup",
]

STRATEGY_MAP = {
    "fvg_multitf": FVGMultiTFStrategy,
    "macd":        MACDStrategy,
    "rsi":         RSIStrategy,
    "cvd":         CVDStrategy,
    "favorites":   FavoritesStrategy,
}


# ─────────────────────────────────────────────
# Estruturas de dados
# ─────────────────────────────────────────────

@dataclass
class SportMarket:
    question: str
    slug: str
    condition_id: str
    token_id: str
    liquidity: float
    volume: float
    yes_price: float
    no_price: float
    days_to_resolve: float
    url: str
    score: float = 0.0

    @property
    def confidence(self) -> float:
        return abs(self.yes_price - 0.5) * 2


@dataclass
class PaperPosition:
    market: SportMarket
    side: str            # BUY / SELL
    entry_price: float
    size: float
    entry_time: datetime
    stop_price: float
    target_price: float
    is_open: bool = True
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl: float = 0.0


# ─────────────────────────────────────────────
# Scanner de mercados
# ─────────────────────────────────────────────

def scan_sports_markets(min_liquidity: float = 300.0, max_markets: int = 15) -> List[SportMarket]:
    """Busca todos os mercados de esportes via paginação completa."""
    all_raw = []
    offset = 0
    batch = 100

    print(f"\n  📡 Escaneando mercados de esportes (liq >= ${min_liquidity:,.0f})...")

    while True:
        try:
            r = requests.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "closed": "false", "limit": batch, "offset": offset},
                timeout=20,
            )
            r.raise_for_status()
            page = r.json()
            if not isinstance(page, list) or not page:
                break
            all_raw.extend(page)
            offset += len(page)
            if len(page) < batch:
                break
            time.sleep(0.15)
        except Exception as e:
            logger.error(f"Erro paginação: {e}")
            break

    logger.info(f"Total mercados brutos: {len(all_raw)}")

    markets: List[SportMarket] = []
    now = datetime.now(timezone.utc)

    for m in all_raw:
        q = m.get("question", "").lower()
        if not any(kw in q for kw in SPORTS_KEYWORDS):
            continue

        liq = float(m.get("liquidityNum", 0))
        if liq < min_liquidity:
            continue

        prices = m.get("outcomePrices", [])
        if isinstance(prices, str):
            try: prices = json.loads(prices)
            except: prices = []
        if len(prices) < 2:
            continue

        yes_p = float(prices[0])
        no_p  = float(prices[1])

        tokens = m.get("clobTokenIds", [])
        token_id = tokens[0] if tokens else ""
        if not token_id:
            continue

        end_str = m.get("endDate", "")
        days = 30.0
        if end_str and "T" in end_str:
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                days = (end_dt - now).total_seconds() / 86400
            except:
                pass
        if days <= 0:
            continue

        slug = m.get("slug", "")
        vol  = float(m.get("volumeNum", 0))

        # Score: prioriza favoritos claros + boa liquidez + resolve logo
        confidence  = abs(yes_p - 0.5) * 2
        liq_score   = min(1.0, liq / 5000) * 0.3
        vol_score   = min(1.0, vol / 10000) * 0.2
        conf_score  = confidence * 0.35
        time_score  = max(0, (30 - days) / 30) * 0.15

        score = liq_score + vol_score + conf_score + time_score

        markets.append(SportMarket(
            question=m.get("question", "?")[:120],
            slug=slug,
            condition_id=m.get("conditionId", ""),
            token_id=token_id,
            liquidity=liq,
            volume=vol,
            yes_price=yes_p,
            no_price=no_p,
            days_to_resolve=days,
            url=f"https://polymarket.com/{slug}" if slug else "",
            score=score,
        ))

    markets.sort(key=lambda m: -m.score)
    selected = markets[:max_markets]

    print(f"  ✅ {len(all_raw)} mercados → {len(markets)} de esportes → top {len(selected)} selecionados\n")
    return selected


# ─────────────────────────────────────────────
# Buscar preço atual via CLOB
# ─────────────────────────────────────────────

def fetch_current_price(token_id: str) -> Optional[float]:
    """Busca mid-price atual do token via CLOB API."""
    try:
        r = requests.get(
            f"{CLOB_API}/midpoint",
            params={"token_id": token_id},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            mid = data.get("mid")
            if mid is not None:
                return float(mid)
    except:
        pass

    # Fallback: Gamma API
    try:
        r = requests.get(
            f"{CLOB_API}/last-trade-price",
            params={"token_id": token_id},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            price = data.get("price")
            if price is not None:
                return float(price)
    except:
        pass

    return None


# ─────────────────────────────────────────────
# Engine de paper trading multi-mercado
# ─────────────────────────────────────────────

class SportsLivePaperEngine:
    def __init__(
        self,
        strategy_name: str,
        markets: List[SportMarket],
        initial_capital: float = 500.0,
        trade_size: float = 20.0,
        poll_interval: int = 60,
        stop_loss_pct: float = 0.10,
    ):
        self.strategy_name = strategy_name
        self.markets = markets
        self.poll_interval = poll_interval
        self.stop_loss_pct = stop_loss_pct
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self._running = False

        # Uma estratégia por mercado (estado independente)
        strategy_cls = STRATEGY_MAP[strategy_name]
        self.strategies: Dict[str, object] = {
            m.token_id: strategy_cls() for m in markets
        }

        # Buffers de preço por token_id
        self.price_buffers: Dict[str, deque] = {
            m.token_id: deque(maxlen=300) for m in markets
        }

        # Posições abertas
        self.open_positions: Dict[str, PaperPosition] = {}
        self.closed_positions: List[PaperPosition] = []
        self.trade_count = 0

        # Risk manager global
        self.risk = RiskManager(
            initial_capital=initial_capital,
            max_trade_size=trade_size,
            max_loss_per_trade=stop_loss_pct,
            max_trades_per_day=20,
            emergency_drawdown=0.25,
        )

        signal.signal(signal.SIGINT, self._shutdown_signal)
        signal.signal(signal.SIGTERM, self._shutdown_signal)

    def _shutdown_signal(self, *_):
        logger.info("\n  ⛔ Shutdown recebido. Encerrando...")
        self._running = False

    def _print_header(self):
        print("\n" + "=" * 100)
        print("  🏆 SPORTS LIVE PAPER TRADING")
        print("=" * 100)
        print(f"  Estratégia  : {self.strategy_name}")
        print(f"  Mercados    : {len(self.markets)}")
        print(f"  Capital     : ${self.initial_capital:,.2f} (simulado)")
        print(f"  Poll        : {self.poll_interval}s")
        print(f"  Stop-loss   : {self.stop_loss_pct*100:.0f}%")
        print()
        print("  Mercados monitorados:")
        for i, m in enumerate(self.markets, 1):
            fav = "YES" if m.yes_price > m.no_price else "NO"
            print(f"    {i:>2}. [{fav} @ {max(m.yes_price, m.no_price):.3f}] {m.question[:70]}")
        print("=" * 100)

    def _fetch_all_prices(self) -> Dict[str, float]:
        """Busca preços atuais para todos os mercados."""
        prices = {}
        for m in self.markets:
            price = fetch_current_price(m.token_id)
            if price is not None:
                prices[m.token_id] = price
                self.price_buffers[m.token_id].append({
                    "timestamp": datetime.now(),
                    "open": price, "high": price, "low": price,
                    "close": price, "volume": 1.0,
                })
        return prices

    def _build_df(self, token_id: str) -> Optional[pd.DataFrame]:
        buf = list(self.price_buffers[token_id])
        if len(buf) < self.strategies[token_id].min_candles:
            return None
        df = pd.DataFrame(buf)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df.set_index("timestamp").sort_index()

    def _check_exit(self, pos: PaperPosition, price: float) -> Optional[Tuple[str, float]]:
        """Verifica stop-loss ou take-profit. Retorna (razão, pnl) ou None."""
        if pos.side == "BUY":
            if price <= pos.stop_price:
                pnl = pos.size * (price - pos.entry_price) / pos.entry_price
                return "stop_loss", pnl
            if price >= pos.target_price:
                pnl = pos.size * (price - pos.entry_price) / pos.entry_price
                return "take_profit", pnl
        else:  # SELL
            if price >= pos.stop_price:
                pnl = pos.size * (pos.entry_price - price) / pos.entry_price
                return "stop_loss", pnl
            if price <= pos.target_price:
                pnl = pos.size * (pos.entry_price - price) / pos.entry_price
                return "take_profit", pnl
        return None

    def _print_status(self, cycle: int, prices: Dict[str, float]):
        open_pnl = 0.0
        for tid, pos in self.open_positions.items():
            price = prices.get(tid, pos.entry_price)
            if pos.side == "BUY":
                open_pnl += pos.size * (price - pos.entry_price) / pos.entry_price
            else:
                open_pnl += pos.size * (pos.entry_price - price) / pos.entry_price

        realized = sum(p.pnl for p in self.closed_positions)
        total_pnl = realized + open_pnl
        pnl_pct = (total_pnl / self.initial_capital) * 100

        print(f"\n  ── Ciclo #{cycle} ── {datetime.now().strftime('%H:%M:%S')} ──────────────────────────")
        print(f"  Capital: ${self.capital:.2f} | PnL realizado: ${realized:+.2f} | Aberto: ${open_pnl:+.2f} | Total: {pnl_pct:+.2f}%")
        print(f"  Posições abertas: {len(self.open_positions)} | Trades fechados: {len(self.closed_positions)}")

        # Mostrar posições abertas
        for tid, pos in self.open_positions.items():
            price = prices.get(tid, pos.entry_price)
            upnl = pos.size * (price - pos.entry_price) / pos.entry_price if pos.side == "BUY" else pos.size * (pos.entry_price - price) / pos.entry_price
            print(f"    📌 {pos.side} {pos.market.question[:55]:55s} @ {pos.entry_price:.3f} | now {price:.3f} | uPnL {upnl:+.3f}")

        # Mostrar preços monitorados sem posição
        for m in self.markets:
            if m.token_id not in self.open_positions and m.token_id in prices:
                p = prices[m.token_id]
                buf_size = len(self.price_buffers[m.token_id])
                strat = self.strategies[m.token_id]
                needed = strat.min_candles
                print(f"    🔍 {m.question[:55]:55s} | {p:.3f} | buffer {buf_size}/{needed}")

    def _save_results(self):
        all_pos = self.closed_positions[:]
        total_pnl = sum(p.pnl for p in all_pos)
        wins = [p for p in all_pos if p.pnl > 0]
        losses = [p for p in all_pos if p.pnl <= 0]

        result = {
            "mode": "sports_live_paper",
            "strategy": self.strategy_name,
            "timestamp": datetime.now().isoformat(),
            "initial_capital": self.initial_capital,
            "total_trades": len(all_pos),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(all_pos) if all_pos else 0,
            "total_pnl": round(total_pnl, 4),
            "trades": [
                {
                    "market": p.market.question,
                    "side": p.side,
                    "entry": p.entry_price,
                    "exit": p.exit_price,
                    "size": p.size,
                    "pnl": round(p.pnl, 4),
                    "reason": p.exit_reason,
                    "url": p.market.url,
                }
                for p in all_pos
            ],
        }

        path = f"results/sports_live_paper_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(path, "w") as f:
            json.dump(result, f, indent=2)

        print(f"\n  💾 Resultados salvos: {path}")
        print(f"  Trades: {len(all_pos)} | Wins: {len(wins)} | Losses: {len(losses)} | PnL: ${total_pnl:+.4f}")

    def run(self):
        self._print_header()
        self._running = True
        cycle = 0
        print("\n  🚀 Iniciando monitoramento... (CTRL+C para parar)\n")

        while self._running:
            cycle += 1

            # 1. Buscar preços
            prices = self._fetch_all_prices()

            # 2. Verificar exits de posições abertas
            to_close = []
            for tid, pos in self.open_positions.items():
                price = prices.get(tid)
                if price is None:
                    continue
                result = self._check_exit(pos, price)
                if result:
                    reason, pnl = result
                    to_close.append((tid, price, reason, pnl))

            for tid, price, reason, pnl in to_close:
                pos = self.open_positions.pop(tid)
                pos.exit_price = price
                pos.exit_reason = reason
                pos.pnl = pnl
                pos.is_open = False
                self.capital += pnl
                self.closed_positions.append(pos)
                icon = "✅" if pnl > 0 else "🔴"
                print(f"\n  {icon} FECHOU [{reason}] {pos.market.question[:60]} | PnL ${pnl:+.4f}")

            # 3. Gerar sinais para mercados sem posição aberta
            for m in self.markets:
                if m.token_id in self.open_positions:
                    continue
                if m.token_id not in prices:
                    continue

                df = self._build_df(m.token_id)
                if df is None:
                    continue

                strat = self.strategies[m.token_id]
                sig = strat.generate_signal(df)

                if not sig.is_actionable:
                    continue

                decision = self.risk.evaluate(sig)
                if not decision.approved:
                    logger.debug(f"Sinal rejeitado ({m.question[:40]}): {decision.reason}")
                    continue

                # Entrar na posição
                price = prices[m.token_id]
                stop_price = (
                    price * (1 - self.stop_loss_pct) if sig.signal_type == SignalType.BUY
                    else price * (1 + self.stop_loss_pct)
                )
                target_price = (
                    min(0.99, price * 1.05) if sig.signal_type == SignalType.BUY
                    else max(0.01, price * 0.95)
                )

                pos = PaperPosition(
                    market=m,
                    side=sig.signal_type.value,
                    entry_price=price,
                    size=decision.adjusted_size,
                    entry_time=datetime.now(),
                    stop_price=stop_price,
                    target_price=target_price,
                )
                self.open_positions[m.token_id] = pos
                self.trade_count += 1
                self.risk.register_trade_open(sig, decision.adjusted_size)

                print(f"\n  📈 ENTROU  [{sig.signal_type.value}] {m.question[:60]}")
                print(f"       Preço: {price:.4f} | Stop: {stop_price:.4f} | Target: {target_price:.4f}")
                print(f"       Size: ${decision.adjusted_size:.2f} | Confiança: {sig.confidence:.0%}")
                print(f"       Motivo: {sig.reason[:100]}")
                print(f"       🔗 {m.url}")

            # 4. Status periódico
            self._print_status(cycle, prices)

            # 5. Aguardar
            print(f"\n  ⏱  Próximo ciclo em {self.poll_interval}s... (CTRL+C para parar)")
            time.sleep(self.poll_interval)

        # Encerrar
        print("\n  Encerrando posições abertas...")
        for tid, pos in list(self.open_positions.items()):
            price = self.price_buffers[tid][-1]["close"] if self.price_buffers[tid] else pos.entry_price
            pnl = pos.size * (price - pos.entry_price) / pos.entry_price if pos.side == "BUY" else pos.size * (pos.entry_price - price) / pos.entry_price
            pos.exit_price = price
            pos.exit_reason = "shutdown"
            pos.pnl = pnl
            pos.is_open = False
            self.capital += pnl
            self.closed_positions.append(pos)

        self._save_results()


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sports Live Paper Trading — Scan automático + entradas reais simuladas",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  # Scan agressivo ($300 mínimo) + FVG (padrão)
  python deploy/sports_live_paper.py --min-liquidity 300 --max-markets 10

  # Favoritos claros (strategy=favorites detecta >= 90% odds)
  python deploy/sports_live_paper.py --strategy favorites --min-liquidity 300

  # Scan mais rápido (poll a cada 30s)
  python deploy/sports_live_paper.py --interval 30 --min-liquidity 300

  # Capital maior simulado
  python deploy/sports_live_paper.py --capital 1000 --trade-size 50
        """,
    )
    parser.add_argument("--strategy",       default="favorites",
                        choices=list(STRATEGY_MAP.keys()), help="Estratégia (padrão: favorites)")
    parser.add_argument("--min-liquidity",  type=float, default=300.0, help="Liquidez mínima USD (padrão: 300)")
    parser.add_argument("--max-markets",    type=int,   default=10,    help="Máx de mercados monitorados (padrão: 10)")
    parser.add_argument("--interval",       type=int,   default=60,    help="Segundos entre ciclos (padrão: 60)")
    parser.add_argument("--capital",        type=float, default=500.0, help="Capital simulado (padrão: $500)")
    parser.add_argument("--trade-size",     type=float, default=20.0,  help="Tamanho por trade (padrão: $20)")
    parser.add_argument("--stop-loss",      type=float, default=0.10,  help="Stop-loss em decimal (padrao: 0.10 = 10%%)")
    args = parser.parse_args()

    markets = scan_sports_markets(
        min_liquidity=args.min_liquidity,
        max_markets=args.max_markets,
    )

    if not markets:
        print("  ❌ Nenhum mercado de esportes encontrado. Tente reduzir --min-liquidity.")
        sys.exit(1)

    engine = SportsLivePaperEngine(
        strategy_name=args.strategy,
        markets=markets,
        initial_capital=args.capital,
        trade_size=args.trade_size,
        poll_interval=args.interval,
        stop_loss_pct=args.stop_loss,
    )
    engine.run()


if __name__ == "__main__":
    main()
