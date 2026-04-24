"""
deploy/arbitrage_bot.py — Bot automatizado de arbitragem combinatória.

Ciclo principal:
    1. Buscar todos os mercados ativos
    2. Agrupar por event.id (mercados relacionados)
    3. Detectar arbitragens categóricas (soma YES != 1.0)
    4. Filtrar por spread mínimo
    5. Executar trades em ambas as pernas
    6. Monitorar posições abertas
    7. Repetir a cada N minutos

Modos de operacao:
    --dry-run    (padrao) Simula tudo sem enviar ordens reais
    --live       Envia ordens reais via py-clob-client

Uso:
    python deploy/arbitrage_bot.py                      # dry-run, padrao
    python deploy/arbitrage_bot.py --interval 300       # a cada 5 min
    python deploy/arbitrage_bot.py --live --confirm     # modo real
    python deploy/arbitrage_bot.py --once               # roda 1 vez e para
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/arbitrage_bot.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)


# ================================================================
# Config
# ================================================================

GAMMA_API = "https://gamma-api.polymarket.com"


# ================================================================
# Data structures
# ================================================================

@dataclass
class ArbOpportunity:
    """Uma oportunidade de arbitragem detectada."""
    event_slug: str
    event_id: str
    arb_type: str              # "overpriced" ou "underpriced"
    spread_bps: int            # Em basis points (0-10000)
    sum_yes: float             # Soma dos YES (deve ser ~1.0)
    num_markets: int
    total_liquidity: float
    top_markets: List[dict]    # Top 5-10 markets
    profit_per_dollar: float
    end_date: str = ""


@dataclass
class ArbitragePosition:
    """Posição aberta de arbitragem."""
    event_id: str
    event_slug: str
    arb_type: str              # "overpriced" ou "underpriced"
    entry_spread_bps: int
    entry_sum_yes: float
    entry_time: str
    num_markets: int
    size_usd: float = 1.0
    status: str = "open"       # "open", "closed", "expired"
    exit_spread_bps: Optional[int] = None
    exit_sum_yes: Optional[float] = None
    pnl: Optional[float] = None
    end_date: str = ""


# ================================================================
# Arbitrage Detection
# ================================================================

def fetch_active_markets(limit: int = 300) -> List[dict]:
    """Busca mercados ativos da Gamma API."""
    markets = []
    offset = 0

    while len(markets) < limit:
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": min(100, limit - len(markets)),
                    "offset": offset,
                },
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()

            if not batch:
                break

            markets.extend(batch)
            offset += len(batch)

            if len(batch) < 100:
                break

            time.sleep(0.2)

        except Exception as e:
            logger.warning(f"Erro ao buscar mercados: {e}")
            break

    return markets[:limit]


def parse_prices(prices_raw) -> list:
    """Parse outcomePrices."""
    if not prices_raw:
        return []

    if isinstance(prices_raw, str):
        try:
            return [float(p) for p in json.loads(prices_raw)]
        except Exception:
            return []

    try:
        return [float(p) for p in prices_raw]
    except Exception:
        return []


def parse_outcomes(outcomes_raw) -> list:
    """Parse outcomes."""
    if not outcomes_raw:
        return []

    if isinstance(outcomes_raw, str):
        try:
            return json.loads(outcomes_raw)
        except Exception:
            return []

    return outcomes_raw or []




def group_markets_by_event(markets: List[dict]) -> dict:
    """Agrupa mercados pelo event.id."""
    groups = defaultdict(lambda: {"slug": "", "markets": []})

    for market in markets:
        events = market.get("events", [])
        for ev in events:
            eid = ev.get("id")
            if eid:
                groups[eid]["slug"] = ev.get("slug", "?")
                groups[eid]["markets"].append(market)

    return groups


def detect_arbitrage(event_id: str, event_slug: str, markets: List[dict],
                     min_spread_bps: int = 50) -> Optional[ArbOpportunity]:
    """Detecta arbitragem categórica em um evento."""
    if len(markets) < 3:
        return None

    market_data = []
    for m in markets:
        prices = parse_prices(m.get("outcomePrices", []))
        outcomes = parse_outcomes(m.get("outcomes", []))

        if not prices or not outcomes or len(prices) < 1:
            continue

        # Pegar preço YES
        yes_idx = None
        for idx, out in enumerate(outcomes):
            if str(out).upper() in ("YES", "SIM"):
                yes_idx = idx
                break

        if yes_idx is None or yes_idx >= len(prices):
            continue

        yes_price = float(prices[yes_idx])

        market_data.append({
            "market": m.get("slug", "?"),
            "question": m.get("question", m.get("title", "?"))[:60],
            "yes_price": yes_price,
            "liquidity": m.get("liquidityNum", 0),
            "token_yes": m.get("clobTokenIds", [None])[yes_idx] if m.get("clobTokenIds") else None,
        })

    if len(market_data) < 3:
        return None

    sum_yes = sum(md["yes_price"] for md in market_data)
    entry_spread = sum_yes - 1.0
    spread_bps = int(abs(entry_spread) * 10000)

    if spread_bps < min_spread_bps:
        return None

    # Tipo
    if sum_yes > 1.0:
        arb_type = "overpriced"
        profit_per_dollar = (1.0 - (1.0 / sum_yes)) if sum_yes > 0 else 0
    else:
        arb_type = "underpriced"
        profit_per_dollar = (1.0 / sum_yes - 1.0) if sum_yes > 0 else 0

    market_data.sort(key=lambda x: x["yes_price"], reverse=True)

    # Pegar a data de fim (máxima entre os mercados do evento)
    end_dates = [m.get("endDateIso", "") for m in markets if m.get("endDateIso")]
    end_date = max(end_dates) if end_dates else ""

    return ArbOpportunity(
        event_slug=event_slug,
        event_id=event_id,
        arb_type=arb_type,
        spread_bps=spread_bps,
        sum_yes=round(sum_yes, 4),
        num_markets=len(market_data),
        total_liquidity=sum(md["liquidity"] for md in market_data),
        top_markets=market_data[:10],
        profit_per_dollar=max(0, profit_per_dollar),
        end_date=end_date,
    )


# ================================================================
# Arbitrage Bot
# ================================================================

class ArbitrageBot:
    """Bot de arbitragem categórica."""

    def __init__(
        self,
        capital: float = 10.0,
        trade_size: float = 1.0,
        min_spread_bps: int = 50,
        max_spread_bps: int = 1000,
        dry_run: bool = True,
    ):
        self.capital = capital
        self.trade_size = trade_size
        self.min_spread_bps = min_spread_bps
        self.max_spread_bps = max_spread_bps
        self.dry_run = dry_run
        self.positions: List[ArbitragePosition] = []
        self._running = False
        self.state_file = os.getenv(
            "STATE_FILE", "data/arbitrage_bot_state.json"
        )
        self.max_positions = 5

        os.makedirs("data", exist_ok=True)
        self._load_state()
        signal.signal(signal.SIGINT, self._handle_shutdown)

    def _load_state(self):
        """Carrega estado anterior."""
        if not os.path.exists(self.state_file):
            return

        try:
            with open(self.state_file) as f:
                state = json.load(f)
                self.capital = state.get("capital", self.capital)
                positions = state.get("positions", [])
                self.positions = [
                    ArbitragePosition(**p) for p in positions
                ]
                logger.info(
                    f"Estado carregado: {len(self.positions)} posições, "
                    f"${self.capital:.2f} capital"
                )
        except Exception as e:
            logger.warning(f"Erro ao carregar estado: {e}")

    def _save_state(self):
        """Salva estado atual."""
        state = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "capital": round(self.capital, 2),
            "positions": [
                {
                    "event_id": p.event_id,
                    "event_slug": p.event_slug,
                    "arb_type": p.arb_type,
                    "entry_spread_bps": p.entry_spread_bps,
                    "entry_sum_yes": p.entry_sum_yes,
                    "entry_time": p.entry_time,
                    "num_markets": p.num_markets,
                    "size_usd": p.size_usd,
                    "status": p.status,
                    "exit_spread_bps": p.exit_spread_bps,
                    "exit_sum_yes": p.exit_sum_yes,
                    "pnl": p.pnl,
                }
                for p in self.positions
            ],
        }

        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)

    def run_cycle(self) -> int:
        """Executa um ciclo de busca e execução."""
        print()
        print("=" * 70)
        print(f"  ARBITRAGE BOT — Ciclo às {datetime.now().strftime('%H:%M:%S')}")
        print("=" * 70)
        print(f"  Capital: ${self.capital:.2f}")
        print(f"  Posições abertas: {len([p for p in self.positions if p.status == 'open'])}")
        print()

        # 1. Buscar mercados
        print("  [1/5] Buscando mercados...")
        markets = fetch_active_markets(limit=1500)
        print(f"        {len(markets)} mercados encontrados")
        print()

        # (Deadline removed - trading indefinitely)
        print("  [1.5/5] Buscando oportunidades (sem limite de data)...")

        # 2. Agrupar e detectar arbitragens
        print("  [2/5] Detectando arbitragens...")
        event_groups = group_markets_by_event(markets)
        opportunities = []

        for event_id, info in event_groups.items():
            if len(info["markets"]) >= 3:
                arb = detect_arbitrage(
                    event_id,
                    info["slug"],
                    info["markets"],
                    min_spread_bps=self.min_spread_bps,
                )
                if arb and arb.spread_bps <= self.max_spread_bps:
                    opportunities.append(arb)

        opportunities.sort(key=lambda x: x.spread_bps, reverse=True)
        print(f"        {len(opportunities)} arbitragens encontradas")
        print()

        # 3. Filtrar e executar
        print("  [3/5] Executando trades...")
        actionable = [
            o for o in opportunities
            if not any(p.event_id == o.event_id and p.status == "open"
                      for p in self.positions)
        ]

        trades_executed = 0
        if actionable and self.capital >= self.trade_size:
            for opp in actionable[:3]:  # Max 3 por ciclo
                if self.capital < self.trade_size:
                    break

                spread_icon = "📉" if opp.arb_type == "overpriced" else "📈"
                print(f"  {spread_icon} {opp.event_slug[:45]}")
                print(f"     Spread: {opp.spread_bps} bps | Soma: {opp.sum_yes:.4f}")
                print(f"     Mercados: {opp.num_markets} | Liq: ${opp.total_liquidity:,.0f}")

                pos = ArbitragePosition(
                    event_id=opp.event_id,
                    event_slug=opp.event_slug,
                    arb_type=opp.arb_type,
                    entry_spread_bps=opp.spread_bps,
                    entry_sum_yes=opp.sum_yes,
                    entry_time=datetime.now(timezone.utc).isoformat(),
                    num_markets=opp.num_markets,
                    size_usd=self.trade_size,
                    status="open",
                    end_date=opp.end_date,
                )

                self.positions.append(pos)
                self.capital -= self.trade_size
                trades_executed += 1
                print()
        else:
            if not actionable:
                print("  Nenhuma oportunidade nova")
            else:
                print(f"  Capital insuficiente: ${self.capital:.2f} (precisa ${self.trade_size})")
            print()

        # 4. Status
        print("  [4/4] Status")
        open_count = sum(1 for p in self.positions if p.status == "open")
        total_invested = sum(
            p.size_usd for p in self.positions if p.status == "open"
        )
        wins, losses, win_rate = self._calculate_stats()

        print(f"  RESUMO: {open_count} posições abertas | "
              f"${total_invested:.2f} investido | "
              f"${self.capital:.2f} disponível")
        if wins + losses > 0:
            print(f"  ESTATÍSTICAS: {wins}W / {losses}L ({win_rate:.1f}% win rate)")
        print()

        self._save_state()
        return trades_executed

    def _calculate_stats(self) -> tuple:
        """Calcula wins, losses e win rate % das posições fechadas."""
        closed_positions = [p for p in self.positions if p.status == "closed"]
        wins = sum(1 for p in closed_positions if p.pnl and p.pnl > 0)
        losses = sum(1 for p in closed_positions if p.pnl and p.pnl <= 0)
        total = wins + losses
        win_rate = (wins / total * 100) if total > 0 else 0
        return wins, losses, win_rate

    def run_loop(self, interval_sec: int = 300):
        """Roda o bot em loop contínuo."""
        self._running = True
        logger.info(f"Bot arbitragem iniciado. Ciclo a cada {interval_sec}s.")

        while self._running:
            try:
                self.run_cycle()

                if not self._running:
                    break

                # Countdown
                print(f"  Próximo ciclo em {interval_sec}s... (Ctrl+C para parar)")
                for remaining in range(interval_sec, 0, -30):
                    if not self._running:
                        break
                    time.sleep(min(30, remaining))

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Erro no ciclo: {e}", exc_info=True)
                time.sleep(30)

        self._shutdown()

    def _handle_shutdown(self, signum, frame):
        logger.info("\nShutdown solicitado...")
        self._running = False

    def _shutdown(self):
        self._save_state()
        logger.info("Bot parado.")


# ================================================================
# Main
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Bot de arbitragem categórica automatizado"
    )
    parser.add_argument(
        "--capital", type=float, default=10.0,
        help="Capital inicial em USDC (padrão: 10.0)",
    )
    parser.add_argument(
        "--trade-size", type=float, default=1.0,
        help="Tamanho de cada trade em USDC (padrão: 1.0)",
    )
    parser.add_argument(
        "--min-spread", type=int, default=50,
        help="Spread mínimo em bps (padrão: 50 = 0.5%%)",
    )
    parser.add_argument(
        "--max-spread", type=int, default=1000,
        help="Spread máximo em bps (padrão: 1000 = 10%%)",
    )
    parser.add_argument(
        "--interval", type=int, default=300,
        help="Intervalo entre ciclos em segundos (padrão: 300 = 5 min)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Modo simulação (padrão: True)",
    )
    parser.add_argument(
        "--live", action="store_true", default=False,
        help="Modo real com ordens reais",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Roda apenas um ciclo e para",
    )

    args = parser.parse_args()

    print("=" * 70)
    print("  ARBITRAGE BOT — Arbitragem Combinatória")
    print("=" * 70)
    print(f"  Capital: ${args.capital:.2f}")
    print(f"  Tamanho trade: ${args.trade_size:.2f}")
    print(f"  Spread: {args.min_spread}-{args.max_spread} bps")
    print(f"  Modo: {'LIVE' if args.live else 'DRY-RUN'}")
    print()

    bot = ArbitrageBot(
        capital=args.capital,
        trade_size=args.trade_size,
        min_spread_bps=args.min_spread,
        max_spread_bps=args.max_spread,
        dry_run=not args.live,
    )

    if args.once:
        bot.run_cycle()
    else:
        bot.run_loop(interval_sec=args.interval)


if __name__ == "__main__":
    main()
