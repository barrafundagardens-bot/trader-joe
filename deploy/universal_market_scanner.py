"""
deploy/universal_market_scanner.py — Scanner universal de TODOS os mercados.

Diferenças:
    1. Busca TODOS os mercados (sem limite de 1000)
    2. Filtra por liquidez E volume
    3. Detecta mercados por categoria (sports, crypto, politics, etc)
    4. Busca pelo nome da pergunta (ex: "Tenis", "Rafa", "Djoko")
    5. Retorna em JSON para fácil integração

Uso:
    # Buscar todos
    python deploy/universal_market_scanner.py

    # Buscar apenas tênis com liquidez >= $5k
    python deploy/universal_market_scanner.py --search "tenis" --min-liquidity 5000

    # Buscar apenas mercados que resolvem em 7 dias
    python deploy/universal_market_scanner.py --days 7

    # Buscar apenas esportes com odds altas (prédito clara)
    python deploy/universal_market_scanner.py --search "tennis" --min-confidence 0.8

    # Buscar e salvar para traders executarem
    python deploy/universal_market_scanner.py --output markets.json --format csv
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/universal_scanner.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class Market:
    """Representa um mercado do Polymarket."""
    question: str
    slug: str
    condition_id: str
    token_id: Optional[str]
    liquidity: float
    volume: float
    yes_price: float
    no_price: float
    days_to_resolve: float
    url: str
    category: str
    is_open: bool

    @property
    def spread(self) -> float:
        """Spread YES-NO em pontos base (bps)."""
        return abs(self.yes_price - self.no_price) * 10000

    @property
    def confidence(self) -> float:
        """Quanto um lado está 'favoritado' (mais longe de 0.5)."""
        return abs(self.yes_price - 0.5) * 2

    @property
    def score(self) -> float:
        """Score composto para ranking."""
        liq = min(1.0, self.liquidity / 5000)
        vol = min(1.0, self.volume / 10000)
        conf = self.confidence
        return liq * 0.4 + vol * 0.3 + conf * 0.3


def fetch_all_markets() -> List[dict]:
    """Busca TODOS os mercados da Gamma API usando paginação."""
    markets = []
    offset = 0
    batch_size = 100

    print("  📥 Buscando todos os mercados (paginação)...")

    while True:
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": batch_size,
                    "offset": offset,
                },
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json()

            if not isinstance(batch, list) or not batch:
                logger.info(f"Fim da paginação em offset={offset}")
                break

            markets.extend(batch)
            offset += len(batch)
            pct = (offset / 10000) * 100 if offset < 10000 else 100
            print(f"    ... {len(markets):>5} mercados | offset {offset:>5}")

            if len(batch) < batch_size:
                break

            time.sleep(0.2)

        except Exception as e:
            logger.error(f"Erro na paginação: {e}")
            break

    logger.info(f"✅ Encontrados {len(markets)} mercados totais")
    return markets


def parse_market(m: dict) -> Optional[Market]:
    """Converte um mercado da API em objeto Market."""
    try:
        # Extrair dados básicos
        question = m.get("question", "?")[:150]
        slug = m.get("slug", "")
        condition_id = m.get("conditionId", "")
        token_ids = m.get("clobTokenIds", [])
        token_id = token_ids[0] if token_ids else None
        liquidity = float(m.get("liquidityNum", 0))
        volume = float(m.get("volumeNum", 0))
        outcomes = m.get("outcomes", [])
        prices = m.get("outcomePrices", [])

        # Parse prices se for string
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except:
                prices = []

        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except:
                outcomes = ["YES", "NO"]

        # Extrair preços YES/NO
        yes_price = float(prices[0]) if len(prices) > 0 else 0.5
        no_price = float(prices[1]) if len(prices) > 1 else 0.5

        # Calcular dias até resolução
        end_str = m.get("endDate", "")
        if end_str:
            if "T" in end_str:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                days_to_resolve = (end_dt - now).total_seconds() / 86400
            else:
                days_to_resolve = 999
        else:
            days_to_resolve = 999

        # Detectar categoria
        q_lower = question.lower()
        if any(w in q_lower for w in ["tennis", "tennis", "wimbledon", "atp", "wta", "rafa", "djoko", "court"]):
            category = "Tennis"
        elif any(w in q_lower for w in ["nba", "lakers", "celtics", "warriors", "basketball"]):
            category = "NBA"
        elif any(w in q_lower for w in ["nfl", "football", "super bowl", "chiefs", "cowboys"]):
            category = "NFL"
        elif any(w in q_lower for w in ["bitcoin", "eth", "crypto", "price"]):
            category = "Crypto"
        elif any(w in q_lower for w in ["election", "trump", "president", "politics"]):
            category = "Politics"
        elif any(w in q_lower for w in ["weather", "rain", "snow", "temperature", "forecast"]):
            category = "Weather"
        else:
            category = "Other"

        # Checar se é aberto
        is_open = days_to_resolve > 0 and yes_price > 0

        url = f"https://polymarket.com/{slug}" if slug else ""

        return Market(
            question=question,
            slug=slug,
            condition_id=condition_id,
            token_id=token_id,
            liquidity=liquidity,
            volume=volume,
            yes_price=yes_price,
            no_price=no_price,
            days_to_resolve=days_to_resolve,
            url=url,
            category=category,
            is_open=is_open,
        )

    except Exception as e:
        logger.debug(f"Erro parseando mercado: {e}")
        return None


def filter_markets(
    markets: List[Market],
    search: Optional[str] = None,
    min_liquidity: float = 0.0,
    max_days: float = 30.0,
    min_confidence: float = 0.0,
    category: Optional[str] = None,
) -> List[Market]:
    """Filtra mercados por critérios."""
    filtered = markets

    if search:
        search_lower = search.lower()
        filtered = [m for m in filtered if search_lower in m.question.lower()]

    if min_liquidity > 0:
        filtered = [m for m in filtered if m.liquidity >= min_liquidity]

    if max_days < 999:
        filtered = [m for m in filtered if 0 < m.days_to_resolve <= max_days]

    if min_confidence > 0:
        filtered = [m for m in filtered if m.confidence >= min_confidence]

    if category:
        filtered = [m for m in filtered if m.category.lower() == category.lower()]

    return filtered


def display_markets(markets: List[Market], top_n: int = 20):
    """Exibe mercados formatados."""
    print()
    print("=" * 120)
    print(f"  MERCADOS ENCONTRADOS ({len(markets)} resultado{'s' if len(markets) != 1 else ''})")
    print("=" * 120)

    if not markets:
        print("  ❌ Nenhum mercado encontrado.")
        return

    # Ordenar por score
    markets.sort(key=lambda m: -m.score)

    for i, m in enumerate(markets[:top_n], 1):
        conf_pct = m.confidence * 100
        conf_icon = "🔴" if conf_pct < 20 else "🟡" if conf_pct < 40 else "🟢"
        liq_icon = "🔴" if m.liquidity < 500 else "🟡" if m.liquidity < 5000 else "🟢"

        print(f"\n  {i:>2}. {m.category:>10} | {conf_icon} {conf_pct:>5.1f}% | {liq_icon} ${m.liquidity:>10,.0f}")
        print(f"      Q: {m.question}")
        print(f"      Preços: YES ${m.yes_price:.3f} | NO ${m.no_price:.3f} | Spread: {m.spread:.0f} bps")
        print(f"      Liquidity: ${m.liquidity:,.0f} | Volume: ${m.volume:,.0f} | Resolve: {m.days_to_resolve:.1f} dias")
        print(f"      🔗 {m.url}")

    print()
    print("=" * 120)


def save_results(markets: List[Market], output_file: str, fmt: str = "json"):
    """Salva resultados em arquivo."""
    os.makedirs("results", exist_ok=True)
    path = f"results/{output_file}"

    if fmt == "json":
        data = [asdict(m) for m in markets]
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    elif fmt == "csv":
        import csv
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=asdict(markets[0]).keys() if markets else [])
            writer.writeheader()
            for m in markets:
                writer.writerow(asdict(m))

    logger.info(f"Resultados salvos: {path}")
    print(f"  💾 Salvo: {path}")


def main():
    parser = argparse.ArgumentParser(
        description="Universal Market Scanner — TODOS os mercados do Polymarket",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  Buscar todos os mercados:
    python deploy/universal_market_scanner.py

  Buscar tênis com liquidez >= $5k:
    python deploy/universal_market_scanner.py --search "tennis" --min-liquidity 5000

  Buscar esportes que resolvem em 7 dias:
    python deploy/universal_market_scanner.py --search "tennis" --days 7

  Buscar com odds claras (>=80% confiança) e alta liquidez:
    python deploy/universal_market_scanner.py --min-confidence 0.8 --min-liquidity 1000

  Salvar em JSON para integração:
    python deploy/universal_market_scanner.py --output markets.json --format json
        """,
    )

    parser.add_argument(
        "--search", type=str, default=None,
        help="Buscar por keyword (ex: 'tennis', 'bitcoin', 'election')",
    )
    parser.add_argument(
        "--min-liquidity", type=float, default=0.0,
        help="Liquidez mínima em USDC (padrão: 0 = sem filtro)",
    )
    parser.add_argument(
        "--days", type=float, default=30.0,
        help="Máximo dias até resolução (padrão: 30)",
    )
    parser.add_argument(
        "--min-confidence", type=float, default=0.0,
        help="Confiança mínima (0.0-1.0, onde 0.5 = 50% de vantagem)",
    )
    parser.add_argument(
        "--category", type=str, default=None,
        help="Filtrar por categoria (Tennis, NBA, NFL, Crypto, Politics, Weather, Other)",
    )
    parser.add_argument(
        "--top", type=int, default=20,
        help="Mostrar top N (padrão: 20)",
    )
    parser.add_argument(
        "--output", type=str, default=f"markets_{int(time.time())}.json",
        help="Salvar em arquivo (padrão: markets_<timestamp>.json)",
    )
    parser.add_argument(
        "--format", type=str, choices=["json", "csv"], default="json",
        help="Formato de saída (padrão: json)",
    )

    args = parser.parse_args()

    print()
    print("=" * 120)
    print("  UNIVERSAL MARKET SCANNER — Todos os mercados do Polymarket")
    print("=" * 120)

    # Buscar
    print()
    raw_markets = fetch_all_markets()

    # Parse
    print(f"  🔍 Parseando {len(raw_markets)} mercados...")
    markets = [m for m in [parse_market(raw) for raw in raw_markets] if m]
    logger.info(f"✅ Parseados {len(markets)} mercados com sucesso")

    # Filtrar
    print(f"  🔎 Aplicando filtros...")
    filtered = filter_markets(
        markets,
        search=args.search,
        min_liquidity=args.min_liquidity,
        max_days=args.days,
        min_confidence=args.min_confidence,
        category=args.category,
    )
    logger.info(f"Após filtros: {len(filtered)} mercados")

    # Exibir
    display_markets(filtered, top_n=args.top)

    # Salvar
    if filtered:
        save_results(filtered, args.output, fmt=args.format)

    print()


if __name__ == "__main__":
    main()
