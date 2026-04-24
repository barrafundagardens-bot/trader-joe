"""
deploy/exit_handler.py — Gerencia saida/resolucao de posicoes abertas.

Responsabilidades:
    1. Verificar se mercados das posicoes abertas ja resolveram
    2. Calcular PnL real (win/loss)
    3. Atualizar state.json (marcar positions como closed)
    4. Liberar capital de volta (retornar ao cash disponivel)
    5. Opcional: Detectar se whales sairam do mercado (exit signal)

Uso:
    python deploy/exit_handler.py                     # Verifica estado padrao
    python deploy/exit_handler.py --state data/whale_trader_state.json
    python deploy/exit_handler.py --force-close       # Fecha tudo forcado

Logica:
    - Para cada posicao 'open' no state:
        * Busca resolucao via Gamma API
        * Se closed=True: calcula PnL real, marca como 'closed'
        * Se ainda aberto: mantem
    - Atualiza capital: retorna valor final das posicoes fechadas
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests

GAMMA_API = "https://gamma-api.polymarket.com"
DEFAULT_STATE = "data/whale_trader_state.json"


def fetch_market_status(slug: str, retries: int = 3) -> Optional[dict]:
    """Busca status de um mercado pelo slug com retry logic."""
    for attempt in range(retries):
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={"slug": slug},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list) and data:
                return data[0]
            elif isinstance(data, dict):
                return data
            return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)  # Aguarda 1s antes de retry
                continue
            print(f"  ⚠️  ERRO ao buscar {slug} (após {retries} tentativas): {e}")
            return None


def parse_prices(market: dict) -> tuple:
    """Extrai (yes_price, no_price, closed, freeze_confidence) de um mercado."""
    prices = market.get("outcomePrices", [])
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except:
            return None, None, False, 0

    if not isinstance(prices, list) or len(prices) < 2:
        return None, None, False, 0

    try:
        yes_price = float(prices[0])
        no_price = float(prices[1])
        closed = market.get("closed", False)

        # Detecção de freeze: se YES ou NO está congelado em 0.0 ou 1.0
        freeze_confidence = 0
        if yes_price in (0.0, 1.0) or no_price in (0.0, 1.0):
            freeze_confidence = 85  # 85% confiança que resolveu

        return yes_price, no_price, closed, freeze_confidence
    except:
        return None, None, False, 0


def resolve_position(position: dict) -> dict:
    """
    Resolve uma posicao, calculando PnL baseado no preco final.

    Returns:
        dict com fields atualizados:
            - status: 'open' | 'closed' | 'unresolved'
            - exit_price: preco final do outcome
            - pnl: lucro/prejuizo em USD
            - final_value: quanto a posicao vale agora
    """
    slug = position.get("slug", "")
    entry_price = position.get("entry_price", 0.5)
    size_usd = position.get("size_usd", 0)
    side = position.get("side", "BUY_YES")  # Default: BUY_YES (retrocompat)
    outcome_side = position.get("outcome_side", "")  # YES, NO, ou categorico (nome)

    # Normalizar side
    if side == "BUY":
        side = "BUY_YES"  # Retrocompatibilidade

    market = fetch_market_status(slug)
    if not market:
        return {
            **position,
            "status": "unresolved",
            "resolution_note": "Mercado nao encontrado",
        }

    yes_price, no_price, closed, freeze_confidence = parse_prices(market)

    if yes_price is None:
        return {
            **position,
            "status": "unresolved",
            "resolution_note": "Precos nao disponiveis",
        }

    # Detectar resolução: closed=True OU congelado (freeze_confidence > 0)
    is_likely_resolved = closed or freeze_confidence > 0

    # Deteccao extra: posicao com mais de 24h E preco extremo (>0.90 ou <0.10)
    # Polymarket demora até 24h para marcar como closed após o evento
    if not is_likely_resolved:
        entry_time_str = position.get("entry_time", "")
        if entry_time_str:
            try:
                entry_dt = datetime.fromisoformat(entry_time_str)
                age_hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
                # Se tem mais de 20h E preco está muito extremo (> 90% ou < 10%)
                if age_hours > 20 and (yes_price >= 0.90 or yes_price <= 0.10 or
                                        no_price >= 0.90 or no_price <= 0.10):
                    is_likely_resolved = True
                    freeze_confidence = 70  # Confiança moderada
            except Exception:
                pass

    if not is_likely_resolved:
        return {
            **position,
            "status": "open",
            "current_yes_price": round(yes_price, 4),
            "current_no_price": round(no_price, 4),
            "resolution_note": "Ainda aberto",
        }

    # Mercado provavelmente resolveu! Calcular final_value
    # Para YES/NO simples
    if side in ("BUY_YES", "BUY_NO"):
        shares = size_usd / entry_price
        if side == "BUY_YES":
            final_value = shares * yes_price
            exit_price = yes_price
        else:  # BUY_NO
            final_value = shares * no_price
            exit_price = no_price
    else:
        # Side é categorico (ex: "BUY_MARIA TIMOFEEVA")
        # Trata como generico: se outcome_side tem valor, usa
        outcomes = market.get("outcomes", [])
        prices = market.get("outcomePrices", [])

        if isinstance(prices, str):
            prices = json.loads(prices)
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        # Encontrar indice do outcome que comprou
        outcome_idx = -1
        if outcome_side:
            try:
                outcome_idx = outcomes.index(outcome_side)
            except ValueError:
                # Case insensitive
                for i, o in enumerate(outcomes):
                    if str(o).strip().upper() == str(outcome_side).strip().upper():
                        outcome_idx = i
                        break

        if outcome_idx < 0 or outcome_idx >= len(prices):
            # Nao conseguiu identificar - assume perda total
            final_value = 0.0
            exit_price = 0.0
        else:
            shares = size_usd / entry_price
            exit_price = float(prices[outcome_idx])
            final_value = shares * exit_price

    pnl = final_value - size_usd
    pnl_pct = (pnl / size_usd) * 100 if size_usd > 0 else 0

    return {
        **position,
        "status": "closed",
        "exit_price": round(exit_price, 4),
        "final_value": round(final_value, 3),
        "pnl": round(pnl, 3),
        "pnl_pct": round(pnl_pct, 1),
        "resolution_note": "Mercado resolvido",
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }


def process_state(state_path: str, dry_run: bool = False) -> dict:
    """
    Processa state.json:
        1. Verifica resolucao de cada posicao aberta
        2. Atualiza posicoes fechadas
        3. Retorna capital ao cash

    Returns: state atualizado
    """
    if not os.path.exists(state_path):
        print(f"  ❌ Estado nao encontrado: {state_path}")
        return {}

    with open(state_path) as f:
        state = json.load(f)

    positions = state.get("positions", [])
    capital = state.get("capital", 0)

    print()
    print("=" * 75)
    print(f"  EXIT HANDLER — Processando {state_path}")
    print("=" * 75)
    print(f"  Capital atual: ${capital:.2f}")
    print(f"  Posicoes totais: {len(positions)}")
    print()

    open_positions = [p for p in positions if p.get("status") == "open"]
    print(f"  Verificando {len(open_positions)} posicoes abertas...")
    print()

    updated_positions = []
    total_returned = 0.0
    total_pnl = 0.0
    wins = 0
    losses = 0

    for pos in positions:
        if pos.get("status") != "open":
            updated_positions.append(pos)
            continue

        title = pos.get("title", "?")[:55]
        slug = pos.get("slug", "?")

        print(f"  [{slug[:45]}]")
        print(f"    {title}")

        resolved = resolve_position(pos)
        status = resolved.get("status")

        if status == "closed":
            pnl = resolved.get("pnl", 0)
            pnl_pct = resolved.get("pnl_pct", 0)
            final_val = resolved.get("final_value", 0)
            exit_price = resolved.get("exit_price", 0)
            entry_price = resolved.get("entry_price", 0)
            closed_at = resolved.get("closed_at", "?")

            icon = "✅ WIN" if pnl > 0 else "❌ LOSS"
            days_open = "?"
            try:
                from datetime import datetime
                entry_time = datetime.fromisoformat(position.get("entry_time", ""))
                closed_time = datetime.fromisoformat(closed_at)
                days_open = (closed_time - entry_time).days
            except:
                pass

            print(f"    {icon} | entrada ${entry_price:.3f} → saida ${exit_price:.3f}")
            print(f"    Retorno: ${final_val:.3f} | PnL: ${pnl:+.3f} ({pnl_pct:+.1f}%) | Dias aberto: {days_open}")

            total_returned += final_val
            total_pnl += pnl
            if pnl > 0:
                wins += 1
            else:
                losses += 1

        elif status == "open":
            yes_p = resolved.get("current_yes_price", 0)
            no_p = resolved.get("current_no_price", 0)
            print(f"    ⏳ AINDA ABERTO | YES ${yes_p:.3f} | NO ${no_p:.3f}")

        else:
            note = resolved.get('resolution_note', 'desconhecido')
            print(f"    ⚠️  {note}")

        print()
        updated_positions.append(resolved)
        time.sleep(0.2)

    # Atualizar capital
    new_capital = capital + total_returned

    print("=" * 75)
    print("  RESUMO DA RESOLUCAO")
    print("=" * 75)
    print(f"  Posicoes fechadas: {wins + losses}")
    print(f"  Wins:              {wins} ✅")
    print(f"  Losses:            {losses} ❌")
    if wins + losses > 0:
        win_rate = (wins / (wins + losses)) * 100
        print(f"  Win rate:          {win_rate:.1f}%")
    print(f"  Total retornado:   ${total_returned:.2f}")
    print(f"  PnL total:         ${total_pnl:+.2f}")
    print(f"  Capital antes:     ${capital:.2f}")
    print(f"  Capital depois:    ${new_capital:.2f}")
    print("=" * 75)
    print()

    # Atualizar state
    state["positions"] = updated_positions
    state["capital"] = round(new_capital, 2)
    state["last_exit_check"] = datetime.now(timezone.utc).isoformat()

    # Salvar
    if not dry_run:
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)
        print(f"  ✅ Estado salvo em: {state_path}")
    else:
        print("  [DRY RUN] Estado NAO foi salvo")

    return state


def main():
    parser = argparse.ArgumentParser(
        description="Exit Handler — Resolve posicoes fechadas e libera capital"
    )
    parser.add_argument(
        "--state", default=DEFAULT_STATE,
        help=f"Caminho do state.json (padrao: {DEFAULT_STATE})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Nao salva mudancas (apenas mostra o que seria feito)",
    )

    args = parser.parse_args()
    process_state(args.state, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
