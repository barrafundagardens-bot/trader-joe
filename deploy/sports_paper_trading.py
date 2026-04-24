"""
deploy/sports_paper_trading.py — Paper trading de ESPORTES EM GERAL.

Workflow:
    1. Escaneia TODOS os mercados de esportes (sem filtros agressivos)
    2. Filtra os com liquidez >= $1k (bem relaxo)
    3. Lista as melhores oportunidades por odds
    4. Simula trades com estratégia configurável

Uso:
    # Rodar com scan amplo de esportes + FVG Multi-TF
    python deploy/sports_paper_trading.py --strategy fvg_multitf --min-liquidity 1000

    # Scan mais agressivo: até $500 de liquidez
    python deploy/sports_paper_trading.py --strategy fvg_multitf --min-liquidity 500

    # Teste com MACD
    python deploy/sports_paper_trading.py --strategy macd --min-liquidity 1000

    # RSI em mercados menos óbvios
    python deploy/sports_paper_trading.py --strategy rsi --min-liquidity 750

    # Teste ensemble (combina várias estratégias)
    python deploy/sports_paper_trading.py --strategy ensemble --min-liquidity 1000
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.makedirs("logs", exist_ok=True)
os.makedirs("results", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/sports_paper_trading.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"

# Palavras-chave para detectar esportes
SPORTS_KEYWORDS = [
    "nba", "nfl", "mlb", "nhl",  # EUA
    "soccer", "football", "champions", "premier league", "laliga", "serie a",  # Soccer
    "tennis", "atp", "wta", "wimbledon", "us open", "french open", "australian open",  # Tennis
    "golf", "pga", "masters", "us open", "open championship",  # Golf
    "boxing", "mma", "ufc", "wrestling",  # Combat
    "formula 1", "f1", "motogp", "nascar",  # Racing
    "rugby", "cricket",  # Outros esportes
    "olympic", "world cup",  # Eventos grandes
]


def fetch_all_sports_markets(min_liquidity: float = 500.0) -> List[dict]:
    """Busca TODOS os mercados de esportes com paginação."""
    all_markets = []
    offset = 0
    batch_size = 100

    print(f"\n  📊 Escaneando TODOS os mercados de esportes...")
    print(f"     Liquidez mínima: ${min_liquidity:,.0f}")

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
                break

            all_markets.extend(batch)
            offset += len(batch)
            print(f"     ... {len(all_markets):>5} mercados encontrados | offset {offset}")

            if len(batch) < batch_size:
                break

            time.sleep(0.15)

        except Exception as e:
            logger.error(f"Erro na paginação: {e}")
            break

    logger.info(f"Total de mercados: {len(all_markets)}")

    # Filtrar por esportes + liquidez
    sports_markets = []
    for m in all_markets:
        question = m.get("question", "").lower()
        liquidity = float(m.get("liquidityNum", 0))

        # Checar se é esporte
        is_sport = any(kw in question for kw in SPORTS_KEYWORDS)

        # Checar liquidez mínima
        has_liquidity = liquidity >= min_liquidity

        if is_sport and has_liquidity:
            sports_markets.append(m)

    logger.info(f"✅ Encontrados {len(sports_markets)} mercados de ESPORTES com liquidez >= ${min_liquidity:,.0f}")
    return sports_markets


def parse_market_info(m: dict) -> dict:
    """Extrai info básica do mercado."""
    try:
        question = m.get("question", "?")
        slug = m.get("slug", "")
        condition_id = m.get("conditionId", "")
        tokens = m.get("clobTokenIds", [])
        token_id = tokens[0] if tokens else None
        liquidity = float(m.get("liquidityNum", 0))
        volume = float(m.get("volumeNum", 0))

        # Parse prices
        prices = m.get("outcomePrices", [])
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except:
                prices = [0.5, 0.5]

        yes_price = float(prices[0]) if len(prices) > 0 else 0.5
        no_price = float(prices[1]) if len(prices) > 1 else 0.5

        # Detectar odds "altas" (claro favorito)
        confidence = abs(yes_price - 0.5)

        # Parse end date
        end_str = m.get("endDate", "")
        days_to_resolve = 30
        if end_str and "T" in end_str:
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                days_to_resolve = (end_dt - now).total_seconds() / 86400
            except:
                pass

        return {
            "question": question[:120],
            "slug": slug,
            "condition_id": condition_id,
            "token_id": token_id,
            "liquidity": liquidity,
            "volume": volume,
            "yes_price": yes_price,
            "no_price": no_price,
            "confidence": confidence,
            "days_to_resolve": days_to_resolve,
            "url": f"https://polymarket.com/{slug}" if slug else "",
        }
    except:
        return None


def display_top_opportunities(markets: List[dict], top_n: int = 15):
    """Exibe as melhores oportunidades por odds."""
    print()
    print("=" * 130)
    print(f"  TOP {top_n} MERCADOS DE ESPORTES (MELHOR ODDS)")
    print("=" * 130)

    # Ordenar por confiança (odds mais claras)
    sorted_markets = sorted(markets, key=lambda m: -m["confidence"])

    for i, m in enumerate(sorted_markets[:top_n], 1):
        conf_pct = m["confidence"] * 100
        conf_emoji = "🟢" if conf_pct >= 30 else "🟡" if conf_pct >= 15 else "🔴"

        print()
        print(f"  {i:>2}. {conf_emoji} {conf_pct:>5.1f}% confiança | ${m['liquidity']:>10,.0f} liq | {m['days_to_resolve']:>5.1f}d")
        print(f"      📌 {m['question']}")
        print(f"      Odds: YES {m['yes_price']:.3f} | NO {m['no_price']:.3f}")
        print(f"      Vol: ${m['volume']:,.0f}")
        print(f"      🔗 {m['url']}")

    print()
    print("=" * 130)
    print()


def save_market_list(markets: List[dict]):
    """Salva lista de mercados para análise."""
    filename = f"results/sports_markets_{int(time.time())}.json"
    with open(filename, "w") as f:
        json.dump(markets, f, indent=2)
    logger.info(f"Mercados salvos em: {filename}")
    print(f"  💾 Salvo: {filename}")
    return filename


def main():
    parser = argparse.ArgumentParser(
        description="Paper Trading de Esportes — Scan amplo + simulação",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  # Scan amplo (até $500 de liquidez) + FVG Multi-TF
  python deploy/sports_paper_trading.py --strategy fvg_multitf --min-liquidity 500

  # Mais seletivo ($1k mínimo) + MACD
  python deploy/sports_paper_trading.py --strategy macd --min-liquidity 1000

  # Teste ensemble (combina 3+ estratégias)
  python deploy/sports_paper_trading.py --strategy ensemble --min-liquidity 750

  # RSI em mercados relaxados
  python deploy/sports_paper_trading.py --strategy rsi --min-liquidity 500 --top 25
        """,
    )

    parser.add_argument(
        "--strategy",
        default="fvg_multitf",
        choices=["fvg_multitf", "macd", "rsi", "cvd", "ensemble"],
        help="Estratégia a usar (padrão: fvg_multitf)",
    )
    parser.add_argument(
        "--min-liquidity",
        type=float,
        default=500.0,
        help="Liquidez mínima em USDC (padrão: $500)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="Mostrar top N (padrão: 15)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Não salvar mercados em arquivo",
    )
    parser.add_argument(
        "--run-backtest",
        action="store_true",
        help="[TODO] Rodar backtest após scan",
    )

    args = parser.parse_args()

    print()
    print("=" * 130)
    print("  SPORTS PAPER TRADING — Scan Amplo + Simulação")
    print("=" * 130)

    # Step 1: Escanear
    print()
    markets_raw = fetch_all_sports_markets(min_liquidity=args.min_liquidity)

    if not markets_raw:
        print("  ❌ Nenhum mercado de esportes encontrado.")
        return

    # Step 2: Parse
    print(f"\n  🔍 Parseando {len(markets_raw)} mercados...")
    markets = [parse_market_info(m) for m in markets_raw]
    markets = [m for m in markets if m]

    logger.info(f"✅ Parseados {len(markets)} mercados")

    # Step 3: Display
    display_top_opportunities(markets, top_n=args.top)

    # Step 4: Save
    if not args.no_save:
        save_market_list(markets)

    # Step 5: Pronto para paper trading
    print()
    print("=" * 130)
    print("  ✅ SCAN COMPLETO!")
    print("=" * 130)
    print()
    print(f"  Encontrados: {len(markets)} mercados de esportes")
    print(f"  Estratégia: {args.strategy}")
    print()
    print("  Próximos passos:")
    print("    1. Escolha um condition_id da lista acima")
    print("    2. Execute o paper trading:")
    print()
    print(f"    python deploy/paper_trading.py --strategy {args.strategy} \\")
    print("           --market <condition_id> --days 30")
    print()
    print("  Ou para rodar multi-mercados (em desenvolvimento):")
    print(f"    python deploy/multi_market_backtest.py --strategy {args.strategy} --markets results/sports_markets_*.json")
    print()


if __name__ == "__main__":
    main()
