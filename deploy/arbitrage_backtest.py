"""
deploy/arbitrage_backtest.py — Backtest de arbitragem categórica.

Lógica CORRETA de arbitragem categórica:
    Em um evento com N outcomes mutuamente exclusivos (ex: "Quem ganha o Masters?"),
    a soma de TODOS os preços YES DEVE ser = 1.0.

    Se soma > 1.0 (overpriced):
        Ação: vender NO de todos os outcomes
        Quando o evento resolve, exatamente 1 YES = 1.0 e N-1 NOs = 1.0
        Receita garantida: (N-1) * $1
        Custo: sum(NO_prices) = N - sum(YES_prices)
        Lucro = (N-1) - (N - sum_YES) = sum_YES - 1.0

    Se soma < 1.0 (underpriced):
        Ação: comprar YES de todos os outcomes
        Custo: sum(YES_prices) = sum_YES
        Receita: exatamente 1 YES resolve a $1.0
        Lucro = 1.0 - sum_YES

    Em ambos os casos: lucro = |sum_YES - 1.0| por ciclo completo.

Backtest:
    Para cada evento, buscar histórico de preços de TODOS os outcomes (top 10).
    A cada candle, calcular sum(YES). Quando desvio > threshold, "entrar".
    Quando desvio converge, "sair". PnL = spread capturado.

Uso:
    python deploy/arbitrage_backtest.py
    python deploy/arbitrage_backtest.py --entry-threshold 100 --position-size 20
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Dict

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CLOB_HOST = "https://clob.polymarket.com"


@dataclass
class ArbTrade:
    """Representa um trade de arbitragem categórica."""
    event: str
    entry_time: datetime
    entry_sum: float          # Sum YES no momento de entrada
    entry_spread_bps: int     # Desvio de 1.0 em bps
    exit_time: Optional[datetime] = None
    exit_sum: Optional[float] = None
    exit_spread_bps: Optional[int] = None
    exit_reason: str = ""
    pnl: float = 0.0

    @property
    def won(self) -> bool:
        return self.pnl > 0


def fetch_price_history(token_id: str, fidelity: int = 60) -> Optional[pd.Series]:
    """Busca histórico de preços e retorna Series com close price por hora."""
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

        df = pd.DataFrame(history).copy()
        if "t" in df.columns:
            df = df.rename(columns={"t": "timestamp", "p": "price"})

        df = df.assign(
            timestamp=pd.to_datetime(df["timestamp"], unit="s", utc=True),
            price=df["price"].astype(float),
        )
        df = df.set_index("timestamp").sort_index()

        # Resample para candles de 1h (close)
        close = df["price"].resample("60min").last().dropna()
        return close

    except Exception:
        return None


def backtest_event(
    event_slug: str,
    top_markets: List[dict],
    position_size: float = 10.0,
    entry_threshold_bps: int = 100,
    exit_threshold_bps: int = 20,
    stop_loss_bps: int = 200,
    max_outcomes: int = 10,
) -> List[ArbTrade]:
    """
    Backtest de arbitragem categórica para um evento inteiro.

    Busca histórico de preço de cada outcome (top N),
    calcula sum(YES) a cada candle, e simula trades quando desvio > threshold.
    """
    trades = []

    # Filtrar outcomes com token válido
    valid_markets = [
        m for m in top_markets
        if m.get("token_yes")
    ][:max_outcomes]

    if len(valid_markets) < 3:
        return trades

    # Buscar histórico de cada outcome
    print(f"    Buscando {len(valid_markets)} outcomes...", end="", flush=True)
    price_series: Dict[str, pd.Series] = {}
    for m in valid_markets:
        token = m["token_yes"]
        series = fetch_price_history(token, fidelity=60)
        if series is not None and len(series) > 10:
            price_series[m["question"][:30]] = series
        time.sleep(0.15)  # Rate limit

    print(f" {len(price_series)} com dados")

    if len(price_series) < 3:
        return trades

    # Criar DataFrame com todos os outcomes sincronizados
    df_all = pd.DataFrame(price_series)

    # Usar apenas timestamps onde TODOS os outcomes têm preço
    df_all = df_all.dropna()

    if len(df_all) < 20:
        print(f"    ⚠️  Apenas {len(df_all)} candles sincronizados")
        return trades

    # Calcular sum(YES) a cada candle
    df_all = df_all.assign(sum_yes=df_all.sum(axis=1))
    df_all = df_all.assign(spread_bps=((df_all["sum_yes"] - 1.0) * 10000).astype(int))

    # ================================================================
    # Simular trades
    # ================================================================
    open_trade: Optional[dict] = None

    def calc_directional_pnl(entry_sum: float, current_sum: float, size: float) -> float:
        """
        PnL direcional: lucro depende do lado em que entramos.

        Se entramos OVERPRICED (entry_sum > 1.0):
            Posicionados para sum CAIR -> lucro = (entry_sum - current_sum) * size
        Se entramos UNDERPRICED (entry_sum < 1.0):
            Posicionados para sum SUBIR -> lucro = (current_sum - entry_sum) * size
        """
        if entry_sum > 1.0:
            return size * (entry_sum - current_sum)
        else:
            return size * (current_sum - entry_sum)

    for i in range(len(df_all)):
        row = df_all.iloc[i]
        timestamp = df_all.index[i]
        sum_yes = row["sum_yes"]
        spread_bps = int(row["spread_bps"])
        abs_spread = abs(spread_bps)

        # SAÍDA
        if open_trade:
            entry_sum = open_trade["entry_sum"]

            # PnL direcional corrente
            current_pnl = calc_directional_pnl(entry_sum, sum_yes, position_size)

            # Convergência: spread voltou para perto de 0 (take profit)
            if abs_spread <= exit_threshold_bps:
                trades.append(ArbTrade(
                    event=event_slug,
                    entry_time=open_trade["entry_time"],
                    entry_sum=entry_sum,
                    entry_spread_bps=open_trade["entry_spread_bps"],
                    exit_time=timestamp,
                    exit_sum=sum_yes,
                    exit_spread_bps=spread_bps,
                    exit_reason="converged",
                    pnl=current_pnl,
                ))
                open_trade = None
                continue

            # Stop-loss: PnL direcional ficou muito negativo
            # (spread se moveu contra nossa posição)
            stop_loss_usd = (stop_loss_bps / 10000.0) * position_size
            if current_pnl < -stop_loss_usd:
                trades.append(ArbTrade(
                    event=event_slug,
                    entry_time=open_trade["entry_time"],
                    entry_sum=entry_sum,
                    entry_spread_bps=open_trade["entry_spread_bps"],
                    exit_time=timestamp,
                    exit_sum=sum_yes,
                    exit_spread_bps=spread_bps,
                    exit_reason="stop_loss",
                    pnl=current_pnl,
                ))
                open_trade = None
                continue

            # Cruzamento: spread cruzou zero (pegamos ainda mais lucro)
            # Se entramos overpriced e agora estamos underpriced (ou vice-versa)
            # Consideramos saída favorável
            crossed_zero = (entry_sum > 1.0 and sum_yes < 1.0) or (entry_sum < 1.0 and sum_yes > 1.0)
            if crossed_zero and abs_spread >= entry_threshold_bps:
                # Cruzou e extremou - take profit agressivo
                trades.append(ArbTrade(
                    event=event_slug,
                    entry_time=open_trade["entry_time"],
                    entry_sum=entry_sum,
                    entry_spread_bps=open_trade["entry_spread_bps"],
                    exit_time=timestamp,
                    exit_sum=sum_yes,
                    exit_spread_bps=spread_bps,
                    exit_reason="crossed_extreme",
                    pnl=current_pnl,
                ))
                open_trade = None
                continue

        # ENTRADA
        if open_trade is None and abs_spread >= entry_threshold_bps:
            open_trade = {
                "entry_time": timestamp,
                "entry_sum": sum_yes,
                "entry_spread_bps": spread_bps,
            }

    # Fechar trade aberto no fim dos dados
    if open_trade:
        last_sum = df_all["sum_yes"].iloc[-1]
        final_pnl = calc_directional_pnl(
            open_trade["entry_sum"], last_sum, position_size
        )
        trades.append(ArbTrade(
            event=event_slug,
            entry_time=open_trade["entry_time"],
            entry_sum=open_trade["entry_sum"],
            entry_spread_bps=open_trade["entry_spread_bps"],
            exit_time=df_all.index[-1],
            exit_sum=last_sum,
            exit_spread_bps=int((last_sum - 1.0) * 10000),
            exit_reason="end_of_data",
            pnl=final_pnl,
        ))

    return trades


def main():
    parser = argparse.ArgumentParser(
        description="Backtest de arbitragem categórica"
    )
    parser.add_argument(
        "--scanner-result", type=str, default=None,
        help="Path ao scanner result JSON",
    )
    parser.add_argument(
        "--entry-threshold", type=int, default=100,
        help="Spread mínimo para entrar em bps (padrão: 100 = 1 por cento)",
    )
    parser.add_argument(
        "--exit-threshold", type=int, default=20,
        help="Spread para considerar convergido em bps (padrão: 20 = 0.2 por cento)",
    )
    parser.add_argument(
        "--position-size", type=float, default=10.0,
        help="Tamanho da posição em USDC (padrão: $10)",
    )
    parser.add_argument(
        "--max-outcomes", type=int, default=10,
        help="Máximo de outcomes a buscar por evento (padrão: 10)",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("  ARBITRAGEM CATEGÓRICA — BACKTEST")
    print("=" * 70)
    print(f"  Posição: ${args.position_size}")
    print(f"  Entrada: quando |spread| > {args.entry_threshold} bps")
    print(f"  Saída: quando |spread| < {args.exit_threshold} bps")
    print(f"  Max outcomes por evento: {args.max_outcomes}")
    print()

    # Procurar resultado do scanner
    if args.scanner_result is None:
        arbs_dir = "results"
        if not os.path.exists(arbs_dir):
            print(f"  ❌ Rode primeiro: python3 deploy/arbitrage_scanner.py")
            return

        arbs_files = [f for f in os.listdir(arbs_dir) if f.startswith("arbitrage_scan_")]
        if not arbs_files:
            print(f"  ❌ Nenhum resultado de scanner encontrado.")
            return

        latest_file = max(arbs_files)
        args.scanner_result = os.path.join(arbs_dir, latest_file)

    print(f"  Scanner: {args.scanner_result}")

    with open(args.scanner_result) as f:
        scanner_result = json.load(f)

    opportunities = scanner_result.get("opportunities", [])
    # Filtrar apenas eventos com spread razoável (< 5000 bps para excluir incompletos)
    opportunities = [o for o in opportunities if o.get("spread_bps", 0) <= 5000]
    print(f"  Eventos a testar: {len(opportunities)}")
    print()

    all_trades: List[ArbTrade] = []

    for i, arb in enumerate(opportunities, 1):
        event_slug = arb.get("event_slug", "?")[:40]
        spread_bps = arb.get("spread_bps", 0)
        num_mkts = arb.get("num_markets", 0)

        print(f"  [{i}/{len(opportunities)}] {event_slug} ({num_mkts} outcomes, {spread_bps} bps)")

        trades = backtest_event(
            event_slug=event_slug,
            top_markets=arb.get("top_markets", []),
            position_size=args.position_size,
            entry_threshold_bps=args.entry_threshold,
            exit_threshold_bps=args.exit_threshold,
            max_outcomes=args.max_outcomes,
        )

        all_trades.extend(trades)

        if trades:
            wins = sum(1 for t in trades if t.won)
            pnl = sum(t.pnl for t in trades)
            status = "✅" if pnl > 0 else "❌"
            print(f"    {status} {len(trades)} trades | {wins} won | ${pnl:+.2f}")
        else:
            print(f"    ➖ Nenhum trade")
        print()

    # ================================================================
    # Resultado agregado
    # ================================================================
    print("=" * 70)
    print("  RESULTADO AGREGADO")
    print("=" * 70)

    if not all_trades:
        print("  ❌ Nenhum trade simulado.")
        print("  Tente: --entry-threshold 50 (mais sensível)")
        return

    total = len(all_trades)
    winners = sum(1 for t in all_trades if t.won)
    total_pnl = sum(t.pnl for t in all_trades)
    win_rate = winners / total

    pnls_won = [t.pnl for t in all_trades if t.won]
    pnls_lost = [abs(t.pnl) for t in all_trades if not t.won]

    avg_win = sum(pnls_won) / len(pnls_won) if pnls_won else 0
    avg_loss = sum(pnls_lost) / len(pnls_lost) if pnls_lost else 0
    ev = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

    # Breakdown por exit_reason
    by_reason = {}
    for t in all_trades:
        r = t.exit_reason
        if r not in by_reason:
            by_reason[r] = {"count": 0, "pnl": 0, "wins": 0}
        by_reason[r]["count"] += 1
        by_reason[r]["pnl"] += t.pnl
        if t.won:
            by_reason[r]["wins"] += 1

    print(f"  Total de trades:    {total}")
    print(f"  Vencedores:         {winners} ({win_rate*100:.1f}%)")
    print(f"  PnL Total:          ${total_pnl:+.2f}")
    print(f"  EV por trade:       ${ev:+.3f}")
    print(f"  Média de ganho:     ${avg_win:.3f}")
    print(f"  Média de perda:     ${avg_loss:.3f}")
    print()
    print(f"  Por tipo de saída:")
    for reason, stats in by_reason.items():
        wr = stats["wins"] / stats["count"] if stats["count"] > 0 else 0
        print(f"    {reason:<15} | {stats['count']:>3} trades | {wr*100:>5.1f}% win | ${stats['pnl']:+.2f}")
    print()

    if ev > 0 and win_rate >= 0.55:
        print(f"  ✅ Edge positivo! EV ${ev:+.3f}/trade")
    elif ev > 0:
        print(f"  ⚠️  EV positivo (${ev:+.3f}) mas win rate baixo ({win_rate*100:.0f}%)")
    else:
        print(f"  ❌ Sem edge. Ajustar thresholds.")

    # Salvar
    os.makedirs("results", exist_ok=True)
    path = f"results/arbitrage_backtest_{int(time.time())}.json"
    with open(path, "w") as f:
        json.dump({
            "backtest_at": datetime.now(timezone.utc).isoformat(),
            "total_trades": total,
            "win_rate": round(win_rate, 3),
            "total_pnl": round(total_pnl, 2),
            "ev_per_trade": round(ev, 4),
            "by_reason": by_reason,
        }, f, indent=2)
    print(f"\n  Resultados salvos: {path}")


if __name__ == "__main__":
    main()
