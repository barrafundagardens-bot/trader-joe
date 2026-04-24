"""
deploy/whale_opportunity_finder.py — Finder de oportunidades PRECISAS.

Diferenças do whale_trader.py:
    1. Busca APENAS mercados ABERTOS (endDate no futuro, active=true)
    2. Valida liquidez mínima (precisa ter volume/bid-ask spreads)
    3. Cruza com posições de whales ATIVAS
    4. Retorna apenas oportunidades TRADÁVEIS AGORA
    5. Inclui link direto para cada mercado

Usa:
    python deploy/whale_opportunity_finder.py
    python deploy/whale_opportunity_finder.py --min-liquidity 1000
    python deploy/whale_opportunity_finder.py --days-to-resolve 7
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/opportunity_finder.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)


# ================================================================
# Config
# ================================================================

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

WHALE_LIST = [
    {"rank": 1,  "name": "0x4924...",            "address": "0x492442eab586f242b53bda933fd5de859c8a3782"},
    {"rank": 2,  "name": "HorizonSplendidView",  "address": "0x02227b8f5a9636e895607edd3185ed6ee5598ff7"},
    {"rank": 3,  "name": "reachingthesky",        "address": "0xefbc5fec8d7b0acdc8911bdd9a98d6964308f9a2"},
    {"rank": 4,  "name": "beachboy4",             "address": "0xc2e7800b5af46e6093872b177b7a5e7f0563be51"},
    {"rank": 6,  "name": "bcda",                  "address": "0xb45a797faa52b0fd8adc56d30382022b7b12192c"},
    {"rank": 7,  "name": "Countryside",           "address": "0xbddf61af533ff524d27154e589d2d7a81510c684"},
    {"rank": 8,  "name": "0x2a2C...",             "address": "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1"},
    {"rank": 9,  "name": "sovereign2013",         "address": "0xee613b3fc183ee44f9da9c05f53e2da107e3debf"},
    {"rank": 10, "name": "RN1",                   "address": "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"},
    {"rank": 16, "name": "swisstony",             "address": "0x204f72f35326db932158cba6adff0b9a1da95e14"},
    {"rank": 20, "name": "GamblingIsAllYouNeed",  "address": "0x507e52ef684ca2dd91f90a9d26d149dd3288beae"},
]


# ================================================================
# Data structures
# ================================================================

@dataclass
class OpenMarket:
    """Um mercado aberto no Polymarket agora."""
    question: str
    slug: str
    token_id: str
    condition_id: str
    end_date: str
    liquidity: float
    volume: float
    outcome_prices: List[float]
    outcomes: List[str]
    url: str
    days_to_resolve: float


@dataclass
class OpportunityPrecise:
    """Oportunidade com whales + mercado aberto = TRADÁVEL."""
    question: str
    slug: str
    url: str
    days_to_resolve: float
    whale_count: int
    whales: List[str]
    best_whale_rank: int
    avg_whale_entry: float
    avg_whale_pnl: float
    current_yes_price: float
    current_no_price: float
    liquidity: float
    volume: float
    confidence: str
    recommendation: str  # "BUY YES" ou "BUY NO"
    entry_target: float
    reason: str

    @property
    def score(self) -> float:
        whale_score = self.whale_count * 25
        pnl_score = max(0, self.avg_whale_pnl) * 2
        rank_score = max(0, 21 - self.best_whale_rank) * 2
        liq_score = min(15, self.liquidity / 5000)
        days_score = max(0, 10 - self.days_to_resolve) * 1.5
        return whale_score + pnl_score + rank_score + liq_score + days_score


# ================================================================
# API Fetchers
# ================================================================

def fetch_open_markets(
    min_liquidity: float = 500.0,
    max_days_to_resolve: float = 30.0,
) -> List[OpenMarket]:
    """Busca APENAS mercados abertos, ativos, com liquidez mínima."""
    try:
        markets = []
        offset = 0

        logger.info("Buscando todos os mercados (com paginação)...")

        # Buscar mercados ativos com paginação
        while True:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "offset": offset,
                },
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json()

            if not isinstance(batch, list):
                logger.warning(f"Resposta inesperada: {type(batch)}")
                break

            if not batch:
                logger.info(f"Fim da paginação em offset={offset}")
                break

            markets.extend(batch)
            offset += len(batch)
            logger.debug(f"Página {offset//100}: +{len(batch)} mercados (total: {len(markets)})")

            if len(batch) < 100:
                break

            time.sleep(0.2)  # Rate limiting

        open_markets = []
        now = datetime.now(timezone.utc)

        for m in markets:
            try:
                # Parse end_date
                end_str = m.get("endDate", "")
                if not end_str:
                    continue

                # Converter ISO para datetime
                if "T" in end_str:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                else:
                    continue

                # Validar: resolve no futuro?
                if end_dt <= now:
                    continue

                days_to_resolve = (end_dt - now).total_seconds() / 86400

                # Filtrar por dias
                if days_to_resolve > max_days_to_resolve:
                    continue

                # Liquidez
                liquidity = m.get("liquidityNum", 0)
                if liquidity < min_liquidity:
                    continue

                # Parse outcomePrices (pode ser string JSON)
                outcome_prices = m.get("outcomePrices", [])
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = json.loads(outcome_prices)
                    except:
                        continue

                if not isinstance(outcome_prices, list) or len(outcome_prices) < 2:
                    continue

                # Parse outcomes
                outcomes = m.get("outcomes", [])
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except:
                        outcomes = ["YES", "NO"]

                # Construir URL
                slug = m.get("slug", "")
                url = f"https://polymarket.com/{slug}" if slug else ""

                market = OpenMarket(
                    question=m.get("question", "?")[:100],
                    slug=slug,
                    token_id=m.get("clobTokenIds", [""])[0] if m.get("clobTokenIds") else "",
                    condition_id=m.get("conditionId", ""),
                    end_date=end_str,
                    liquidity=float(liquidity),
                    volume=float(m.get("volumeNum", 0)),
                    outcome_prices=[float(p) for p in outcome_prices],
                    outcomes=outcomes,
                    url=url,
                    days_to_resolve=days_to_resolve,
                )

                open_markets.append(market)

            except Exception as e:
                logger.debug(f"Erro processando mercado: {e}")
                continue

        logger.info(f"Encontrados {len(open_markets)} mercados abertos com liquidez >= ${min_liquidity}")
        return open_markets

    except Exception as e:
        logger.error(f"Erro ao buscar mercados: {e}")
        return []


def fetch_whale_positions(
    address: str, min_value: float = 500.0
) -> List[dict]:
    """Busca posições ativas de uma whale."""
    try:
        resp = requests.get(
            f"{DATA_API}/positions",
            params={"user": address},
            timeout=15,
        )
        resp.raise_for_status()
        positions = resp.json()

        if not isinstance(positions, list):
            return []

        active = [
            p for p in positions
            if isinstance(p.get("currentValue"), (int, float))
            and p["currentValue"] >= min_value
        ]

        return active

    except Exception as e:
        logger.debug(f"Erro ao buscar posições: {e}")
        return []


# ================================================================
# Core Logic
# ================================================================

def cross_whales_with_markets(
    open_markets: List[OpenMarket],
    whale_list: List[dict],
    min_whales: int = 2,
    delay: float = 0.3,
) -> List[OpportunityPrecise]:
    """
    Cruza posições de whales com mercados abertos.
    Retorna APENAS oportunidades onde:
      - Mercado está ABERTO AGORA
      - 2+ whales estão posicionadas
      - Tem liquidez
    """
    opportunities = []

    # Index mercados por slug
    markets_by_slug: Dict[str, OpenMarket] = {m.slug: m for m in open_markets}

    # Buscar posições de whales
    whale_positions_by_slug: Dict[str, list] = defaultdict(list)

    for whale in whale_list:
        positions = fetch_whale_positions(whale["address"])

        for pos in positions:
            slug = pos.get("slug", "")
            if slug in markets_by_slug:  # Só interesse em mercados abertos
                pos["_whale_name"] = whale["name"]
                pos["_whale_rank"] = whale["rank"]
                whale_positions_by_slug[slug].append(pos)

        time.sleep(delay)

    logger.info(
        f"Encontradas {sum(len(v) for v in whale_positions_by_slug.values())} "
        f"posições de whales em mercados abertos"
    )

    # Encontrar consenso
    for slug, positions in whale_positions_by_slug.items():
        whale_names = list(set(p["_whale_name"] for p in positions))

        if len(whale_names) < min_whales:
            continue

        market = markets_by_slug[slug]

        # Calcular stats
        total_value = sum(p.get("currentValue", 0) for p in positions)
        total_size = sum(p.get("size", 0) for p in positions)
        pnls = [p.get("percentPnl", 0) for p in positions]
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0
        avg_entry = (
            sum(p.get("avgPrice", 0) * p.get("size", 0) for p in positions)
            / total_size
            if total_size > 0
            else 0.5
        )
        best_rank = min(p["_whale_rank"] for p in positions)

        # Confidence
        n = len(whale_names)
        if n >= 4:
            confidence = "ALTA"
        elif n >= 3:
            confidence = "MEDIA"
        else:
            confidence = "MODERADA"

        # Determinar recomendação (YES ou NO)
        yes_price = market.outcome_prices[0] if len(market.outcome_prices) > 0 else 0.5
        no_price = market.outcome_prices[1] if len(market.outcome_prices) > 1 else 0.5

        # Whales entraram mais perto de YES ou NO?
        if avg_entry < 0.5:
            recommendation = "BUY NO"
            entry_target = no_price
        else:
            recommendation = "BUY YES"
            entry_target = yes_price

        reason = f"{n} whales consenso (avg entry {avg_entry:.3f}, PnL {avg_pnl:+.1f}%)"

        opp = OpportunityPrecise(
            question=market.question,
            slug=market.slug,
            url=market.url,
            days_to_resolve=market.days_to_resolve,
            whale_count=n,
            whales=whale_names,
            best_whale_rank=best_rank,
            avg_whale_entry=round(avg_entry, 4),
            avg_whale_pnl=round(avg_pnl, 1),
            current_yes_price=round(yes_price, 4),
            current_no_price=round(no_price, 4),
            liquidity=round(market.liquidity, 2),
            volume=round(market.volume, 2),
            confidence=confidence,
            recommendation=recommendation,
            entry_target=round(entry_target, 4),
            reason=reason,
        )

        opportunities.append(opp)

    # Ordenar por score
    opportunities.sort(key=lambda o: -o.score)
    return opportunities


# ================================================================
# Display
# ================================================================

def display_opportunities(opps: List[OpportunityPrecise], top_n: int = 10):
    """Exibe oportunidades formatadas."""
    print()
    print("=" * 85)
    print("  WHALE OPPORTUNITIES — Mercados ABERTOS + Consenso de Whales")
    print("=" * 85)
    print()

    if not opps:
        print("  ❌ Nenhuma oportunidade encontrada.")
        print()
        return

    print(f"  Encontradas {len(opps)} oportunidades precisas:\n")

    for i, opp in enumerate(opps[:top_n], 1):
        conf_icon = {"ALTA": "🟢", "MEDIA": "🟡", "MODERADA": "🟠"}[opp.confidence]
        pnl_icon = "🟢" if opp.avg_whale_pnl > 0 else "🔴"

        print(f"  {i:>2}. {conf_icon} {opp.question}")
        print(f"      Resolve em: {opp.days_to_resolve:.1f} dias")
        print(f"      {opp.whale_count} whales | Confiança: {opp.confidence}")
        print(f"      Whales: {', '.join(opp.whales[:4])}")
        print()
        print(f"      Preços agora:     YES ${opp.current_yes_price:.3f} | NO ${opp.current_no_price:.3f}")
        print(f"      Entrada das whales: ${opp.avg_whale_entry:.3f}")
        print(f"      PnL das whales: {pnl_icon} {opp.avg_whale_pnl:+.1f}%")
        print()
        print(f"      💡 RECOMENDAÇÃO: {opp.recommendation} @ ${opp.entry_target:.3f}")
        print(f"      Liquidez: ${opp.liquidity:,.0f} | Volume: ${opp.volume:,.0f}")
        print()
        print(f"      🔗 Link: {opp.url}")
        print(f"      Score: {opp.score:.0f}")
        print()

    # Save to JSON
    os.makedirs("results", exist_ok=True)
    path = f"results/opportunities_{int(time.time())}.json"
    with open(path, "w") as f:
        json.dump([{
            "question": o.question,
            "slug": o.slug,
            "url": o.url,
            "days_to_resolve": o.days_to_resolve,
            "whale_count": o.whale_count,
            "whales": o.whales,
            "confidence": o.confidence,
            "recommendation": o.recommendation,
            "entry_target": o.entry_target,
            "current_yes": o.current_yes_price,
            "current_no": o.current_no_price,
            "liquidity": o.liquidity,
            "whale_pnl": o.avg_whale_pnl,
            "score": o.score,
        } for o in opps[:top_n]], f, indent=2)

    print(f"  Resultados salvos: {path}")
    print()


# ================================================================
# Main
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Whale Opportunity Finder — Mercados abertos + Consenso de whales",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  Buscar oportunidades padrão:
    python deploy/whale_opportunity_finder.py

  Mais rigoroso (liquidez >= $5k):
    python deploy/whale_opportunity_finder.py --min-liquidity 5000

  Apenas mercados que resolvem nos próximos 7 dias:
    python deploy/whale_opportunity_finder.py --days 7

  Top 20 oportunidades:
    python deploy/whale_opportunity_finder.py --top 20
        """,
    )

    parser.add_argument(
        "--min-liquidity", type=float, default=500.0,
        help="Liquidez mínima em USDC (padrão: $500)",
    )
    parser.add_argument(
        "--days", type=float, default=30.0,
        help="Máximo de dias até resolução (padrão: 30)",
    )
    parser.add_argument(
        "--min-whales", type=int, default=2,
        help="Mínimo de whales para consenso (padrão: 2)",
    )
    parser.add_argument(
        "--top", type=int, default=10,
        help="Quantas oportunidades exibir (padrão: 10)",
    )

    args = parser.parse_args()

    print()
    print("=" * 85)
    print("  WHALE OPPORTUNITY FINDER")
    print("=" * 85)
    print(f"  Buscando mercados abertos...")
    print(f"  Liquidez mínima: ${args.min_liquidity:,.0f}")
    print(f"  Dias até resolução: <= {args.days:.0f}")
    print(f"  Mínimo whales: {args.min_whales}")
    print()

    # Step 1: Buscar mercados abertos
    print("  [1/2] Buscando mercados abertos no Polymarket...")
    open_markets = fetch_open_markets(
        min_liquidity=args.min_liquidity,
        max_days_to_resolve=args.days,
    )

    if not open_markets:
        print("  ❌ Nenhum mercado aberto encontrado.")
        sys.exit(1)

    # Step 2: Cruzar com whales
    print(f"  [2/2] Cruzando com posições de {len(WHALE_LIST)} whales...")
    print()

    opportunities = cross_whales_with_markets(
        open_markets,
        WHALE_LIST,
        min_whales=args.min_whales,
    )

    # Display
    display_opportunities(opportunities, top_n=args.top)

    if opportunities:
        print("=" * 85)
        print("  ✅ Oportunidades encontradas!")
        print("  👉 Clique nos links acima para abrir no Polymarket")
        print("  👉 Siga a recomendação (BUY YES ou BUY NO)")
        print("=" * 85)
        print()


if __name__ == "__main__":
    main()
