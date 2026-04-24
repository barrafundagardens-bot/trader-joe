"""
deploy/fetch_real_data.py — Busca dados históricos reais do Polymarket.

Usa a API pública do CLOB (sem autenticação) para baixar o histórico
de preços de um mercado específico e salvar como OHLCV em CSV.

Uso:
    python deploy/fetch_real_data.py
    python deploy/fetch_real_data.py --token SEU_TOKEN_ID --interval 1w

API utilizada (pública, sem chave):
    GET https://clob.polymarket.com/prices-history
        ?market={token_id}
        &interval={max|1d|1w|1m}
        &fidelity={minutos por candle}
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import pandas as pd
import requests

# ----------------------------------------------------------------
# Market IDs do projeto polymarket-rbi-bot (BTC up/down)
# ----------------------------------------------------------------
DEFAULT_TOKEN_ID = (
    "383975077506218930573468880033441136112987238933685677349709401910643842844855"
)

CLOB_HOST = "https://clob.polymarket.com"


def fetch_price_history(
    token_id: str,
    interval: str = "max",
    fidelity: int = 15,
) -> list[dict]:
    """
    Busca histórico de preços via API pública do Polymarket CLOB.

    Args:
        token_id:  ID do token (YES ou NO outcome do mercado).
        interval:  Janela de tempo: 'max', '1m', '1w', '1d', '6h', '1h'.
        fidelity:  Tamanho do candle em minutos (15 = candles de 15min).

    Returns:
        Lista de dicts com {'t': timestamp_unix, 'p': price}.
    """
    url = f"{CLOB_HOST}/prices-history"
    params = {
        "market": token_id,
        "interval": interval,
        "fidelity": fidelity,
    }

    print(f"Buscando dados: {url}")
    print(f"  Token:    {token_id[:20]}...")
    print(f"  Interval: {interval} | Fidelity: {fidelity}min")

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        history = data.get("history", [])
        if not history:
            print("⚠️  API retornou lista vazia. Mercado pode ter expirado.")
            return []

        print(f"  Pontos recebidos: {len(history)}")
        return history

    except requests.RequestException as e:
        print(f"❌ Erro ao buscar dados: {e}")
        return []


def build_ohlcv(history: list[dict], fidelity_min: int = 15) -> pd.DataFrame:
    """
    Converte lista de preços tick-level em candles OHLCV.

    Como a API retorna apenas preço (sem volume), o volume é estimado
    como a variação absoluta de preço × 1000 (proxy razoável).

    Args:
        history:      Lista de {'t': unix_ts, 'p': price}.
        fidelity_min: Tamanho do candle em minutos.

    Returns:
        DataFrame OHLCV com DatetimeIndex.
    """
    if not history:
        return pd.DataFrame()

    df_raw = pd.DataFrame(history)
    df_raw.columns = ["timestamp", "price"] if list(df_raw.columns) == ["t", "p"] else df_raw.columns

    # Renomear colunas se necessário
    if "t" in df_raw.columns:
        df_raw = df_raw.rename(columns={"t": "timestamp", "p": "price"})

    df_raw["timestamp"] = pd.to_datetime(df_raw["timestamp"], unit="s")
    df_raw = df_raw.set_index("timestamp").sort_index()
    df_raw["price"] = df_raw["price"].astype(float)

    # Resample em candles OHLCV
    rule = f"{fidelity_min}min"
    ohlcv = df_raw["price"].resample(rule).agg(
        open="first",
        high="max",
        low="min",
        close="last",
    ).dropna()

    # Volume estimado: variação de preço × fator de escala
    ohlcv["volume"] = (ohlcv["high"] - ohlcv["low"]) * 1000

    ohlcv = ohlcv.reset_index()
    ohlcv = ohlcv.rename(columns={"timestamp": "timestamp"})

    return ohlcv


def main():
    parser = argparse.ArgumentParser(
        description="Busca dados históricos reais do Polymarket"
    )
    parser.add_argument(
        "--token",
        default=DEFAULT_TOKEN_ID,
        help="Token ID do mercado (padrão: BTC up/down do polymarket-rbi-bot)",
    )
    parser.add_argument(
        "--interval",
        default="max",
        choices=["max", "1m", "1w", "1d", "6h", "1h"],
        help="Janela de tempo (padrão: max = máximo disponível)",
    )
    parser.add_argument(
        "--fidelity",
        type=int,
        default=15,
        help="Tamanho do candle em minutos (padrão: 15)",
    )
    parser.add_argument(
        "--output",
        default="./data/poly_data.csv",
        help="Caminho do arquivo de saída (padrão: ./data/poly_data.csv)",
    )
    args = parser.parse_args()

    print("=" * 55)
    print("  POLYMARKET — BUSCA DE DADOS HISTÓRICOS REAIS")
    print("=" * 55)

    # Buscar dados
    history = fetch_price_history(
        token_id=args.token,
        interval=args.interval,
        fidelity=args.fidelity,
    )

    if not history:
        print("\n❌ Nenhum dado recebido. Verifique o token ID.")
        print("   Dica: mercados encerrados não retornam histórico via esta API.")
        print("   Tente um mercado ativo em: https://polymarket.com/markets")
        sys.exit(1)

    # Construir candles OHLCV
    print("\nConstruindo candles OHLCV...")
    df = build_ohlcv(history, fidelity_min=args.fidelity)

    if df.empty:
        print("❌ Não foi possível construir candles com os dados recebidos.")
        sys.exit(1)

    # Salvar
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    df.to_csv(args.output, index=False)

    # Resumo
    print(f"\n✅ Dados salvos: {args.output}")
    print(f"   Candles:  {len(df)}")
    print(f"   Período:  {df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]}")
    print(f"   Preço:    min={df['close'].min():.4f} | max={df['close'].max():.4f}")
    print(f"   Timeframe: {args.fidelity}min por candle")
    print()
    print("Próximo passo:")
    print("  python deploy/main.py --mode paper --strategy macd --capital 1000")
    print("  python deploy/main.py --mode paper --strategy rsi --capital 1000")


if __name__ == "__main__":
    main()
