"""
deploy/favorites_scanner.py — Scanner de mercados favoritos no Polymarket.

Busca mercados ativos onde a probabilidade está acima de 92% e o tempo
até a expiração é curto o suficiente para capturar o prêmio residual.

Lógica:
    1. Busca mercados ativos via Gamma API
    2. Filtra por: probabilidade YES ou NO >= threshold
    3. Filtra por: dias até expiração dentro da janela configurada
    4. Exibe oportunidades ordenadas por atratividade
    5. Salva JSON com oportunidades para usar no paper/live trading

Uso:
    python deploy/favorites_scanner.py
    python deploy/favorites_scanner.py --threshold 0.90 --max-days 72
    python deploy/favorites_scanner.py --threshold 0.95 --max-days 24
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import requests

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST  = "https://clob.polymarket.com"


# ----------------------------------------------------------------
# Buscar mercados ativos via Gamma API
# ----------------------------------------------------------------

def fetch_active_markets(limit: int = 500) -> List[dict]:
    """Busca mercados ativos da Gamma API."""
    markets = []
    offset = 0

    while True:
        try:
            resp = requests.get(
                f"{GAMMA_HOST}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": min(limit, 100),
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

            if len(batch) < 100 or len(markets) >= limit:
                break

            time.sleep(0.3)  # Respeitar rate limit

        except Exception as e:
            print(f"  ❌ Erro ao buscar mercados: {e}")
            break

    return markets


def parse_end_date(end_date_str: Optional[str]) -> Optional[datetime]:
    """Converte string de data da API para datetime."""
    if not end_date_str:
        return None
    try:
        # Formato: "2025-06-01T00:00:00Z" ou "2025-06-01"
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(end_date_str, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    except Exception:
        pass
    return None


def extract_probability(market: dict) -> tuple[float, str]:
    """
    Extrai a maior probabilidade do mercado (YES ou NO).
    Retorna (probabilidade, lado) onde lado é 'YES' ou 'NO'.
    """
    outcome_prices = market.get("outcomePrices", [])
    outcomes = market.get("outcomes", ["YES", "NO"])

    if not outcome_prices:
        return 0.0, "?"

    # A API retorna uma string JSON: '["0.535", "0.465"]'
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except Exception:
            return 0.0, "?"

    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = ["YES", "NO"]

    try:
        prices = [float(p) for p in outcome_prices]
    except (TypeError, ValueError):
        return 0.0, "?"

    if not prices:
        return 0.0, "?"

    max_price = max(prices)
    max_idx = prices.index(max_price)
    side = outcomes[max_idx] if max_idx < len(outcomes) else f"Outcome {max_idx}"

    return max_price, side


# ----------------------------------------------------------------
# Scanner principal
# ----------------------------------------------------------------

def scan_favorites(
    threshold: float = 0.92,
    max_days: float = 48,
    min_days: float = 0.5,
    limit: int = 500,
) -> List[dict]:
    """
    Escaneia mercados do Polymarket buscando favoritos.

    Args:
        threshold: Probabilidade mínima para considerar favorito (padrão: 0.92)
        max_days:  Máximo de horas até expiração (padrão: 48h)
        min_days:  Mínimo de horas até expiração (evitar mercados já expirando)
        limit:     Máximo de mercados a buscar

    Returns:
        Lista de oportunidades ordenadas por score de atratividade.
    """
    now = datetime.now(timezone.utc)
    max_delta = timedelta(hours=max_days)
    min_delta = timedelta(hours=min_days)

    print(f"\n  Buscando mercados ativos (limite: {limit})...")
    markets = fetch_active_markets(limit=limit)
    print(f"  Total de mercados encontrados: {len(markets)}")

    opportunities = []

    for mkt in markets:
        # Filtrar por probabilidade
        prob, side = extract_probability(mkt)
        if prob < threshold:
            continue

        # Filtrar por data de expiração
        end_date = parse_end_date(mkt.get("endDate") or mkt.get("end_date_iso"))
        if end_date is None:
            continue

        delta = end_date - now
        if delta < min_delta or delta > max_delta:
            continue

        # Calcular métricas de atratividade
        hours_left = delta.total_seconds() / 3600
        residual_premium = 1.0 - prob     # Quanto sobra para ganhar
        expected_gain_pct = residual_premium / prob * 100  # Retorno % sobre o capital

        # Score: premia probabilidade alta + tempo curto
        # Quanto mais certo e mais próximo do vencimento, melhor
        prob_score = (prob - threshold) / (1.0 - threshold)  # 0 a 1
        time_score = 1.0 - (hours_left / max_days)           # 0 a 1 (mais alto = mais próximo)
        score = prob_score * 0.6 + time_score * 0.4

        token_id = None
        clob_token_ids = mkt.get("clobTokenIds")
        if clob_token_ids:
            try:
                ids = json.loads(clob_token_ids) if isinstance(clob_token_ids, str) else clob_token_ids
                # Side YES = índice 0, NO = índice 1
                side_idx = 0 if side == "YES" else 1
                token_id = ids[side_idx] if side_idx < len(ids) else ids[0]
            except Exception:
                pass

        opportunities.append({
            "question": mkt.get("question", mkt.get("title", "?")),
            "side": side,
            "probability": round(prob, 4),
            "residual_premium": round(residual_premium, 4),
            "expected_gain_pct": round(expected_gain_pct, 2),
            "hours_left": round(hours_left, 1),
            "end_date": end_date.strftime("%Y-%m-%d %H:%M UTC"),
            "score": round(score, 4),
            "token_id": token_id,
            "condition_id": mkt.get("conditionId"),
            "market_slug": mkt.get("slug"),
        })

    # Ordenar por score descendente
    opportunities.sort(key=lambda x: x["score"], reverse=True)
    return opportunities


# ----------------------------------------------------------------
# Exibir resultados
# ----------------------------------------------------------------

def display_opportunities(opps: List[dict], threshold: float, max_days: float):
    """Exibe oportunidades formatadas no terminal."""
    print(f"\n{'='*65}")
    print(f"  FAVORITOS — MERCADOS COM PROBABILIDADE ≥ {threshold*100:.0f}%")
    print(f"  Janela: próximas {max_days:.0f} horas")
    print(f"{'='*65}")

    if not opps:
        print(f"\n  Nenhum mercado encontrado com os critérios atuais.")
        print(f"  Tente aumentar --max-days ou diminuir --threshold.")
        return

    print(f"\n  {len(opps)} oportunidade(s) encontrada(s):\n")

    for i, opp in enumerate(opps, 1):
        prob_bar = "█" * int(opp["probability"] * 20)
        print(f"  {i:>2}. {opp['question'][:58]}")
        print(f"      Lado:  {opp['side']:<6} | Prob: {opp['probability']:.1%} {prob_bar}")
        print(f"      Tempo: {opp['hours_left']:.1f}h restantes | Expira: {opp['end_date']}")
        print(f"      Prêmio residual: {opp['residual_premium']:.3f} ({opp['expected_gain_pct']:.1f}% de retorno)")
        if opp["token_id"]:
            print(f"      Token: {opp['token_id'][:30]}...")
        print(f"      Score: {opp['score']:.3f}")
        print()


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scanner de favoritos no Polymarket"
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.92,
        help="Probabilidade mínima (padrão: 0.92 = 92 por cento)",
    )
    parser.add_argument(
        "--max-days",
        type=float,
        default=48.0,
        help="Horas máximas até expiração (padrão: 48h)",
    )
    parser.add_argument(
        "--min-days",
        type=float,
        default=0.5,
        help="Horas mínimas até expiração (padrão: 0.5h)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Máximo de mercados a escanear (padrão: 500)",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Salvar oportunidades em JSON",
    )
    args = parser.parse_args()

    print("=" * 65)
    print("  POLYMARKET — SCANNER DE FAVORITOS")
    print("=" * 65)
    print(f"  Threshold:  {args.threshold*100:.0f}%")
    print(f"  Janela:     até {args.max_days:.0f}h antes da expiração")

    opps = scan_favorites(
        threshold=args.threshold,
        max_days=args.max_days,
        min_days=args.min_days,
        limit=args.limit,
    )

    display_opportunities(opps, args.threshold, args.max_days)

    # Salvar JSON
    if args.save or opps:
        os.makedirs("results", exist_ok=True)
        path = f"results/favorites_scan_{int(time.time())}.json"
        with open(path, "w") as f:
            json.dump(
                {
                    "scanned_at": datetime.now(timezone.utc).isoformat(),
                    "threshold": args.threshold,
                    "max_hours": args.max_days,
                    "total_found": len(opps),
                    "opportunities": opps,
                },
                f,
                indent=2,
            )
        if opps:
            print(f"  Resultados salvos: {path}")

    # Interpretação
    if opps:
        avg_prob = sum(o["probability"] for o in opps) / len(opps)
        avg_premium = sum(o["residual_premium"] for o in opps) / len(opps)
        print(f"  Probabilidade média: {avg_prob:.1%}")
        print(f"  Prêmio médio disponível: {avg_premium:.3f} ({avg_premium/(1-avg_premium)*100:.1f}% de retorno)")
        print()
        print("  Próximo passo:")
        print("    python3 deploy/favorites_backtest.py  ← valida o histórico")
        print("    python3 deploy/main.py --mode paper --strategy favorites --capital 1000")


if __name__ == "__main__":
    main()
