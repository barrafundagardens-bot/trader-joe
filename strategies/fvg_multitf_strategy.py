"""
fvg_multitf_strategy.py — Estratégia FVG Multi-Timeframe para Polymarket.

Fair Value Gap (FVG) é um desequilíbrio de preço onde há um "gap" entre
o high de um candle e o low do candle dois posições depois (ou vice-versa),
indicando que o mercado se moveu tão rapidamente que não houve negociação
em uma certa faixa de preço.

Arquitetura Multi-Timeframe:
    4H (bias):    Detecta FVGs de alta/baixa para definir a DIREÇÃO do trade.
                  FVG bullish no 4H → bias de compra.
                  FVG bearish no 4H → bias de venda.

    15M (entrada): Confirma o timing da entrada.
                   Entrada long quando preço testa (entra no) FVG bullish do 15M
                   E está alinhado com o bias bullish do 4H.
                   Entrada short quando preço testa FVG bearish do 15M
                   E está alinhado com o bias bearish do 4H.

Definição de FVG válido:
    Bullish FVG: candle[i].low > candle[i-2].high
    Bearish FVG: candle[i].high < candle[i-2].low
    Requisito adicional: body do candle central > 50% do range total
    Validade: no máximo MAX_FVG_AGE candles no timeframe respectivo
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

from strategies.base_strategy import BaseStrategy, Signal, SignalType


# ============================================================
# Data Classes
# ============================================================

@dataclass
class FVGZone:
    """Representa um Fair Value Gap detectado."""
    fvg_type: str          # 'bullish' ou 'bearish'
    top: float             # Topo da zona FVG
    bottom: float          # Base da zona FVG
    candle_index: int      # Índice do candle que criou o FVG (o terceiro do padrão)
    timestamp: pd.Timestamp
    is_filled: bool = False  # FVG preenchido pelo preço

    @property
    def midpoint(self) -> float:
        """Ponto médio da zona FVG."""
        return (self.top + self.bottom) / 2

    @property
    def size(self) -> float:
        """Tamanho (gap) da zona FVG."""
        return self.top - self.bottom

    def contains_price(self, price: float) -> bool:
        """Verifica se um preço está dentro da zona FVG."""
        return self.bottom <= price <= self.top

    def __repr__(self) -> str:
        return (
            f"FVG({self.fvg_type} [{self.bottom:.4f}–{self.top:.4f}] "
            f"@{self.timestamp.strftime('%Y-%m-%d %H:%M')} "
            f"{'FILLED' if self.is_filled else 'OPEN'})"
        )


# ============================================================
# Função reutilizável detect_fvg
# ============================================================

def detect_fvg(
    df: pd.DataFrame,
    max_age: int = 10,
    min_body_ratio: float = 0.50,
) -> List[FVGZone]:
    """
    Detecta Fair Value Gaps (FVGs) em um DataFrame OHLC.

    Um FVG é um desequilíbrio de 3 candles onde o candle central tem
    um movimento forte (body > min_body_ratio × range) criando um gap
    entre o segundo e o quarto candle.

    Regras:
        Bullish FVG: candle[i].low > candle[i-2].high
        Bearish FVG: candle[i].high < candle[i-2].low
        Válido se: body do candle i-1 > min_body_ratio × (high - low) do candle i-1
        Válido se: idade ≤ max_age candles (medida a partir do candle mais recente)

    Args:
        df:             DataFrame OHLC com DatetimeIndex.
                        Requer colunas: open, high, low, close.
        max_age:        Número máximo de candles que um FVG pode ter.
                        FVGs mais antigos são descartados.
        min_body_ratio: Fração mínima do body em relação ao range do candle central.
                        Garante que o FVG foi criado por um movimento forte.

    Returns:
        Lista de FVGZone com os FVGs válidos encontrados (mais recente primeiro).
    """
    if len(df) < 3:
        return []

    required_cols = {"open", "high", "low", "close"}
    if not required_cols.issubset(df.columns):
        return []

    fvgs: List[FVGZone] = []
    n = len(df)
    # Limite de busca: apenas os últimos max_age + 2 candles
    start_idx = max(2, n - max_age - 2)

    for i in range(start_idx, n):
        # Candle da esquerda (i-2), central (i-1), da direita (i)
        c_left = df.iloc[i - 2]
        c_mid = df.iloc[i - 1]
        c_right = df.iloc[i]

        # Body ratio do candle central (quanto do movimento foi body vs shadow)
        mid_body = abs(c_mid["close"] - c_mid["open"])
        mid_range = c_mid["high"] - c_mid["low"]

        if mid_range < 1e-10:
            continue  # Candle doji — não conta

        body_ratio = mid_body / mid_range

        if body_ratio < min_body_ratio:
            continue  # Candle sem momentum suficiente

        # Idade do FVG em número de candles a partir do fim do DataFrame
        age = n - 1 - i
        if age > max_age:
            continue  # FVG expirado

        # --------------------------------------------------------
        # Verificar padrão FVG
        # --------------------------------------------------------

        # Bullish FVG: gap entre high do candle esquerdo e low do candle direito
        if c_right["low"] > c_left["high"]:
            fvg = FVGZone(
                fvg_type="bullish",
                top=c_right["low"],        # Topo = low do candle direito
                bottom=c_left["high"],     # Base = high do candle esquerdo
                candle_index=i,
                timestamp=df.index[i],
            )
            # Verificar se o FVG já foi preenchido por um candle posterior
            if i < n - 1:
                subsequent = df.iloc[i + 1:]
                fvg.is_filled = bool((subsequent["low"] <= fvg.bottom).any())
            fvgs.append(fvg)

        # Bearish FVG: gap entre low do candle esquerdo e high do candle direito
        elif c_right["high"] < c_left["low"]:
            fvg = FVGZone(
                fvg_type="bearish",
                top=c_left["low"],         # Topo = low do candle esquerdo
                bottom=c_right["high"],    # Base = high do candle direito
                candle_index=i,
                timestamp=df.index[i],
            )
            if i < n - 1:
                subsequent = df.iloc[i + 1:]
                fvg.is_filled = bool((subsequent["high"] >= fvg.top).any())
            fvgs.append(fvg)

    # Retornar do mais recente ao mais antigo
    return list(reversed(fvgs))


# ============================================================
# Estratégia Principal
# ============================================================

class FVGMultiTFStrategy(BaseStrategy):
    """
    Estratégia Fair Value Gap Multi-Timeframe.

    Combina análise de 4H (bias direcional) com 15M (timing de entrada).
    100% implementada em pandas — sem dependências externas de indicadores.

    Parâmetros configuráveis:
        tf_high:             Timeframe alto para bias ('4h' para 4 horas)
        tf_low:              Timeframe de entrada ('15min' para 15 minutos)
        max_fvg_age_high:    Idade máxima de FVG no TF alto (padrão: 10 candles)
        max_fvg_age_low:     Idade máxima de FVG no TF baixo (padrão: 10 candles)
        min_body_ratio:      Body mínimo para FVG válido (padrão: 0.50)
        entry_tolerance:     Tolerância para considerar preço "no FVG" (padrão: 0.002)
    """

    DEFAULT_PARAMS = {
        "tf_high": "4h",
        "tf_low": "15min",
        "max_fvg_age_high": 10,
        "max_fvg_age_low": 10,
        "min_body_ratio": 0.50,
        "entry_tolerance": 0.002,  # 0.2% de tolerância para entrar na zona
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)

    @property
    def name(self) -> str:
        return "FVGMultiTFStrategy"

    @property
    def min_candles(self) -> int:
        # Precisa de dados suficientes para construir ambos os timeframes
        # 4H = 16 candles de 15M por 4H; precisamos de 10 candles de 4H mínimo
        return 16 * 12 + 50  # ~200 candles de 15M

    def _resample_ohlcv(self, df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        """
        Constrói candles OHLCV para o timeframe solicitado.

        Assume que df tem frequência de 15 minutos ou dados de menor granularidade.
        Usa pandas.resample() para agregar.

        Args:
            df:        DataFrame OHLCV com DatetimeIndex.
            timeframe: String pandas de frequência ('15min', '4h', etc.)

        Returns:
            DataFrame OHLCV resampleado e sem linhas NaN.
        """
        df = df.copy()
        df.index = pd.to_datetime(df.index)

        resampled = df.resample(timeframe).agg({
            "open":   "first",
            "high":   "max",
            "low":    "min",
            "close":  "last",
            "volume": "sum",
        }).dropna(subset=["open", "close"])

        return resampled

    def _get_4h_bias(self, df_4h: pd.DataFrame) -> str:
        """
        Determina o bias direcional com base nos FVGs do timeframe 4H.

        Bias bullish: existe FVG bullish não preenchido e recente no 4H
        Bias bearish: existe FVG bearish não preenchido e recente no 4H
        Bias neutro: sem FVGs relevantes

        Args:
            df_4h: DataFrame de candles 4H.

        Returns:
            'bullish', 'bearish' ou 'neutral'
        """
        max_age = self.get_param("max_fvg_age_high")
        min_body = self.get_param("min_body_ratio")

        fvgs_4h = detect_fvg(df_4h, max_age=max_age, min_body_ratio=min_body)

        if not fvgs_4h:
            return "neutral"

        # Considerar apenas FVGs não preenchidos
        open_fvgs = [f for f in fvgs_4h if not f.is_filled]

        if not open_fvgs:
            return "neutral"

        # O FVG mais recente determina o bias
        latest = open_fvgs[0]
        return latest.fvg_type  # 'bullish' ou 'bearish'

    def _find_entry_signal_15m(
        self,
        df_15m: pd.DataFrame,
        bias: str,
        tolerance: float,
    ) -> Optional[tuple]:
        """
        Procura confirmação de entrada no timeframe 15M.

        Critério de entrada:
            - Preço atual está dentro ou próximo de um FVG alinhado com o bias
            - O FVG é recente (dentro do max_fvg_age_low)
            - O FVG não foi completamente preenchido

        Args:
            df_15m:    DataFrame de candles 15M.
            bias:      'bullish' ou 'bearish' (definido pelo 4H)
            tolerance: Tolerância percentual para considerar preço "no FVG"

        Returns:
            Tupla (FVGZone, price, reason) ou None se sem confirmação.
        """
        max_age = self.get_param("max_fvg_age_low")
        min_body = self.get_param("min_body_ratio")

        fvgs_15m = detect_fvg(df_15m, max_age=max_age, min_body_ratio=min_body)

        if not fvgs_15m:
            return None

        price_curr = df_15m["close"].iloc[-1]
        price_low = df_15m["low"].iloc[-1]
        price_high = df_15m["high"].iloc[-1]

        for fvg in fvgs_15m:
            if fvg.fvg_type != bias:
                continue  # Só entrar em FVGs alinhados com o bias do 4H
            if fvg.is_filled:
                continue  # FVG já preenchido não é uma zona de interesse

            # Expandir a zona de entrada com tolerância
            zone_bottom = fvg.bottom * (1 - tolerance)
            zone_top = fvg.top * (1 + tolerance)

            # Verificar se o preço está na zona (fill ou teste do FVG)
            price_in_zone = zone_bottom <= price_curr <= zone_top
            candle_touching_zone = (price_low <= zone_top) and (price_high >= zone_bottom)

            if price_in_zone or candle_touching_zone:
                reason = (
                    f"Preço {'dentro' if price_in_zone else 'tocando'} do FVG {bias} 15M "
                    f"[{fvg.bottom:.4f}–{fvg.top:.4f}] "
                    f"(age={fvg.candle_index}, filled={fvg.is_filled})"
                )
                return fvg, price_curr, reason

        return None

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Gera sinal FVG Multi-Timeframe.

        Constrói candles 4H e 15M a partir do DataFrame de entrada,
        determina bias no 4H e confirma entrada no 15M.

        Args:
            df: DataFrame OHLCV base com DatetimeIndex.
                Pode ser de qualquer frequência >= 15M.
                Se for de 15M já, é usado diretamente para o TF baixo.

        Returns:
            Signal com BUY, SELL ou HOLD.
        """
        if not self.validate_dataframe(df):
            return self.hold_signal("DataFrame inválido ou insuficiente")

        timestamp = df.index[-1]
        tolerance = self.get_param("entry_tolerance")

        # ----------------------------------------------------------------
        # Construir timeframes via pandas resample
        # ----------------------------------------------------------------
        try:
            df_15m = self._resample_ohlcv(df, self.get_param("tf_low"))
            df_4h = self._resample_ohlcv(df, self.get_param("tf_high"))
        except Exception as e:
            return self.hold_signal(f"Erro no resample: {e}", timestamp)

        if len(df_4h) < 3:
            return self.hold_signal(
                f"Dados insuficientes para 4H: {len(df_4h)} candles", timestamp
            )
        if len(df_15m) < 3:
            return self.hold_signal(
                f"Dados insuficientes para 15M: {len(df_15m)} candles", timestamp
            )

        # ----------------------------------------------------------------
        # Passo 1: Determinar bias via FVG do 4H
        # ----------------------------------------------------------------
        bias = self._get_4h_bias(df_4h)

        if bias == "neutral":
            return self.hold_signal(
                "Sem FVG válido no 4H — bias neutro, aguardando setup",
                timestamp,
            )

        # ----------------------------------------------------------------
        # Passo 2: Confirmar entrada via FVG do 15M
        # ----------------------------------------------------------------
        entry_result = self._find_entry_signal_15m(df_15m, bias, tolerance)

        if entry_result is None:
            return self.hold_signal(
                f"Bias {bias} no 4H, mas sem confirmação no 15M",
                timestamp,
            )

        fvg_15m, price_curr, entry_reason = entry_result

        # ----------------------------------------------------------------
        # Calcular confiança baseada no tamanho e recência do FVG
        # ----------------------------------------------------------------
        max_age = self.get_param("max_fvg_age_low")
        age_factor = 1.0 - (len(df_15m) - 1 - fvg_15m.candle_index) / max(max_age, 1)
        size_factor = min(1.0, fvg_15m.size * 100)  # Maior gap = maior confiança
        confidence = min(0.90, 0.60 + age_factor * 0.15 + size_factor * 0.15)

        # ----------------------------------------------------------------
        # Construir metadados para log e backtesting
        # ----------------------------------------------------------------
        metadata = {
            "bias_4h": bias,
            "fvg_15m_type": fvg_15m.fvg_type,
            "fvg_15m_top": fvg_15m.top,
            "fvg_15m_bottom": fvg_15m.bottom,
            "fvg_15m_midpoint": fvg_15m.midpoint,
            "fvg_15m_size": fvg_15m.size,
            "fvg_15m_timestamp": str(fvg_15m.timestamp),
            "candles_4h": len(df_4h),
            "candles_15m": len(df_15m),
        }

        # ----------------------------------------------------------------
        # Gerar sinal direcional
        # ----------------------------------------------------------------
        if bias == "bullish":
            return Signal(
                signal_type=SignalType.BUY,
                price=price_curr,
                confidence=confidence,
                strategy=self.name,
                reason=(
                    f"[4H FVG Bullish bias] + [15M: {entry_reason}] "
                    f"→ Entrada LONG @ {price_curr:.4f}"
                ),
                timestamp=timestamp,
                metadata=metadata,
            )
        else:  # bearish
            return Signal(
                signal_type=SignalType.SELL,
                price=price_curr,
                confidence=confidence,
                strategy=self.name,
                reason=(
                    f"[4H FVG Bearish bias] + [15M: {entry_reason}] "
                    f"→ Entrada SHORT @ {price_curr:.4f}"
                ),
                timestamp=timestamp,
                metadata=metadata,
            )
