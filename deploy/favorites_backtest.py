"""
deploy/favorites_backtest.py — Backtest da estratégia de favoritos.

Lógica diferente dos outros backtests:
    Não usa candles OHLCV com indicadores técnicos.
    Simula: "Se eu tivesse comprado quando o preço estava acima de X%
    com menos de Y horas para expirar, qual seria o resultado?"

Dados:
    Busca mercados já RESOLVIDOS na Gamma API e simula entradas
    nos momentos em que o preço atingiu o threshold antes da expiração.

Métricas reportadas:
    - Win rate (esperado: 90%+)
    - Profit por trade
    - EV por trade
    - Número de oportunidades encontradas

Uso:
    python deploy/favorites_backtest.py
    python deploy/favorites_backtest.py --threshold 0.90 --max-hours 48
    python deploy/favorites_backtest.py --threshold 0.95 --max-hours 24
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST  = "https://clob.polymarket.com"


@dataclass
class FavoritesTrade:
    question: str
    entry_price: float
    exit_price: float      # 1.0 se ganhou, 0.0 se perdeu, ou stop
    resolved_yes: bool     # True = mercado resolveu YES
    side: str              # 'YES' ou 'NO'
    hours_to_expiry: float
    won: bool
    pnl: float             # PnL em fração (ex: +0.05 = ganhou 5 centavos por dólar)


# ----------------------------------------------------------------
# Buscar mercados RESOLVIDOS
# ----------------------------------------------------------------

def fetch_resolved_markets(limit: int = 500) -> List[dict]:
    """Busca mercados já resolvidos para backtest (prioriza mercados recentes)."""
    # Busca mercados que fecharam recentemente (últimos 6 meses)
    # usando `closed: true` sem `resolved` porque esse param às vezes não funciona
    markets = []
    offset = 0

    while len(markets) < limit:
        try:
            resp = requests.get(
                f"{GAMMA_HOST}/markets",
                params={
                    "closed": "true",
                    "limit": 100,
                    "offset": offset,
                    "sortBy": "endDate",  # Ordena por data de encerramento
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
            print(f"  ❌ Erro: {e}")
            break

    return markets[:limit]


def fetch_price_history(token_id: str, fidelity: int = 60) -> Optional[pd.DataFrame]:
    """Busca histórico de preços para um token."""
    try:
        resp = requests.get(
            f"{CLOB_HOST}/prices-history",
            params={"market": token_id, "interval": "max", "fidelity": fidelity},
            timeout=10,
        )
        resp.raise_for_status()
        history = resp.json().get("history", [])
        if not history:
            return None

        df = pd.DataFrame(history)
        if "t" in df.columns:
            df = df.rename(columns={"t": "timestamp", "p": "price"})
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        df = df.set_index("timestamp").sort_index()
        df["price"] = df["price"].astype(float)
        return df

    except Exception:
        return None


def parse_end_date(market: dict) -> Optional[datetime]:
    for key in ("endDate", "end_date_iso", "endDateIso"):
        val = market.get(key)
        if val:
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(val, fmt)
                    return dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
    return None


def get_resolution(market: dict) -> Optional[bool]:
    """Retorna True se resolveu YES, False se resolveu NO, None se incerto."""
    outcome_prices = market.get("outcomePrices", [])
    if not outcome_prices:
        return None

    # A API retorna string JSON: '["1", "0"]'
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except Exception:
            return None

    try:
        prices = [float(p) for p in outcome_prices]
        if prices[0] >= 0.99:
            return True   # YES ganhou
        elif len(prices) > 1 and prices[1] >= 0.99:
            return False  # NO ganhou
        # Alguns mercados antigos chegam como ["0","0"] — fallback pelo histórico de preço
    except Exception:
        pass
    return None


# ----------------------------------------------------------------
# Simular trades
# ----------------------------------------------------------------

def simulate_favorites_trades(
    df_price: pd.DataFrame,
    end_date: datetime,
    resolved_yes: bool,
    question: str,
    threshold: float = 0.92,
    max_hours: float = 48.0,
    trade_size: float = 10.0,
    exit_stop: float = 0.85,
) -> List[FavoritesTrade]:
    """
    Simula entradas na estratégia de favoritos para um mercado resolvido.
    """
    trades = []
    cutoff = end_date - timedelta(hours=max_hours)

    # Filtrar apenas o período relevante (últimas max_hours antes da expiração)
    window = df_price[df_price.index >= cutoff].copy()
    if window.empty:
        return trades

    in_trade = False
    entry_price = 0.0
    side = "YES"  # Sempre compramos o lado favorito

    for ts, row in window.iterrows():
        price = row["price"]
        hours_left = (end_date - ts).total_seconds() / 3600

        if hours_left <= 0:
            break

        if not in_trade:
            # Verificar se pode entrar
            if price >= threshold:
                in_trade = True
                entry_price = price
                entry_hours = hours_left
                # Descobrir qual lado está favorito (YES ou NO)
                # Se price >= threshold, o YES está favorito
                # Se (1-price) >= threshold, NO está favorito (não capturamos aqui)
                side = "YES"

        else:
            # Verificar saída
            # Stop loss
            if price <= exit_stop:
                # Saiu com perda
                exit_price = exit_stop
                # Se compramos YES e resolveu YES: recebemos 1.0
                # Mas saímos antes no stop
                pnl_frac = exit_stop - entry_price
                dollar_pnl = pnl_frac * trade_size
                trades.append(FavoritesTrade(
                    question=question,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    resolved_yes=resolved_yes,
                    side=side,
                    hours_to_expiry=entry_hours,
                    won=False,
                    pnl=dollar_pnl,
                ))
                in_trade = False
                continue

            # Saída por lucro antecipado (próximo da resolução)
            if price >= 0.97:
                exit_price = price
                pnl_frac = exit_price - entry_price
                dollar_pnl = pnl_frac * trade_size
                trades.append(FavoritesTrade(
                    question=question,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    resolved_yes=resolved_yes,
                    side=side,
                    hours_to_expiry=entry_hours,
                    won=dollar_pnl > 0,
                    pnl=dollar_pnl,
                ))
                in_trade = False
                continue

    # Trade ainda aberto na resolução
    if in_trade:
        if resolved_yes and side == "YES":
            exit_price = 1.0
            won = True
        else:
            exit_price = 0.0
            won = False

        pnl_frac = exit_price - entry_price
        dollar_pnl = pnl_frac * trade_size
        trades.append(FavoritesTrade(
            question=question,
            entry_price=entry_price,
            exit_price=exit_price,
            resolved_yes=resolved_yes,
            side=side,
            hours_to_expiry=entry_hours,
            won=won,
            pnl=dollar_pnl,
        ))

    return trades


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Backtest da estratégia de comprar favoritos"
    )
    parser.add_argument("--threshold", type=float, default=0.92,
                        help="Probabilidade mínima de entrada (padrão: 0.92)")
    parser.add_argument("--max-hours", type=float, default=48.0,
                        help="Janela de entrada antes da expiração em horas (padrão: 48h)")
    parser.add_argument("--trade-size", type=float, default=10.0,
                        help="Tamanho do trade em USDC (padrão: $10)")
    parser.add_argument("--stop", type=float, default=0.85,
                        help="Stop loss (padrão: 0.85)")
    parser.add_argument("--markets", type=int, default=200,
                        help="Máximo de mercados resolvidos a analisar (padrão: 200)")
    args = parser.parse_args()

    print("=" * 62)
    print("  FAVORITOS — BACKTEST EM MERCADOS RESOLVIDOS")
    print("=" * 62)
    print(f"  Threshold:    {args.threshold*100:.0f}%")
    print(f"  Janela:       últimas {args.max_hours:.0f}h antes da expiração")
    print(f"  Stop loss:    {args.stop*100:.0f}%")
    print(f"  Tamanho:      ${args.trade_size:.0f} por trade")
    print()

    print(f"  Buscando mercados resolvidos...")
    markets = fetch_resolved_markets(limit=args.markets)
    print(f"  Mercados resolvidos encontrados: {len(markets)}")
    print()

    all_trades: List[FavoritesTrade] = []
    skip_reason = {"no_date": 0, "no_hist": 0, "no_token": 0, "unclear_res": 0, "other": 0}
    processed = 0

    for i, mkt in enumerate(markets):
        question = mkt.get("question", mkt.get("title", "?"))[:55]
        end_date = parse_end_date(mkt)

        if end_date is None:
            skip_reason["no_date"] += 1
            continue

        resolved_yes = get_resolution(mkt)

        # Buscar token YES (índice 0)
        clob_token_ids = mkt.get("clobTokenIds")
        token_id = None
        if clob_token_ids:
            try:
                ids = json.loads(clob_token_ids) if isinstance(clob_token_ids, str) else clob_token_ids
                token_id = ids[0] if ids else None
            except Exception:
                pass

        if not token_id:
            skip_reason["no_token"] += 1
            continue

        df = fetch_price_history(token_id, fidelity=60)  # Candles de 1h
        if df is None or df.empty:
            skip_reason["no_hist"] += 1
            continue

        # Fallback: se outcomePrices não indica resolução, usar último preço do histórico
        if resolved_yes is None:
            last_price = df["price"].iloc[-1]
            if last_price >= 0.99:
                resolved_yes = True
            elif last_price <= 0.01:
                resolved_yes = False
            else:
                skip_reason["unclear_res"] += 1
                continue

        trades = simulate_favorites_trades(
            df_price=df,
            end_date=end_date,
            resolved_yes=resolved_yes,
            question=question,
            threshold=args.threshold,
            max_hours=args.max_hours,
            trade_size=args.trade_size,
            exit_stop=args.stop,
        )

        all_trades.extend(trades)
        processed += 1

        if trades:
            wins = sum(1 for t in trades if t.won)
            pnl = sum(t.pnl for t in trades)
            status = "✅" if pnl > 0 else "❌"
            print(f"  {status} [{i+1:>3}/{len(markets)}] {question:<55} | {len(trades)} trades | {wins}/{len(trades)} win | ${pnl:+.2f}")

        # Rate limiting
        time.sleep(0.2)

    # ----------------------------------------------------------------
    # Resultado agregado
    # ----------------------------------------------------------------
    print()
    print("=" * 62)
    print("  RESULTADO AGREGADO")
    print("=" * 62)
    total_skipped = sum(skip_reason.values())
    print(f"  Mercados processados: {processed}")
    print(f"  Mercados pulados: {total_skipped}")
    if total_skipped > 0:
        print(f"    - Sem data válida: {skip_reason['no_date']}")
        print(f"    - Sem histórico de preço: {skip_reason['no_hist']}")
        print(f"    - Sem token ID: {skip_reason['no_token']}")
        print(f"    - Resolução incerta: {skip_reason['unclear_res']}")
    print()

    if not all_trades:
        print("  Nenhum trade simulado.")
        print("  Possíveis causas:")
        print("    - Threshold muito alto para o período analisado")
        print("    - Mercados sem histórico de preços disponível")
        print("    - Tente: --threshold 0.88 --max-hours 72")
        return

    total = len(all_trades)
    winners = sum(1 for t in all_trades if t.won)
    total_pnl = sum(t.pnl for t in all_trades)
    win_rate = winners / total

    wins_only  = [t.pnl for t in all_trades if t.won]
    losses_only = [abs(t.pnl) for t in all_trades if not t.won]

    avg_win  = sum(wins_only)  / max(len(wins_only), 1)
    avg_loss = sum(losses_only) / max(len(losses_only), 1)
    rr = avg_win / avg_loss if avg_loss > 0 else 99.0
    ev = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
    pf = (avg_win * winners) / max(avg_loss * (total - winners), 0.001)
    breakeven_wr = avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) > 0 else 0.5

    entry_prices = [t.entry_price for t in all_trades]
    avg_entry = sum(entry_prices) / len(entry_prices)

    print(f"  Total de trades:      {total}")
    print(f"  Vencedores:           {winners} ({win_rate*100:.1f}%)")
    print(f"  PnL Total:            ${total_pnl:+.2f}")
    print(f"  EV por trade:         ${ev:+.3f}")
    print(f"  Média de ganho:       ${avg_win:.3f}")
    print(f"  Média de perda:       ${avg_loss:.3f}")
    print(f"  Risk/Reward:          {rr:.2f}x")
    print(f"  Profit Factor:        {pf:.2f}")
    print(f"  Win rate p/ breakeven:{breakeven_wr*100:.1f}%")
    print(f"  Preço médio de entrada: {avg_entry:.3f} ({avg_entry*100:.1f}%)")
    print()

    if ev > 0 and win_rate >= 0.70:
        print(f"  ✅ Edge positivo confirmado!")
        print(f"     Win rate {win_rate*100:.1f}% (meta era 55%+)")
        print(f"     Em 100 trades → PnL esperado: ${ev*100:+.2f}")
    elif ev > 0:
        print(f"  ⚠️  EV positivo mas win rate baixo ({win_rate*100:.1f}%). Ajustar threshold.")
    else:
        print(f"  ❌ Sem edge. Tentar --threshold 0.95 --max-hours 24")

    # Salvar resultado
    os.makedirs("results", exist_ok=True)
    path = f"results/favorites_backtest_{int(time.time())}.json"
    with open(path, "w") as f:
        json.dump({
            "threshold": args.threshold,
            "max_hours": args.max_hours,
            "total_trades": total,
            "win_rate": round(win_rate, 3),
            "total_pnl": round(total_pnl, 2),
            "ev_per_trade": round(ev, 4),
            "rr": round(rr, 2),
            "profit_factor": round(pf, 2),
            "avg_entry_price": round(avg_entry, 4),
        }, f, indent=2)
    print(f"\n  Resultados salvos: {path}")


if __name__ == "__main__":
    main()
