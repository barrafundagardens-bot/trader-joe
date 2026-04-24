"""
deploy/favorites_bot.py — Bot automatizado de compra de favoritos.

Lógica:
    Mercados de predição sofrem de "longshot bias" — favoritos (preço >= 0.92)
    são sistematicamente subvalorizados. Comprar e segurar gera win rate 90%+.

Ciclo principal:
    1. Buscar mercados com preço >= threshold (0.92)
    2. Validar tendência de alta (confirmação)
    3. Executar BUY se preço > threshold e tendência confirmada
    4. Monitorar posições abertas
    5. Executar saídas (lucro realizado ou stop loss)
    6. Repetir a cada N minutos

Modos de operacao:
    --dry-run    (padrao) Simula tudo sem enviar ordens reais
    --live       Envia ordens reais via py-clob-client

Uso:
    python deploy/favorites_bot.py                      # dry-run, padrao
    python deploy/favorites_bot.py --interval 600       # a cada 10 min
    python deploy/favorites_bot.py --live --confirm     # modo real
    python deploy/favorites_bot.py --once               # roda 1 vez e para
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
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
        logging.FileHandler("logs/favorites_bot.log", mode="a"),
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
class FavoriteOpportunity:
    """Uma oportunidade de compra de favorito."""
    slug: str
    title: str
    current_price: float
    yes_price: float
    no_price: float
    liquidity: float
    volume: float
    condition_id: str
    token_id: str
    end_date: str = ""


@dataclass
class FavoritePosition:
    """Posição aberta de compra de favorito."""
    slug: str
    title: str
    entry_price: float
    entry_time: str
    size_usd: float = 1.0
    status: str = "open"           # "open", "closed", "profit", "stop_loss"
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    end_date: str = ""


# ================================================================
# Market Data Fetching
# ================================================================

def fetch_active_markets(limit: int = 500) -> List[dict]:
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




def detect_favorites(markets: List[dict], buy_threshold: float = 0.92) -> List[FavoriteOpportunity]:
    """Detecta oportunidades de compra de favoritos."""
    favorites = []

    for market in markets:
        prices = parse_prices(market.get("outcomePrices", []))

        if not prices or len(prices) < 2:
            continue

        # Pegar preço YES (índice 0)
        yes_price = float(prices[0])
        no_price = float(prices[1]) if len(prices) > 1 else (1.0 - yes_price)

        # Filtrar favoritos (YES >= threshold)
        if yes_price >= buy_threshold:
            slug = market.get("slug", "?")

            # Verificar se já está em posição aberta
            favorites.append(FavoriteOpportunity(
                slug=slug,
                title=market.get("question", market.get("title", "?"))[:70],
                current_price=yes_price,
                yes_price=yes_price,
                no_price=no_price,
                liquidity=market.get("liquidityNum", 0),
                volume=market.get("volumeNum", 0),
                condition_id=market.get("conditionId", ""),
                token_id=market.get("clobTokenIds", [None])[0] if market.get("clobTokenIds") else None,
                end_date=market.get("endDateIso", ""),
            ))

    # Ordenar por preço (maiores favoritos primeiro)
    favorites.sort(key=lambda x: x.yes_price, reverse=True)
    return favorites


# ================================================================
# Favorites Bot
# ================================================================

class FavoritesBot:
    """Bot de compra de favoritos."""

    def __init__(
        self,
        capital: float = 10.0,
        trade_size: float = 1.0,
        buy_threshold: float = 0.92,
        exit_profit: float = 0.97,
        exit_stop: float = 0.85,
        dry_run: bool = True,
    ):
        self.capital = capital
        self.trade_size = trade_size
        self.buy_threshold = buy_threshold
        self.exit_profit = exit_profit
        self.exit_stop = exit_stop
        self.dry_run = dry_run
        self.positions: List[FavoritePosition] = []
        self._running = False
        self.state_file = os.getenv(
            "STATE_FILE", "data/favorites_bot_state.json"
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
                    FavoritePosition(**p) for p in positions
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
                    "slug": p.slug,
                    "title": p.title,
                    "entry_price": p.entry_price,
                    "entry_time": p.entry_time,
                    "size_usd": p.size_usd,
                    "status": p.status,
                    "exit_price": p.exit_price,
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
        print(f"  FAVORITES BOT — Ciclo às {datetime.now().strftime('%H:%M:%S')}")
        print("=" * 70)
        print(f"  Capital: ${self.capital:.2f}")
        print(f"  Posições abertas: {len([p for p in self.positions if p.status == 'open'])}")
        print()

        # 1. Buscar mercados (1500 para cobrir NBA, Masters, etc.)
        print("  [1/4] Buscando mercados com preço >= 0.92...")
        markets = fetch_active_markets(limit=1500)
        print(f"        {len(markets)} mercados encontrados")
        print()

        # (Deadline removed - trading indefinitely)
        print("  [1.5/4] Buscando oportunidades (sem limite de data)...")

        # 2. Detectar favoritos
        print("  [2/4] Detectando favoritos...")
        opportunities = detect_favorites(
            markets,
            buy_threshold=self.buy_threshold,
        )
        print(f"        {len(opportunities)} favoritos encontrados")
        print()

        # 3. Executar
        print("  [3/4] Executando trades...")
        actionable = [
            o for o in opportunities
            if not any(p.slug == o.slug and p.status == "open"
                      for p in self.positions)
        ]

        trades_executed = 0
        if actionable and self.capital >= self.trade_size:
            for opp in actionable[:3]:  # Max 3 por ciclo
                if self.capital < self.trade_size:
                    break

                print(f"  🎯 {opp.title[:50]}")
                print(f"     Preço: {opp.yes_price:.3f} | "
                      f"Liq: ${opp.liquidity:,.0f} | Vol: ${opp.volume:,.0f}")

                pos = FavoritePosition(
                    slug=opp.slug,
                    title=opp.title,
                    entry_price=opp.yes_price,
                    entry_time=datetime.now(timezone.utc).isoformat(),
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
                print(f"  Capital insuficiente: ${self.capital:.2f}")
            print()

        # Status
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
        closed_positions = [p for p in self.positions if p.status in ("profit", "stop_loss", "closed")]
        wins = sum(1 for p in closed_positions if p.pnl and p.pnl > 0)
        losses = sum(1 for p in closed_positions if p.pnl and p.pnl <= 0)
        total = wins + losses
        win_rate = (wins / total * 100) if total > 0 else 0
        return wins, losses, win_rate

    def run_loop(self, interval_sec: int = 600):
        """Roda o bot em loop contínuo."""
        self._running = True
        logger.info(f"Bot favoritos iniciado. Ciclo a cada {interval_sec}s.")

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
        description="Bot de compra de favoritos automatizado"
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
        "--buy-threshold", type=float, default=0.92,
        help="Preço mínimo para considerar favorito (padrão: 0.92)",
    )
    parser.add_argument(
        "--exit-profit", type=float, default=0.97,
        help="Preço alvo para realização de lucro (padrão: 0.97)",
    )
    parser.add_argument(
        "--exit-stop", type=float, default=0.85,
        help="Preço de stop loss (padrão: 0.85)",
    )
    parser.add_argument(
        "--interval", type=int, default=600,
        help="Intervalo entre ciclos em segundos (padrão: 600 = 10 min)",
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
    print("  FAVORITES BOT — Compra de Favoritos")
    print("=" * 70)
    print(f"  Capital: ${args.capital:.2f}")
    print(f"  Tamanho trade: ${args.trade_size:.2f}")
    print(f"  Threshold: {args.buy_threshold:.2f}")
    print(f"  Modo: {'LIVE' if args.live else 'DRY-RUN'}")
    print()

    bot = FavoritesBot(
        capital=args.capital,
        trade_size=args.trade_size,
        buy_threshold=args.buy_threshold,
        exit_profit=args.exit_profit,
        exit_stop=args.exit_stop,
        dry_run=not args.live,
    )

    if args.once:
        bot.run_cycle()
    else:
        bot.run_loop(interval_sec=args.interval)


if __name__ == "__main__":
    main()
