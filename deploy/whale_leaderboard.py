"""
deploy/whale_leaderboard.py — Rastreador de baleias do Polymarket (dados reais).

Usa dados REAIS do leaderboard do Polymarket:
    1. Scrape da página de leaderboard para obter top traders + endereços
    2. Consulta data-api.polymarket.com/positions para posições ativas de cada whale
    3. Cruza posições entre whales para encontrar consenso
    4. Sugere trades baseados no que os melhores estão fazendo

APIs descobertas:
    - Leaderboard: https://polymarket.com/leaderboard (scrape HTML)
    - Posições:    https://data-api.polymarket.com/positions?user={address}
    - Perfil:      https://polymarket.com/profile/{address}

Uso:
    python deploy/whale_leaderboard.py
    python deploy/whale_leaderboard.py --top 10 --min-position 1000
    python deploy/whale_leaderboard.py --focus-markets "Masters,NBA,FIFA"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ================================================================
# Top traders do leaderboard (atualizado 2026-04-04)
# Endereços reais extraídos de polymarket.com/leaderboard
# ================================================================

KNOWN_WHALES = [
    {
        "rank": 1,
        "name": "0x4924...",
        "address": "0x492442eab586f242b53bda933fd5de859c8a3782",
        "profit": 6_365_300,
        "volume": 11_141_964,
    },
    {
        "rank": 2,
        "name": "HorizonSplendidView",
        "address": "0x02227b8f5a9636e895607edd3185ed6ee5598ff7",
        "profit": 4_016_108,
    },
    {
        "rank": 3,
        "name": "reachingthesky",
        "address": "0xefbc5fec8d7b0acdc8911bdd9a98d6964308f9a2",
        "profit": 3_742_635,
    },
    {
        "rank": 4,
        "name": "beachboy4",
        "address": "0xc2e7800b5af46e6093872b177b7a5e7f0563be51",
        "profit": 3_450_654,
        "volume": 2_343_420,
    },
    {
        "rank": 5,
        "name": "majorexploiter",
        "address": "0x019782cab5d844f02bafb71f512758be78579f3c",
        "profit": 2_416_975,
    },
    {
        "rank": 6,
        "name": "bcda",
        "address": "0xb45a797faa52b0fd8adc56d30382022b7b12192c",
        "profit": 2_092_462,
        "volume": 5_248_939,
    },
    {
        "rank": 7,
        "name": "Countryside",
        "address": "0xbddf61af533ff524d27154e589d2d7a81510c684",
        "profit": 1_883_409,
        "volume": 4_824_082,
    },
    {
        "rank": 8,
        "name": "0x2a2C...",
        "address": "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1",
        "profit": 1_792_049,
        "volume": 13_597_769,
    },
    {
        "rank": 9,
        "name": "sovereign2013",
        "address": "0xee613b3fc183ee44f9da9c05f53e2da107e3debf",
        "profit": 1_720_594,
        "volume": 13_521_544,
    },
    {
        "rank": 10,
        "name": "RN1",
        "address": "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",
        "profit": 1_584_760,
        "volume": 12_567_743,
    },
    {
        "rank": 11,
        "name": "432614799197",
        "address": "0xdc876e6873772d38716fda7f2452a78d426d7ab6",
        "profit": 1_495_977,
    },
    {
        "rank": 12,
        "name": "lo34567Taipe",
        "address": "0xf195721ad850377c96cd634457c70cd9e8308057",
        "profit": 1_459_488,
    },
    {
        "rank": 13,
        "name": "gatorr",
        "address": "0x93abbc022ce98d6f45d4444b594791cc4b7a9723",
        "profit": 1_420_982,
        "volume": 1_602_762,
    },
    {
        "rank": 14,
        "name": "Blessed-Sunshine",
        "address": "0x59a0744db1f39ff3afccd175f80e6e8dfc239a09",
        "profit": 1_202_927,
    },
    {
        "rank": 15,
        "name": "Anointed-Connect",
        "address": "0x8f037a2e4fd49d11267f4ab874ab7ba745ac64d6",
        "profit": 1_202_670,
        "volume": 575,
    },
    {
        "rank": 16,
        "name": "swisstony",
        "address": "0x204f72f35326db932158cba6adff0b9a1da95e14",
        "profit": 1_019_149,
        "volume": 7_715_476,
    },
    {
        "rank": 17,
        "name": "JPMorgan101",
        "address": "0xb6d6e99d3bfe055874a04279f659f009fd57be17",
        "profit": 887_475,
    },
    {
        "rank": 18,
        "name": "ewelmealt",
        "address": "0x07921379f7b31ef93da634b688b2fe36897db778",
        "profit": 879_966,
        "volume": 133_904,
    },
    {
        "rank": 19,
        "name": "SecondWindCapital",
        "address": "0x8c80d213c0cbad777d06ee3f58f6ca4bc03102c3",
        "profit": 777_028,
        "volume": 350_681,
    },
    {
        "rank": 20,
        "name": "GamblingIsAllYouNeed",
        "address": "0x507e52ef684ca2dd91f90a9d26d149dd3288beae",
        "profit": 756_766,
        "volume": 7_563_382,
    },
]

DATA_API = "https://data-api.polymarket.com"


# ================================================================
# Data structures
# ================================================================

@dataclass
class WhalePosition:
    """Uma posição ativa de uma whale."""
    whale_name: str
    whale_rank: int
    title: str
    slug: str
    size: float           # Quantidade de shares
    avg_price: float      # Preço médio de entrada
    current_value: float  # Valor atual em USD
    cash_pnl: float       # P&L realizado
    percent_pnl: float    # % de P&L
    condition_id: str = ""


@dataclass
class ConsensusPosition:
    """Posição que múltiplas whales compartilham."""
    title: str
    slug: str
    whales: List[str]         # Nomes das whales
    total_value: float        # Soma de currentValue
    total_size: float         # Soma de shares
    avg_entry: float          # Preço médio ponderado
    avg_pnl_pct: float        # % PnL médio
    whale_count: int          # Quantas whales
    best_whale_rank: int      # Rank da melhor whale
    positions: List[WhalePosition] = field(default_factory=list)


# ================================================================
# API calls
# ================================================================

def fetch_whale_positions(
    address: str,
    size_threshold: float = 0,
) -> List[dict]:
    """Busca posições ativas de uma whale via data-api."""
    try:
        params = {"user": address}
        if size_threshold > 0:
            params["sizeThreshold"] = size_threshold

        resp = requests.get(
            f"{DATA_API}/positions",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        positions = resp.json()

        if not isinstance(positions, list):
            return []

        return positions

    except Exception as e:
        print(f"    ❌ Erro: {e}")
        return []


def filter_active_positions(
    positions: List[dict],
    min_value: float = 100.0,
) -> List[dict]:
    """Filtra apenas posições ativas (currentValue > 0)."""
    active = []
    for p in positions:
        current_val = p.get("currentValue", 0)
        if isinstance(current_val, (int, float)) and current_val >= min_value:
            active.append(p)
    return active


# ================================================================
# Core logic
# ================================================================

def scan_all_whales(
    whales: List[dict],
    min_position_value: float = 500.0,
    max_whales: int = 20,
    delay: float = 0.5,
) -> Dict[str, List[WhalePosition]]:
    """
    Escaneia posições ativas de todas as whales.

    Returns: {slug: [WhalePosition, ...]}
    """
    all_positions: Dict[str, List[WhalePosition]] = defaultdict(list)

    for i, whale in enumerate(whales[:max_whales], 1):
        name = whale["name"]
        address = whale["address"]
        rank = whale["rank"]

        print(f"  [{i}/{min(len(whales), max_whales)}] #{rank} {name:<25}", end="", flush=True)

        raw_positions = fetch_whale_positions(address)

        if not raw_positions:
            print(" 0 posicoes")
            time.sleep(delay)
            continue

        active = filter_active_positions(raw_positions, min_value=min_position_value)
        total_value = sum(p.get("currentValue", 0) for p in active)

        print(f" {len(active):>3} ativas (${total_value:,.0f})")

        for pos in active:
            slug = pos.get("slug", "unknown")
            wp = WhalePosition(
                whale_name=name,
                whale_rank=rank,
                title=pos.get("title", slug)[:80],
                slug=slug,
                size=pos.get("size", 0),
                avg_price=pos.get("avgPrice", 0),
                current_value=pos.get("currentValue", 0),
                cash_pnl=pos.get("cashPnl", 0),
                percent_pnl=pos.get("percentPnl", 0),
                condition_id=pos.get("conditionId", ""),
            )
            all_positions[slug].append(wp)

        time.sleep(delay)

    return all_positions


def find_consensus(
    all_positions: Dict[str, List[WhalePosition]],
    min_whales: int = 2,
) -> List[ConsensusPosition]:
    """
    Encontra mercados onde 2+ whales estão posicionadas.
    Ordena por número de whales e valor total.
    """
    consensus = []

    for slug, positions in all_positions.items():
        if len(positions) < min_whales:
            continue

        whale_names = [p.whale_name for p in positions]
        total_value = sum(p.current_value for p in positions)
        total_size = sum(p.size for p in positions)

        # Preço médio ponderado por size
        weighted_price = sum(p.avg_price * p.size for p in positions)
        avg_entry = weighted_price / total_size if total_size > 0 else 0

        # PnL médio
        pnls = [p.percent_pnl for p in positions if p.percent_pnl != 0]
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0

        best_rank = min(p.whale_rank for p in positions)

        consensus.append(ConsensusPosition(
            title=positions[0].title,
            slug=slug,
            whales=whale_names,
            total_value=total_value,
            total_size=total_size,
            avg_entry=round(avg_entry, 4),
            avg_pnl_pct=round(avg_pnl, 1),
            whale_count=len(positions),
            best_whale_rank=best_rank,
            positions=positions,
        ))

    # Ordenar: mais whales primeiro, depois por valor total
    consensus.sort(key=lambda c: (-c.whale_count, -c.total_value))
    return consensus


def categorize_markets(consensus: List[ConsensusPosition]) -> Dict[str, List[ConsensusPosition]]:
    """Categoriza os mercados por tipo (sports, politics, crypto, etc)."""
    categories = defaultdict(list)

    for c in consensus:
        title_lower = c.title.lower()
        slug_lower = c.slug.lower()

        if any(k in title_lower or k in slug_lower for k in [
            "nba", "nfl", "mlb", "nhl", "soccer", "football", "tennis",
            "win on 20", "spread:", "o/u", "masters", "champions",
            "league", "fc ", "vs.", "game", "serie a", "la liga",
            "lol:", "esport",
        ]):
            categories["sports"].append(c)
        elif any(k in title_lower or k in slug_lower for k in [
            "president", "election", "trump", "biden", "vance",
            "governor", "senate", "congress", "political", "vote",
        ]):
            categories["politics"].append(c)
        elif any(k in title_lower or k in slug_lower for k in [
            "bitcoin", "ethereum", "crypto", "btc", "eth", "price",
            "fed", "interest rate", "gdp", "inflation",
        ]):
            categories["crypto_finance"].append(c)
        else:
            categories["other"].append(c)

    return categories


# ================================================================
# Display
# ================================================================

def display_consensus(consensus: List[ConsensusPosition], top_n: int = 30):
    """Exibe resultados de consenso formatados."""
    print()
    print("=" * 75)
    print("  CONSENSO DE WHALES — Mercados onde multiplas baleias apostam")
    print("=" * 75)
    print()

    if not consensus:
        print("  Nenhum consenso encontrado entre as whales analisadas.")
        return

    # Separar por categoria
    categories = categorize_markets(consensus)

    for cat_name, positions in sorted(categories.items()):
        cat_display = {
            "sports": "ESPORTES",
            "politics": "POLITICA",
            "crypto_finance": "CRYPTO / FINANCAS",
            "other": "OUTROS",
        }.get(cat_name, cat_name.upper())

        print(f"  --- {cat_display} ({len(positions)} mercados) ---")
        print()

        for c in positions[:top_n]:
            whale_icons = "🐋" * min(c.whale_count, 5)
            pnl_icon = "🟢" if c.avg_pnl_pct > 0 else "🔴" if c.avg_pnl_pct < 0 else "⚪"

            print(f"  {whale_icons} {c.title}")
            print(f"      Whales: {c.whale_count} | "
                  f"Valor total: ${c.total_value:,.0f} | "
                  f"Preco medio: {c.avg_entry:.3f} | "
                  f"PnL medio: {pnl_icon} {c.avg_pnl_pct:+.1f}%")
            print(f"      Melhor rank: #{c.best_whale_rank} | "
                  f"Quem: {', '.join(c.whales[:5])}")
            print()

    # Top 10 geral (maiores consensos)
    print()
    print("=" * 75)
    print("  TOP 10 MAIORES CONSENSOS (todas as categorias)")
    print("=" * 75)
    print()

    for i, c in enumerate(consensus[:10], 1):
        pnl_icon = "🟢" if c.avg_pnl_pct > 0 else "🔴"
        print(f"  {i:>2}. [{c.whale_count} whales] {c.title}")
        print(f"      ${c.total_value:,.0f} investido | "
              f"entrada avg {c.avg_entry:.3f} | "
              f"{pnl_icon} {c.avg_pnl_pct:+.1f}% PnL")
        print(f"      Whales: {', '.join(c.whales[:5])}")
        if len(c.whales) > 5:
            print(f"              + {len(c.whales) - 5} mais")
        print()


def display_recommendations(consensus: List[ConsensusPosition]):
    """Gera recomendações acionáveis baseadas no consenso."""
    print()
    print("=" * 75)
    print("  RECOMENDACOES PARA COPIAR")
    print("=" * 75)
    print()

    # Filtrar: 3+ whales, PnL positivo, valor significativo
    strong = [
        c for c in consensus
        if c.whale_count >= 3 and c.avg_pnl_pct > 0 and c.total_value > 5000
    ]

    if not strong:
        # Relaxar para 2+ whales
        strong = [
            c for c in consensus
            if c.whale_count >= 2 and c.avg_pnl_pct > 0 and c.total_value > 2000
        ]

    if not strong:
        print("  Nenhuma recomendacao forte no momento.")
        print("  Criterio: 2+ whales com PnL positivo e valor > $2,000")
        return

    print(f"  Encontradas {len(strong)} oportunidades fortes:")
    print()

    for i, c in enumerate(strong[:10], 1):
        confidence = "ALTA" if c.whale_count >= 4 else "MEDIA" if c.whale_count >= 3 else "MODERADA"
        emoji = "🟢" if confidence == "ALTA" else "🟡" if confidence == "MEDIA" else "🟠"

        print(f"  {emoji} {i}. {c.title}")
        print(f"      Confianca: {confidence} ({c.whale_count} whales concordam)")
        print(f"      Preco medio de entrada: {c.avg_entry:.3f}")
        print(f"      PnL atual das whales: {c.avg_pnl_pct:+.1f}%")
        print(f"      Valor total apostado: ${c.total_value:,.0f}")
        print(f"      Sugestao: Comprar a ~{c.avg_entry:.2f} ou melhor")
        print()

    print("  AVISO: Copiar whales NAO garante lucro. Elas podem sair")
    print("  a qualquer momento. Use stop-loss e nunca invista mais")
    print("  do que pode perder.")


# ================================================================
# Main
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Rastreador de baleias do Polymarket (dados reais)"
    )
    parser.add_argument(
        "--top", type=int, default=15,
        help="Quantas whales do leaderboard analisar (padrao: 15)",
    )
    parser.add_argument(
        "--min-position", type=float, default=500,
        help="Valor minimo de posicao em USD para considerar (padrao: $500)",
    )
    parser.add_argument(
        "--min-whales", type=int, default=2,
        help="Minimo de whales para consenso (padrao: 2)",
    )
    parser.add_argument(
        "--save", action="store_true",
        help="Salvar resultado em JSON",
    )
    args = parser.parse_args()

    print("=" * 75)
    print("  POLYMARKET — WHALE LEADERBOARD TRACKER")
    print("  Dados reais via data-api.polymarket.com")
    print("=" * 75)
    print(f"  Analisando top {args.top} traders do leaderboard")
    print(f"  Posicao minima: ${args.min_position:,.0f}")
    print(f"  Consenso minimo: {args.min_whales} whales")
    print()

    # Escanear posições
    print("  Buscando posicoes ativas de cada whale...")
    print()
    all_positions = scan_all_whales(
        KNOWN_WHALES,
        min_position_value=args.min_position,
        max_whales=args.top,
        delay=0.5,
    )

    total_positions = sum(len(v) for v in all_positions.values())
    unique_markets = len(all_positions)
    print()
    print(f"  Total: {total_positions} posicoes em {unique_markets} mercados unicos")

    # Encontrar consenso
    consensus = find_consensus(all_positions, min_whales=args.min_whales)
    print(f"  Consenso: {len(consensus)} mercados com {args.min_whales}+ whales")

    # Exibir resultados
    display_consensus(consensus)
    display_recommendations(consensus)

    # Salvar
    if args.save or consensus:
        os.makedirs("results", exist_ok=True)
        path = f"results/whale_leaderboard_{int(time.time())}.json"

        # Serializar para JSON (sem dataclass)
        consensus_data = []
        for c in consensus:
            consensus_data.append({
                "title": c.title,
                "slug": c.slug,
                "whale_count": c.whale_count,
                "whales": c.whales,
                "total_value": round(c.total_value, 2),
                "total_size": round(c.total_size, 2),
                "avg_entry": c.avg_entry,
                "avg_pnl_pct": c.avg_pnl_pct,
                "best_whale_rank": c.best_whale_rank,
            })

        with open(path, "w") as f:
            json.dump({
                "scanned_at": datetime.now(timezone.utc).isoformat(),
                "whales_analyzed": min(args.top, len(KNOWN_WHALES)),
                "total_positions": total_positions,
                "unique_markets": unique_markets,
                "consensus_count": len(consensus),
                "consensus": consensus_data,
            }, f, indent=2)
        print(f"\n  Resultados salvos: {path}")

    print()
    print("  Proximo passo:")
    print("    1. Analise as recomendacoes acima")
    print("    2. Verifique no polymarket.com os mercados sugeridos")
    print("    3. Entre com posicoes pequenas ($5-10) seguindo o consenso")
    print("    4. Use stop-loss de 10-15%")
    print()


if __name__ == "__main__":
    main()
