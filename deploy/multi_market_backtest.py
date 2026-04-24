"""
deploy/multi_market_backtest.py — RSI em múltiplos mercados simultaneamente.

Busca dados de N mercados, roda RSI em cada um e agrega os resultados
para ter volume estatístico suficiente (objetivo: 50-100 trades total).

Mercados selecionados (preço 25-75%, volume razoável, ativos):
    1. Russia-Ukraine Ceasefire before GTA VI (53.5%)
    2. Rihanna Album before GTA VI (59.5%)
    3. Edmonton Oilers (41.5%) — já validado com RSI positivo
    4. Trump out as President before GTA VI (preço a verificar)

Uso:
    python deploy/multi_market_backtest.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.rsi_strategy import RSIStrategy
from bot.risk_manager import RiskManager
from backtesting.metrics import BacktestMetrics
from backtesting.engine import BacktestTrade, BacktestResult

logging.basicConfig(level=logging.WARNING)  # Silenciar logs para leitura limpa

CLOB_HOST = "https://clob.polymarket.com"

# Mercados com preço na faixa útil (25-75%)
MARKETS = [
    {
        "label": "Russia-Ukraine Ceasefire / GTA VI",
        "token": "8501497159083948713316135768103773293754490207922884688769443031624417212426",
        "expected_price": 0.535,
    },
    {
        "label": "Rihanna Album / GTA VI",
        "token": "98022490269692409998126496127597032490334070080325855126491859374983463996227",
        "expected_price": 0.595,
    },
    {
        "label": "Edmonton Oilers — Pacific Division",
        "token": "85687115062270092189696630379304469921570604205230345809153365454081081413248",
        "expected_price": 0.415,
    },
    {
        "label": "Trump out as President / GTA VI",
        "token": "108999723207897941876452935557011604067917389120996960199512481363958770540884",
        "expected_price": 0.465,
    },
]


# ----------------------------------------------------------------
# Fetch + OHLCV
# ----------------------------------------------------------------

def fetch_ohlcv(token_id: str, label: str, fidelity: int = 15) -> Optional[pd.DataFrame]:
    """Busca dados históricos e converte para OHLCV."""
    try:
        resp = requests.get(
            f"{CLOB_HOST}/prices-history",
            params={"market": token_id, "interval": "max", "fidelity": fidelity},
            timeout=10,
        )
        resp.raise_for_status()
        history = resp.json().get("history", [])
        if not history:
            print(f"  ⚠️  Sem dados: {label}")
            return None

        df = pd.DataFrame(history)                          # colunas: t, p
        df = df.rename(columns={"t": "timestamp", "p": "price"})
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
        df = df.set_index("timestamp").sort_index()
        df["price"] = df["price"].astype(float)

        ohlcv = df["price"].resample(f"{fidelity}min").agg(
            open="first", high="max", low="min", close="last"
        ).dropna()
        ohlcv["volume"] = (ohlcv["high"] - ohlcv["low"]) * 1000
        return ohlcv.reset_index()

    except Exception as e:
        print(f"  ❌ Erro em {label}: {e}")
        return None


# ----------------------------------------------------------------
# Backtest simples inline (sem paper_trading.py completo)
# ----------------------------------------------------------------

@dataclass
class TradeResult:
    market: str
    signal: str
    entry_price: float
    exit_price: float
    pnl: float
    won: bool


def run_rsi_backtest(
    df: pd.DataFrame,
    label: str,
    initial_capital: float = 1000.0,
    trade_size: float = 10.0,
    stop_loss_pct: float = 0.10,
) -> List[TradeResult]:
    """Executa backtest do RSI num DataFrame OHLCV."""
    strategy = RSIStrategy()
    results: List[TradeResult] = []

    df = df.copy()
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    df.columns = [c.lower() for c in df.columns]

    min_c = strategy.min_candles
    open_trade: Optional[dict] = None

    for i in range(min_c, len(df)):
        window = df.iloc[:i+1]
        signal = strategy.generate_signal(window)

        candle = df.iloc[i]

        # Verificar stop-loss ou saída de trade aberto
        if open_trade:
            stop = open_trade["entry"] * (1 - stop_loss_pct)
            hit_stop = candle["low"] <= stop
            # Sair no próximo sinal contrário ou stop
            exit_now = hit_stop or (
                signal.signal_type.value != open_trade["side"]
                and signal.signal_type.value != "HOLD"
            )
            if exit_now:
                exit_price = stop if hit_stop else float(candle["close"])
                pnl = (exit_price - open_trade["entry"]) / open_trade["entry"]
                if open_trade["side"] == "SELL":
                    pnl = -pnl
                dollar_pnl = pnl * trade_size

                results.append(TradeResult(
                    market=label,
                    signal=open_trade["side"],
                    entry_price=open_trade["entry"],
                    exit_price=exit_price,
                    pnl=dollar_pnl,
                    won=dollar_pnl > 0,
                ))
                open_trade = None

        # Abrir novo trade se sem posição aberta
        if open_trade is None and signal.signal_type.value in ("BUY", "SELL"):
            open_trade = {
                "side": signal.signal_type.value,
                "entry": float(candle["close"]),
            }

    return results


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------

def main():
    print("=" * 60)
    print("  RSI MULTI-MERCADO — BACKTEST AGREGADO")
    print("=" * 60)

    all_trades: List[TradeResult] = []
    market_summaries = []

    for mkt in MARKETS:
        label = mkt["label"]
        print(f"\n📊 {label}")
        print(f"   Buscando dados...")

        df = fetch_ohlcv(mkt["token"], label)
        if df is None:
            continue

        price_range = f"{df['close'].min():.3f} → {df['close'].max():.3f}"
        candle_count = len(df)
        print(f"   Candles: {candle_count} | Preço: {price_range}")

        # Verificar se preço está na faixa útil
        mid_price = df["close"].mean()
        if mid_price < 0.05 or mid_price > 0.95:
            print(f"   ⚠️  Preço médio {mid_price:.3f} — fora da faixa útil (5-95%). Pulando.")
            continue

        trades = run_rsi_backtest(df, label)
        all_trades.extend(trades)

        if trades:
            won = sum(1 for t in trades if t.won)
            total_pnl = sum(t.pnl for t in trades)
            avg_win = sum(t.pnl for t in trades if t.won) / max(won, 1)
            avg_loss = abs(sum(t.pnl for t in trades if not t.won)) / max(len(trades) - won, 1)
            rr = avg_win / avg_loss if avg_loss > 0 else 0
            print(f"   Trades: {len(trades)} | Win: {won/len(trades)*100:.0f}% | PnL: ${total_pnl:+.2f} | R/R: {rr:.2f}")
            market_summaries.append({
                "market": label,
                "trades": len(trades),
                "win_rate": won/len(trades),
                "pnl": total_pnl,
                "rr": rr,
            })
        else:
            print(f"   Sem trades gerados.")

    # ----------------------------------------------------------------
    # Resultado agregado
    # ----------------------------------------------------------------
    print("\n" + "=" * 60)
    print("  RESULTADO AGREGADO")
    print("=" * 60)

    if not all_trades:
        print("Nenhum trade gerado em nenhum mercado.")
        return

    total = len(all_trades)
    winners = sum(1 for t in all_trades if t.won)
    total_pnl = sum(t.pnl for t in all_trades)
    win_rate = winners / total

    avg_win = sum(t.pnl for t in all_trades if t.won) / max(winners, 1)
    avg_loss = abs(sum(t.pnl for t in all_trades if not t.won)) / max(total - winners, 1)
    rr = avg_win / avg_loss if avg_loss > 0 else 0
    profit_factor = (avg_win * winners) / max(avg_loss * (total - winners), 0.001)
    ev_per_trade = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
    breakeven_winrate = 1 / (1 + rr) if rr > 0 else 0.5

    print(f"  Total de trades:    {total} ({len(market_summaries)} mercados)")
    print(f"  Vencedores:         {winners} ({win_rate*100:.1f}%)")
    print(f"  PnL Total:          ${total_pnl:+.2f}")
    print(f"  Média de ganho:     ${avg_win:.3f}")
    print(f"  Média de perda:     ${avg_loss:.3f}")
    print(f"  Risk/Reward:        {rr:.2f}")
    print(f"  Profit Factor:      {profit_factor:.2f}")
    print(f"  EV por trade:       ${ev_per_trade:+.3f}")
    print(f"  Win rate mínima:    {breakeven_winrate*100:.1f}% (para breakeven)")
    print()

    if ev_per_trade > 0:
        print("  ✅ Edge positivo detectado.")
        print(f"     Em 100 trades → PnL esperado: ${ev_per_trade * 100:+.2f}")
    else:
        print("  ❌ Sem edge. Ajustar parâmetros antes de avançar.")

    print()
    print("  Por mercado:")
    for s in market_summaries:
        status = "✅" if s["pnl"] > 0 else "❌"
        print(f"  {status} {s['market'][:45]:<45} | {s['trades']:>2} trades | {s['win_rate']*100:.0f}% win | ${s['pnl']:+.2f}")

    # Salvar JSON
    os.makedirs("results", exist_ok=True)
    out = {
        "total_trades": total,
        "win_rate": round(win_rate, 3),
        "total_pnl": round(total_pnl, 2),
        "ev_per_trade": round(ev_per_trade, 4),
        "rr": round(rr, 2),
        "profit_factor": round(profit_factor, 2),
        "markets": market_summaries,
    }
    path = f"results/multi_market_rsi_{int(time.time())}.json"
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n  Resultados salvos: {path}")


if __name__ == "__main__":
    main()
