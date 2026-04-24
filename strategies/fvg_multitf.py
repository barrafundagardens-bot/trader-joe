"""
strategies/fvg_multitf.py — Estratégia FVG Multi-Timeframe para Polymarket.

Fair Value Gap (FVG) é um desequilíbrio de preço criado quando o mercado
se move tão rapidamente que deixa um "gap" sem negociação entre candles.

Arquitetura Multi-Timeframe:
    4H  (bias):    FVG bullish no 4H → só entrar LONG.
                   FVG bearish no 4H → só entrar SHORT.
    15M (entrada): Confirma timing exato quando preço testa a zona FVG.

Definição formal de FVG (3 candles):
    Bullish: candle[i].low  > candle[i-2].high  → gap entre i-2 e i
    Bearish: candle[i].high < candle[i-2].low   → gap entre i-2 e i

    Requisito adicional:
        body do candle central (i-1) > 50% do range total
        (garante movimento forte, não lateralização)

    Validade: no máximo 10 candles de idade no timeframe respectivo.

Candles construídos via pandas.resample() internamente a partir dos dados brutos.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

from strategies.base_strategy import BaseStrategy, Signal, SignalType


# ============================================================
# FVG Zone — representa um gap detectado
# ============================================================

@dataclass
class FVGZone:
    """
    Representa uma zona de Fair Value Gap detectada.

    Attributes:
        fvg_type:     'bullish' ou 'bearish'
        top:          Topo da zona (preço máximo do gap)
        bottom:       Base da zona (preço mínimo do gap)
        candle_index: Índice do candle que criou o FVG (terceiro do padrão)
        timestamp:    Timestamp do candle que criou o FVG
        is_filled:    True se o preço já passou pela zona completamente
    """

    fvg_type: str
    top: float
    bottom: float
    candle_index: int
    timestamp: pd.Timestamp
    is_filled: bool = False

    @property
    def midpoint(self) -> float:
        """Ponto médio da zona FVG."""
        return (self.top + self.bottom) / 2.0

    @property
    def size(self) -> float:
        """Amplitude do gap (top - bottom)."""
        return self.top - self.bottom

    def contains_price(self, price: float) -> bool:
        """Retorna True se o preço está dentro da zona FVG."""
        return self.bottom <= price <= self.top

    def __repr__(self) -> str:
        status = "FILLED" if self.is_filled else "OPEN"
        ts = self.timestamp.strftime("%Y-%m-%d %H:%M") if self.timestamp else "?"
        return (
            f"FVG({self.fvg_type} [{self.bottom:.4f}–{self.top:.4f}] "
            f"@{ts} [{status}])"
        )


# ============================================================
# detect_fvg — função reutilizável
# ============================================================

def detect_fvg(
    df: pd.DataFrame,
    max_age: int = 10,
    min_body_ratio: float = 0.50,
) -> List[FVGZone]:
    """
    Detecta Fair Value Gaps (FVGs) em um DataFrame OHLC.

    Itera sobre os candles do DataFrame procurando o padrão de 3 candles
    que forma um desequilíbrio de preço. Retorna apenas FVGs que:
        - Têm body ratio >= min_body_ratio no candle central
        - Têm no máximo max_age candles de idade

    Args:
        df:             DataFrame com colunas [open, high, low, close] e DatetimeIndex.
        max_age:        Número máximo de candles de idade para um FVG ser válido.
                        FVGs mais antigos são descartados. (padrão: 10)
        min_body_ratio: Fração mínima do body vs range do candle central.
                        Filtra movimentos fracos/doji. (padrão: 0.50 = 50%)

    Returns:
        Lista de FVGZone, ordenada do mais recente ao mais antigo.
        Lista vazia se não houver FVGs válidos.

    Example:
        >>> df_4h = resample_ohlcv(df_raw, '4h')
        >>> fvgs = detect_fvg(df_4h, max_age=10)
        >>> bullish = [f for f in fvgs if f.fvg_type == 'bullish']
    """
    required = {"open", "high", "low", "close"}
    if not required.issubset(df.columns) or len(df) < 3:
        return []

    fvgs: List[FVGZone] = []
    n = len(df)

    # Só analisar os últimos (max_age + 2) candles — o resto é muito antigo
    start = max(2, n - max_age - 2)

    for i in range(start, n):
        c_left = df.iloc[i - 2]   # Primeiro candle do padrão
        c_mid  = df.iloc[i - 1]   # Segundo candle (movimento forte)
        c_right = df.iloc[i]      # Terceiro candle (cria o gap)

        # --------------------------------------------------------
        # Validar body ratio do candle central
        # --------------------------------------------------------
        mid_body  = abs(c_mid["close"] - c_mid["open"])
        mid_range = c_mid["high"] - c_mid["low"]

        # Ignorar candles doji (range zero ou quase zero)
        if mid_range < 1e-10:
            continue

        if (mid_body / mid_range) < min_body_ratio:
            continue  # Movimento fraco — FVG não confiável

        # --------------------------------------------------------
        # Calcular idade: quantos candles atrás foi criado este FVG
        # --------------------------------------------------------
        age = (n - 1) - i  # 0 = candle atual, 1 = candle anterior, etc.
        if age > max_age:
            continue

        # --------------------------------------------------------
        # Bullish FVG: gap entre high[i-2] e low[i]
        # O gap indica que nenhum vendedor preencheu o espaço entre
        # o high do primeiro candle e o low do terceiro.
        # --------------------------------------------------------
        if c_right["low"] > c_left["high"]:
            fvg = FVGZone(
                fvg_type="bullish",
                top=c_right["low"],      # Limite superior = low do candle direito
                bottom=c_left["high"],   # Limite inferior = high do candle esquerdo
                candle_index=i,
                timestamp=df.index[i],
            )
            # Verificar se candles posteriores preencheram a zona
            if i < n - 1:
                posterior = df.iloc[i + 1:]
                fvg.is_filled = bool((posterior["low"] <= fvg.bottom).any())
            fvgs.append(fvg)

        # --------------------------------------------------------
        # Bearish FVG: gap entre low[i-2] e high[i]
        # Indica que nenhum comprador preencheu o espaço entre
        # o low do primeiro candle e o high do terceiro.
        # --------------------------------------------------------
        elif c_right["high"] < c_left["low"]:
            fvg = FVGZone(
                fvg_type="bearish",
                top=c_left["low"],       # Limite superior = low do candle esquerdo
                bottom=c_right["high"],  # Limite inferior = high do candle direito
                candle_index=i,
                timestamp=df.index[i],
            )
            if i < n - 1:
                posterior = df.iloc[i + 1:]
                fvg.is_filled = bool((posterior["high"] >= fvg.top).any())
            fvgs.append(fvg)

    # Mais recente primeiro
    return list(reversed(fvgs))


# ============================================================
# Estratégia Principal
# ============================================================

class FVGMultiTF(BaseStrategy):
    """
    Estratégia Fair Value Gap Multi-Timeframe.

    Passo 1 — Bias via 4H:
        Detecta FVGs no timeframe de 4 horas.
        FVG bullish aberto → bias = 'bullish' → só entrar LONG.
        FVG bearish aberto → bias = 'bearish' → só entrar SHORT.
        Sem FVG válido     → bias = 'neutral' → HOLD.

    Passo 2 — Entrada via 15M:
        Com bias definido, procura FVG alinhado no timeframe de 15 minutos.
        Entrada quando o preço atual entra (ou toca) a zona FVG do 15M.
        O FVG do 15M deve ter o mesmo tipo do bias do 4H.

    Parâmetros:
        tf_high:          Timeframe alto para bias (padrão: '4h')
        tf_low:           Timeframe de entrada (padrão: '15min')
        max_fvg_age_high: Validade máxima do FVG no 4H em candles (padrão: 10)
        max_fvg_age_low:  Validade máxima do FVG no 15M em candles (padrão: 10)
        min_body_ratio:   Body ratio mínimo para FVG válido (padrão: 0.50)
        entry_tolerance:  Tolerância % para entrar na zona FVG (padrão: 0.002)
    """

    DEFAULT_PARAMS = {
        "tf_high": "4h",
        "tf_low": "15min",
        "max_fvg_age_high": 10,
        "max_fvg_age_low": 10,
        "min_body_ratio": 0.50,
        "entry_tolerance": 0.002,  # 0.2% de tolerância
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)

    @property
    def name(self) -> str:
        return "FVGMultiTF"

    @property
    def min_candles(self) -> int:
        # Suficiente para construir ao menos 10 candles de 4H a partir de dados de 1M
        # 4H × 10 = 40H → ~2400 candles de 1M | em 15M = 160 candles
        return 160

    # ----------------------------------------------------------------
    # Resample interno
    # ----------------------------------------------------------------

    @staticmethod
    def _resample_ohlcv(df: pd.DataFrame, freq: str) -> pd.DataFrame:
        """
        Constrói candles OHLCV para o timeframe solicitado via pandas.resample().

        Args:
            df:   DataFrame OHLCV base com DatetimeIndex.
            freq: Frequência pandas ('15min', '4h', '1h', etc.)

        Returns:
            DataFrame OHLCV resampleado, sem linhas com NaN em open/close.
        """
        df = df.copy()
        df.index = pd.to_datetime(df.index)

        agg: dict = {
            "open":   "first",
            "high":   "max",
            "low":    "min",
            "close":  "last",
        }
        if "volume" in df.columns:
            agg["volume"] = "sum"

        return df.resample(freq).agg(agg).dropna(subset=["open", "close"])

    # ----------------------------------------------------------------
    # Bias via 4H
    # ----------------------------------------------------------------

    def _get_bias(self, df: pd.DataFrame) -> str:
        """
        Determina o bias direcional detectando FVGs no timeframe 4H.

        Returns:
            'bullish', 'bearish' ou 'neutral'
        """
        df_4h = self._resample_ohlcv(df, self.get_param("tf_high"))

        if len(df_4h) < 3:
            return "neutral"

        fvgs = detect_fvg(
            df_4h,
            max_age=self.get_param("max_fvg_age_high"),
            min_body_ratio=self.get_param("min_body_ratio"),
        )

        # Apenas FVGs abertos (não preenchidos)
        open_fvgs = [f for f in fvgs if not f.is_filled]

        if not open_fvgs:
            return "neutral"

        # FVG mais recente define o bias
        return open_fvgs[0].fvg_type  # 'bullish' ou 'bearish'

    # ----------------------------------------------------------------
    # Confirmação via 15M
    # ----------------------------------------------------------------

    def _confirm_entry_15m(
        self,
        df: pd.DataFrame,
        bias: str,
    ) -> Optional[tuple]:
        """
        Procura confirmação de entrada no timeframe 15M.

        Critérios:
            1. Existe FVG do mesmo tipo do bias no 15M
            2. FVG não foi completamente preenchido
            3. Preço atual está dentro ou tocando a zona (com tolerância)

        Args:
            df:   DataFrame base.
            bias: 'bullish' ou 'bearish'

        Returns:
            (FVGZone, price, reason) ou None se sem confirmação.
        """
        df_15m = self._resample_ohlcv(df, self.get_param("tf_low"))

        if len(df_15m) < 3:
            return None

        fvgs = detect_fvg(
            df_15m,
            max_age=self.get_param("max_fvg_age_low"),
            min_body_ratio=self.get_param("min_body_ratio"),
        )

        tol = self.get_param("entry_tolerance")
        price = df_15m["close"].iloc[-1]
        candle_low = df_15m["low"].iloc[-1]
        candle_high = df_15m["high"].iloc[-1]

        for fvg in fvgs:
            # Só considerar FVGs alinhados com o bias do 4H
            if fvg.fvg_type != bias:
                continue
            if fvg.is_filled:
                continue

            # Zona expandida com tolerância
            zone_bottom = fvg.bottom * (1 - tol)
            zone_top    = fvg.top    * (1 + tol)

            # Preço dentro da zona ou candle tocando a zona
            price_in    = zone_bottom <= price <= zone_top
            touching    = candle_low <= zone_top and candle_high >= zone_bottom

            if price_in or touching:
                how = "dentro" if price_in else "tocando"
                reason = (
                    f"Preço {how} do FVG {bias} 15M "
                    f"[{fvg.bottom:.4f}–{fvg.top:.4f}] "
                    f"(gap={fvg.size:.4f}, filled={fvg.is_filled})"
                )
                return fvg, price, reason

        return None

    # ----------------------------------------------------------------
    # generate_signal — interface obrigatória de BaseStrategy
    # ----------------------------------------------------------------

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Gera sinal FVG Multi-Timeframe.

        1. Constrói 4H a partir de df → detecta bias
        2. Se bias válido, constrói 15M → confirma entrada
        3. Retorna BUY/SELL/HOLD com metadados completos

        Args:
            df: DataFrame OHLCV base com DatetimeIndex.

        Returns:
            Signal com BUY, SELL ou HOLD.
        """
        if not self.validate_dataframe(df):
            return self.hold_signal("DataFrame inválido ou insuficiente")

        timestamp = df.index[-1]

        # --------------------------------------------------------
        # Passo 1: Bias via 4H
        # --------------------------------------------------------
        try:
            bias = self._get_bias(df)
        except Exception as e:
            return self.hold_signal(f"Erro ao calcular bias 4H: {e}", timestamp)

        if bias == "neutral":
            return self.hold_signal(
                "Sem FVG válido no 4H — aguardando setup direcional",
                timestamp,
            )

        # --------------------------------------------------------
        # Passo 2: Confirmação via 15M
        # --------------------------------------------------------
        try:
            result = self._confirm_entry_15m(df, bias)
        except Exception as e:
            return self.hold_signal(f"Erro ao confirmar entrada 15M: {e}", timestamp)

        if result is None:
            return self.hold_signal(
                f"Bias {bias} no 4H confirmado, mas sem toque no FVG do 15M",
                timestamp,
            )

        fvg_15m, price, entry_reason = result

        # --------------------------------------------------------
        # Confiança: penaliza FVGs mais velhos e de gap menor
        # --------------------------------------------------------
        max_age = self.get_param("max_fvg_age_low")
        n_15m = len(self._resample_ohlcv(df, self.get_param("tf_low")))
        age = max(0, n_15m - 1 - fvg_15m.candle_index)
        age_factor  = 1.0 - (age / max(max_age, 1))
        size_factor = min(1.0, fvg_15m.size * 200)  # gaps maiores = mais confiança
        confidence  = min(0.90, 0.60 + age_factor * 0.15 + size_factor * 0.15)

        # --------------------------------------------------------
        # Sinal direcional
        # --------------------------------------------------------
        metadata = {
            "bias_4h":           bias,
            "fvg_15m_type":      fvg_15m.fvg_type,
            "fvg_15m_top":       fvg_15m.top,
            "fvg_15m_bottom":    fvg_15m.bottom,
            "fvg_15m_midpoint":  fvg_15m.midpoint,
            "fvg_15m_size":      fvg_15m.size,
            "fvg_15m_timestamp": str(fvg_15m.timestamp),
            "fvg_15m_age":       age,
        }

        if bias == "bullish":
            return Signal(
                signal_type=SignalType.BUY,
                price=price,
                confidence=confidence,
                strategy=self.name,
                reason=(
                    f"[4H FVG Bullish] + [15M: {entry_reason}] "
                    f"→ LONG @ {price:.4f}"
                ),
                timestamp=timestamp,
                metadata=metadata,
            )
        else:
            return Signal(
                signal_type=SignalType.SELL,
                price=price,
                confidence=confidence,
                strategy=self.name,
                reason=(
                    f"[4H FVG Bearish] + [15M: {entry_reason}] "
                    f"→ SHORT @ {price:.4f}"
                ),
                timestamp=timestamp,
                metadata=metadata,
            )
