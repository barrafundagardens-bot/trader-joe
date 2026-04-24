"""
deploy/generate_sample_data.py — Gera dados OHLCV sintéticos para testes.

Cria um arquivo ./data/poly_data.csv com dados realistas de um mercado
de predição (preços entre 0.0 e 1.0) para testar as estratégias offline.

Uso:
    python deploy/generate_sample_data.py
"""

import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

np.random.seed(42)

# Aceita --candles N como argumento opcional
CANDLES = 2000
for i, arg in enumerate(sys.argv):
    if arg == "--candles" and i + 1 < len(sys.argv):
        CANDLES = int(sys.argv[i + 1])

START_PRICE = 0.45      # Preço inicial (45% de chance)
INTERVAL_MIN = 15       # Candles de 15 minutos

print(f"Gerando {CANDLES} candles sintéticos de mercado de predição...")

timestamps = [
    datetime(2025, 1, 1) + timedelta(minutes=i * INTERVAL_MIN)
    for i in range(CANDLES)
]

# Simular caminhada aleatória com tendências e reversão à média
# Volatilidade mais alta = mais cruzamentos MACD = mais trades no backtest
prices = [START_PRICE]
trend = 0.0
for i in range(CANDLES - 1):
    # Tendência que muda de direção ocasionalmente (a cada ~80 candles)
    if i % 80 == 0:
        trend = np.random.normal(0, 0.0008)
    mean_reversion = 0.0005 * (0.5 - prices[-1])  # Reversão suave
    shock = np.random.normal(0, 0.018)             # Volatilidade 2x maior
    new_price = prices[-1] + trend + mean_reversion + shock
    new_price = max(0.03, min(0.97, new_price))
    prices.append(new_price)

closes = np.array(prices)

# Construir OHLCV realista a partir dos closes
half_spread = np.random.uniform(0.003, 0.015, CANDLES)
highs  = np.minimum(0.99, closes + half_spread * np.random.uniform(0.5, 2.0, CANDLES))
lows   = np.maximum(0.01, closes - half_spread * np.random.uniform(0.5, 2.0, CANDLES))
opens  = np.roll(closes, 1); opens[0] = closes[0]

# Volume com spikes ocasionais (simula atividade de whales)
base_volume = np.random.lognormal(mean=3.0, sigma=0.5, size=CANDLES)
spike_mask  = np.random.random(CANDLES) < 0.05     # 5% de candles com spike
base_volume[spike_mask] *= np.random.uniform(3, 8, spike_mask.sum())
volumes = base_volume

df = pd.DataFrame({
    "timestamp": timestamps,
    "open":   np.round(opens,  4),
    "high":   np.round(highs,  4),
    "low":    np.round(lows,   4),
    "close":  np.round(closes, 4),
    "volume": np.round(volumes, 2),
})

os.makedirs("./data", exist_ok=True)
df.to_csv("./data/poly_data.csv", index=False)

print(f"Arquivo criado: ./data/poly_data.csv")
print(f"  Candles : {CANDLES} ({INTERVAL_MIN}min cada)")
print(f"  Período : {timestamps[0].date()} → {timestamps[-1].date()}")
print(f"  Preço   : {closes.min():.4f} → {closes.max():.4f} (close)")
print(f"  Volume  : média={volumes.mean():.1f} | max={volumes.max():.1f}")
print("")
print("Agora rode:")
print("  python deploy/main.py --mode paper --strategy ensemble --capital 1000")
