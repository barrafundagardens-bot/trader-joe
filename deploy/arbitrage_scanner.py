"""
deploy/arbitrage_scanner.py — Scanner de oportunidades de arbitragem no Polymarket.

Lógica:
    1. Buscar todos os mercados ativos
    2. Agrupar por conditionId (mercados logicamente relacionados)
    3. Dentro de cada grupo, encontrar outcomes complementares
    4. Calcular se há misprice: sum(prices) != 1.0
    5. Rankear por spread size e liquidity

Exemplo de grupo:
    conditionId = "0x123abc..."
    Market A: "Trump wins 2024?" → YES @ 0.40, NO @ 0.60
    Market B: "Trump loses 2024?" → YES @ 0.65, NO @ 0.35

    Relação detectada: A.YES ≈ B.NO (mesmo evento)

    Arbitrage:
        Sum = 0.40 + 0.65 = 1.05 (overpriced)
        SHORT A.NO @ 0.60 + LONG B.NO @ 0.35
        Locked profit = 0.60 - 0.35 = 0.25 per $1

Uso:
    python deploy/arbitrage_scanner.py
    python deploy/arbitrage_scanner.py --min-spread 50 --markets 500
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import requests

GAMMA_HOST = "https://gamma-api.polymarket.com"


# ================================================================
# Data Fetching
# ================================================================

def fetch_active_markets(limit: int = 500) -> List[dict]:
    """Busca mercados ativos da Gamma API."""
    markets = []
    offset = 0

    print("  Buscando mercados...")
    while len(markets) < limit:
        try:
            resp = requests.get(
                f"{GAMMA_HOST}/markets",
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

            time.sleep(0.3)

        except Exception as e:
            print(f"  ❌ Erro ao buscar: {e}")
            break

    return markets[:limit]


def parse_prices(prices_raw) -> list:
    """Parse outcomePrices que pode ser string JSON ou array."""
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
    """Parse outcomes que pode ser string JSON ou array."""
    if not outcomes_raw:
        return []

    if isinstance(outcomes_raw, str):
        try:
            return json.loads(outcomes_raw)
        except Exception:
            return []

    return outcomes_raw or []


def parse_token_ids(token_ids_raw) -> list:
    """Parse clobTokenIds."""
    if not token_ids_raw:
        return []

    if isinstance(token_ids_raw, str):
        try:
            return json.loads(token_ids_raw)
        except Exception:
            return []

    return token_ids_raw or []


# ================================================================
# Arbitrage Detection
# ================================================================

def group_markets_by_event(markets: List[dict]) -> dict:
    """
    Agrupa mercados pelo event.id (eventos compartilham o mesmo evento lógico).

    No Polymarket, conditionId é único por mercado.
    Mercados relacionados (como "Will X win the Masters?" para cada jogador)
    compartilham o mesmo event.id.

    Returns: {event_id: {"slug": str, "markets": [market1, market2, ...]}}
    """
    groups = defaultdict(lambda: {"slug": "", "markets": []})

    for market in markets:
        events = market.get("events", [])
        for ev in events:
            eid = ev.get("id")
            if eid:
                groups[eid]["slug"] = ev.get("slug", "?")
                groups[eid]["markets"].append(market)

    return groups


def find_categorical_arbitrage(event_id: str, event_slug: str, markets: List[dict]) -> Optional[dict]:
    """
    Detecta arbitragem categórica: todos os YES de um evento devem somar 1.0.

    Exemplo real (Masters 2026):
        Scottie Scheffler YES @ 0.130
        Rory McIlroy YES @ 0.065
        Bryson Dechambeau YES @ 0.085
        ... (59 jogadores)
        SOMA = 1.057 → overpriced by 5.7% (569 bps)

    Arbitrage se overpriced (soma > 1.0):
        Vender NO de todos os outcomes = aposta de que ALGUÉM vai ganhar
        Custo = sum(NO_prices) = sum(1 - YES_price) = N - sum(YES)
        Se N outcomes e sum(YES) = 1.057:
            custo dos NOs = 59 - 1.057 = 57.943
            Garantido: 58 NOs resolvem a 1.0, 1 NO resolve a 0.0
            Retorno = 58 * $1 = $58 (já que 58 dos 59 NOs serão corretos)
            Lucro = $58 - $57.943 = $0.057 por $57.943 investido
        Isso é complexo demais para posições individuais.

    Abordagem simplificada:
        Se soma > 1.0: os preços estão inflados → pode-se vender o conjunto
        Se soma < 1.0: os preços estão deflacionados → pode-se comprar o conjunto
        O spread é a diferença para 1.0

    Returns: dict com oportunidade ou None
    """
    # Coletar preço YES de cada mercado no evento
    market_data = []
    for m in markets:
        prices = parse_prices(m.get("outcomePrices", []))
        outcomes = parse_outcomes(m.get("outcomes", []))
        tokens = parse_token_ids(m.get("clobTokenIds", []))

        if not prices or not outcomes or not tokens:
            continue

        # Pegar preço YES (índice 0 se outcomes tem "Yes"/"YES")
        yes_idx = None
        for idx, out in enumerate(outcomes):
            if out.upper() in ("YES", "SIM"):
                yes_idx = idx
                break

        if yes_idx is None or yes_idx >= len(prices):
            continue

        yes_price = prices[yes_idx]
        no_price = prices[1 - yes_idx] if len(prices) > 1 else (1.0 - yes_price)

        market_data.append({
            "market": m.get("slug", "?"),
            "question": m.get("question", m.get("title", "?"))[:80],
            "yes_price": yes_price,
            "no_price": no_price,
            "token_yes": tokens[yes_idx] if yes_idx < len(tokens) else None,
            "token_no": tokens[1 - yes_idx] if len(tokens) > 1 else None,
            "liquidity": m.get("liquidityNum", 0),
            "volume": m.get("volumeNum", 0),
        })

    if len(market_data) < 2:
        return None

    # Calcular soma dos YES
    sum_yes = sum(md["yes_price"] for md in market_data)
    entry_spread = sum_yes - 1.0
    spread_bps = int(abs(entry_spread) * 10000)

    if spread_bps < 30:  # Spread muito pequeno (< 0.3%)
        return None

    # Ordenar por preço YES (maiores primeiro — mais impactantes)
    market_data.sort(key=lambda x: x["yes_price"], reverse=True)

    # Determinar tipo de arbitragem
    if sum_yes > 1.0:
        arb_type = "overpriced"
        action = "SELL NO de todos (ou BUY YES dos menos prováveis)"
    else:
        arb_type = "underpriced"
        action = "BUY YES de todos (ou SELL NO dos mais prováveis)"

    # Calcular lucro potencial por $100 investido
    if sum_yes > 1.0:
        # Se compramos todos os NOs: custo = sum(no_prices), retorno = (N-1) * 1.0
        total_no_cost = sum(md["no_price"] for md in market_data)
        guaranteed_return = (len(market_data) - 1) * 1.0
        profit_per_dollar = (guaranteed_return - total_no_cost) / total_no_cost if total_no_cost > 0 else 0
    else:
        # Se compramos todos os YES: custo = sum(yes_prices), retorno = 1.0 (1 vai ganhar)
        total_yes_cost = sum_yes
        guaranteed_return = 1.0
        profit_per_dollar = (guaranteed_return - total_yes_cost) / total_yes_cost if total_yes_cost > 0 else 0

    return {
        "event_id": event_id,
        "event_slug": event_slug,
        "arb_type": arb_type,
        "num_markets": len(market_data),
        "sum_yes": round(sum_yes, 4),
        "entry_spread": round(entry_spread, 4),
        "spread_bps": spread_bps,
        "profit_per_dollar": round(profit_per_dollar, 4),
        "action": action,
        "top_markets": market_data[:10],  # Top 10 por preço
        "total_liquidity": sum(md["liquidity"] for md in market_data),
        "total_volume": sum(md["volume"] for md in market_data),
    }


# ================================================================
# Main
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Scanner de arbitragem no Polymarket"
    )
    parser.add_argument(
        "--min-spread",
        type=int,
        default=30,
        help="Spread mínimo em basis points (padrão: 30 = 0.3 por cento)",
    )
    parser.add_argument(
        "--markets",
        type=int,
        default=500,
        help="Máximo de mercados a escanear (padrão: 500)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Mostrar top N oportunidades (padrão: 20)",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Salvar resultado em JSON",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("  POLYMARKET — SCANNER DE ARBITRAGEM COMBINATÓRIA")
    print("=" * 70)
    print(f"  Spread mínimo: {args.min_spread} bps ({args.min_spread / 100:.1f}%)")
    print(f"  Limite de mercados: {args.markets}")
    print()

    # Buscar mercados
    markets = fetch_active_markets(limit=args.markets)
    print(f"  Mercados encontrados: {len(markets)}")
    print()

    # Agrupar por event.id (mercados relacionados compartilham o mesmo evento)
    print("  Agrupando por event.id...")
    event_groups = group_markets_by_event(markets)
    groups_with_multiple = {
        eid: info for eid, info in event_groups.items()
        if len(info["markets"]) >= 3  # Pelo menos 3 outcomes para ser categórico
    }
    print(f"  Eventos com 3+ mercados: {len(groups_with_multiple)}")
    print()

    # Procurar arbitragens categóricas
    print("  Procurando arbitragens categóricas...")
    all_arbs = []
    for eid, info in groups_with_multiple.items():
        arb = find_categorical_arbitrage(eid, info["slug"], info["markets"])
        if arb:
            all_arbs.append(arb)

    # Filtrar por spread mínimo
    filtered = [a for a in all_arbs if a["spread_bps"] >= args.min_spread]
    filtered.sort(key=lambda x: x["spread_bps"], reverse=True)

    print()
    print("=" * 70)
    print(f"  ARBITRAGENS ENCONTRADAS ({len(filtered)} oportunidades)")
    print("=" * 70)
    print()

    if not filtered:
        print("  ❌ Nenhuma arbitragem encontrada.")
        print(f"  Tente reduzir --min-spread ou aumentar --markets")
        return

    for i, arb in enumerate(filtered[: args.top], 1):
        sum_y = arb["sum_yes"]
        spread_bps = arb["spread_bps"]
        arb_type = arb["arb_type"]
        profit_pd = arb["profit_per_dollar"]
        top = arb["top_markets"]

        icon = "📈" if arb_type == "underpriced" else "📉"
        print(f"  {i:>2}. {icon} {arb['event_slug']:<40}")
        print(f"      Tipo: {arb_type} | Mercados: {arb['num_markets']} | "
              f"Soma YES: {sum_y:.4f} | Spread: {spread_bps} bps")
        print(f"      Lucro/dolar: {profit_pd*100:.2f}% | "
              f"Liq: ${arb['total_liquidity']:,.0f} | Vol: ${arb['total_volume']:,.0f}")
        print(f"      Acao: {arb['action']}")
        print(f"      Top outcomes:")
        for md in top[:5]:
            print(f"        {md['yes_price']:.3f} YES | {md['question'][:55]}")
        print()

    # Estatísticas
    avg_spread_bps = sum(a["spread_bps"] for a in filtered) / len(filtered)
    sorted_spreads = sorted([a["spread_bps"] for a in filtered])
    median_spread_bps = sorted_spreads[len(sorted_spreads) // 2]

    print()
    print("  ESTATÍSTICAS:")
    print(f"    Total de oportunidades: {len(filtered)}")
    print(f"    Spread médio: {avg_spread_bps:.0f} bps ({avg_spread_bps / 100:.2f}%)")
    print(f"    Spread mediano: {median_spread_bps:.0f} bps ({median_spread_bps / 100:.2f}%)")
    print(f"    Maior spread: {max(a['spread_bps'] for a in filtered)} bps")
    print()

    # Salvar
    if args.save or filtered:
        os.makedirs("results", exist_ok=True)
        path = f"results/arbitrage_scan_{int(time.time())}.json"
        with open(path, "w") as f:
            json.dump({
                "scanned_at": datetime.now(timezone.utc).isoformat(),
                "criteria": {
                    "min_spread_bps": args.min_spread,
                    "markets_scanned": len(markets),
                },
                "total_opportunities": len(filtered),
                "opportunities": filtered[:args.top],
                "statistics": {
                    "avg_spread_bps": round(avg_spread_bps, 1),
                    "median_spread_bps": median_spread_bps,
                    "max_spread_bps": max(a["spread_bps"] for a in filtered) if filtered else 0,
                },
            }, f, indent=2)
        print(f"  Resultados salvos: {path}")

    print()
    print("  Próximo passo:")
    print("    python3 deploy/arbitrage_backtest.py")
    print("    (valida historicamente e simula trades)")


if __name__ == "__main__":
    main()
