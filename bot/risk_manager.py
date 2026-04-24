"""
bot/risk_manager.py — Controle de risco para o trading bot.

Responsabilidades:
    1. Validar cada sinal antes de executar (tamanho, capital, limites)
    2. Monitorar drawdown e acionar modo de emergência se necessário
    3. Aplicar stop-loss por trade (10% do tamanho por padrão)
    4. Limitar número de trades por dia
    5. Impedir novas ordens em modo de emergência

REGRAS NÃO NEGOCIÁVEIS:
    - Stop-loss máximo por trade: MAX_LOSS_PER_TRADE % do tamanho
    - Limite de trades por dia: MAX_TRADES_PER_DAY
    - Emergência automática se drawdown >= EMERGENCY_DRAWDOWN
    - Capital por trade limitado a MAX_TRADE_SIZE
    - Nunca usar mais de 95% do capital disponível
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional

from strategies.base_strategy import Signal, SignalType

logger = logging.getLogger(__name__)


@dataclass
class RiskDecision:
    """
    Decisão do RiskManager sobre um sinal.

    approved:   True se o sinal pode ser executado.
    reason:     Motivo da aprovação ou rejeição.
    adjusted_size: Tamanho ajustado (pode ser menor que o solicitado).
    stop_price: Preço de stop-loss calculado para este trade.
    """
    approved: bool
    reason: str
    adjusted_size: float = 0.0
    stop_price: float = 0.0

    def __repr__(self) -> str:
        status = "✅ APROVADO" if self.approved else "❌ REJEITADO"
        return f"RiskDecision({status}: {self.reason}, size={self.adjusted_size:.2f})"


class RiskManager:
    """
    Gerenciador de risco para o trading bot.

    Valida sinais antes da execução e monitora o estado do portfólio.
    Em modo de emergência, rejeita TODOS os novos sinais até reset manual.

    Args:
        initial_capital:    Capital inicial em USDC.
        max_trade_size:     Tamanho máximo por trade em USDC.
        max_loss_per_trade: Stop-loss em % do tamanho (0.10 = 10%).
        max_trades_per_day: Limite de trades por dia.
        emergency_drawdown: Drawdown máximo antes de emergência (0.20 = 20%).
    """

    def __init__(
        self,
        initial_capital: float = 100.0,
        max_trade_size: float = 10.0,
        max_loss_per_trade: float = 0.10,
        max_trades_per_day: int = 15,
        emergency_drawdown: float = 0.20,
    ):
        if max_trade_size <= 0:
            raise ValueError("max_trade_size deve ser positivo")
        if not (0 < max_loss_per_trade <= 1.0):
            raise ValueError("max_loss_per_trade deve estar entre 0 e 1")
        if max_trades_per_day <= 0:
            raise ValueError("max_trades_per_day deve ser positivo")
        if not (0 < emergency_drawdown <= 1.0):
            raise ValueError("emergency_drawdown deve estar entre 0 e 1")

        self.initial_capital = initial_capital
        self.max_trade_size = max_trade_size
        self.max_loss_per_trade = max_loss_per_trade
        self.max_trades_per_day = max_trades_per_day
        self.emergency_drawdown = emergency_drawdown

        # Estado interno
        self._current_capital: float = initial_capital
        self._peak_capital: float = initial_capital
        self._trades_today: int = 0
        self._last_trade_date: Optional[date] = None
        self._emergency_mode: bool = False
        self._total_trades: int = 0
        self._total_pnl: float = 0.0
        self._trade_history: List[dict] = []

        logger.info(
            f"RiskManager iniciado: capital=${initial_capital:.2f} | "
            f"max_size=${max_trade_size:.2f} | stop={max_loss_per_trade*100:.0f}% | "
            f"max_trades/dia={max_trades_per_day} | emergency={emergency_drawdown*100:.0f}%"
        )

    # ----------------------------------------------------------------
    # Propriedades de estado
    # ----------------------------------------------------------------

    @property
    def current_capital(self) -> float:
        return self._current_capital

    @property
    def drawdown(self) -> float:
        """Drawdown atual em % em relação ao pico de capital."""
        if self._peak_capital <= 0:
            return 0.0
        return (self._peak_capital - self._current_capital) / self._peak_capital

    @property
    def is_emergency_mode(self) -> bool:
        return self._emergency_mode

    @property
    def trades_today(self) -> int:
        self._reset_daily_counter_if_needed()
        return self._trades_today

    # ----------------------------------------------------------------
    # Validação de sinais
    # ----------------------------------------------------------------

    def evaluate(self, signal: Signal) -> RiskDecision:
        """
        Avalia um sinal e decide se pode ser executado.

        Verificações em ordem:
            1. Modo de emergência ativo?
            2. Sinal é HOLD?
            3. Capital insuficiente?
            4. Limite diário de trades atingido?
            5. Drawdown crítico?
            6. Ajuste de tamanho se necessário

        Args:
            signal: Sinal gerado pela estratégia.

        Returns:
            RiskDecision com aprovação e tamanho ajustado.
        """
        # 1. Modo de emergência
        if self._emergency_mode:
            return RiskDecision(
                approved=False,
                reason="MODO DE EMERGÊNCIA ATIVO — Todas as novas ordens bloqueadas. "
                       "Resolva manualmente e chame reset_emergency().",
            )

        # 2. Sinal de HOLD não requer ação
        if not signal.is_actionable:
            return RiskDecision(approved=False, reason="Sinal HOLD — sem ação necessária")

        # 3. Capital disponível
        if self._current_capital < 1.0:
            return RiskDecision(
                approved=False,
                reason=f"Capital insuficiente: ${self._current_capital:.2f}",
            )

        # 4. Limite diário de trades
        self._reset_daily_counter_if_needed()
        if self._trades_today >= self.max_trades_per_day:
            return RiskDecision(
                approved=False,
                reason=(
                    f"Limite diário atingido: {self._trades_today}/{self.max_trades_per_day} trades hoje. "
                    f"Aguarde o próximo dia."
                ),
            )

        # 5. Drawdown alto (aviso antes da emergência)
        current_drawdown = self.drawdown
        warning_threshold = self.emergency_drawdown * 0.75  # 75% do limite = aviso
        if current_drawdown >= warning_threshold:
            logger.warning(
                f"Drawdown em {current_drawdown*100:.1f}% "
                f"(limite de emergência: {self.emergency_drawdown*100:.0f}%). "
                f"Reduzindo tamanho por segurança."
            )
            # Reduzir tamanho para 50% em drawdown elevado
            effective_size = min(
                self.max_trade_size * 0.50,
                self._current_capital * 0.95,
            )
        else:
            effective_size = min(
                self.max_trade_size,
                self._current_capital * 0.95,  # Nunca mais que 95% do capital
            )

        effective_size = max(0.0, effective_size)

        if effective_size < 1.0:
            return RiskDecision(
                approved=False,
                reason=f"Tamanho efetivo muito pequeno: ${effective_size:.2f}",
            )

        # 6. Calcular stop-loss price
        stop_price = self._calculate_stop_price(signal, effective_size)

        logger.info(
            f"Sinal APROVADO: {signal.signal_type.value} @ {signal.price:.4f} | "
            f"size=${effective_size:.2f} | stop={stop_price:.4f} | "
            f"drawdown={current_drawdown*100:.1f}% | trades hoje={self._trades_today+1}"
        )

        return RiskDecision(
            approved=True,
            reason=(
                f"Aprovado: {signal.signal_type.value} size=${effective_size:.2f} "
                f"stop={stop_price:.4f}"
            ),
            adjusted_size=effective_size,
            stop_price=stop_price,
        )

    def _calculate_stop_price(self, signal: Signal, size: float) -> float:
        """
        Calcula o preço de stop-loss para o trade.

        Para BUY: stop abaixo do preço de entrada (max_loss_per_trade %)
        Para SELL: stop acima do preço de entrada (max_loss_per_trade %)

        Args:
            signal: Sinal do trade.
            size:   Tamanho do trade (não usado no cálculo do preço, apenas para log).

        Returns:
            Preço de stop-loss.
        """
        if signal.signal_type == SignalType.BUY:
            return signal.price * (1 - self.max_loss_per_trade)
        else:
            return signal.price * (1 + self.max_loss_per_trade)

    def calculate_emergency_stop(self, entry_price: float, is_buy: bool, position_age_minutes: float) -> float:
        """
        ✅ NEW: Calcula stop-loss de emergência baseado em tempo.

        Se posição estiver aberta > 30 minutos, aplicar stop mais agressivo (-15%).
        Isso protege contra mercados que não se liquidam rápido.

        Args:
            entry_price: Preço de entrada
            is_buy: True para BUY, False para SELL
            position_age_minutes: Idade da posição em minutos

        Returns:
            Preço de emergency stop (ou None se não aplicável)
        """
        EMERGENCY_TIMEOUT_MINUTES = 30
        EMERGENCY_STOP_LOSS = 0.15  # -15% instead of -33%

        if position_age_minutes > EMERGENCY_TIMEOUT_MINUTES:
            if is_buy:
                return entry_price * (1 - EMERGENCY_STOP_LOSS)
            else:
                return entry_price * (1 + EMERGENCY_STOP_LOSS)

        return None

    # ----------------------------------------------------------------
    # Registro de trades executados
    # ----------------------------------------------------------------

    def register_trade_open(self, signal: Signal, size: float) -> None:
        """
        Registra a abertura de um trade.
        Deve ser chamado logo após a execução de uma ordem.

        Args:
            signal: Sinal que gerou o trade.
            size:   Tamanho real executado em USDC.
        """
        self._reset_daily_counter_if_needed()
        self._trades_today += 1
        self._total_trades += 1
        self._current_capital -= size  # Capital comprometido

        logger.info(
            f"Trade aberto registrado: {signal.signal_type.value} "
            f"size=${size:.2f} | capital restante=${self._current_capital:.2f} | "
            f"trades hoje={self._trades_today}/{self.max_trades_per_day}"
        )

    def register_trade_close(self, pnl: float, size: float) -> None:
        """
        Registra o fechamento de um trade com PnL.
        Atualiza capital, drawdown e verifica emergência.

        Args:
            pnl:  Lucro/prejuízo do trade em USDC.
            size: Tamanho do trade que foi fechado.
        """
        # Retornar capital + PnL
        self._current_capital += size + pnl
        self._total_pnl += pnl

        # Atualizar pico de capital
        if self._current_capital > self._peak_capital:
            self._peak_capital = self._current_capital

        # Verificar drawdown para emergência
        current_drawdown = self.drawdown
        if current_drawdown >= self.emergency_drawdown:
            self._activate_emergency(current_drawdown)

        self._trade_history.append({
            "timestamp": datetime.now().isoformat(),
            "pnl": pnl,
            "size": size,
            "capital": self._current_capital,
            "drawdown": current_drawdown,
        })

        logger.info(
            f"Trade fechado: PnL={pnl:+.3f} | "
            f"capital=${self._current_capital:.2f} | "
            f"drawdown={current_drawdown*100:.1f}%"
        )

    # ----------------------------------------------------------------
    # Modo de emergência
    # ----------------------------------------------------------------

    def _activate_emergency(self, drawdown: float) -> None:
        """Ativa o modo de emergência e registra o motivo."""
        if not self._emergency_mode:
            self._emergency_mode = True
            logger.critical(
                f"🚨 MODO DE EMERGÊNCIA ATIVADO! "
                f"Drawdown={drawdown*100:.1f}% >= limite={self.emergency_drawdown*100:.0f}%. "
                f"Todas as novas ordens foram BLOQUEADAS. "
                f"Verifique posições abertas MANUALMENTE no Polymarket. "
                f"Chame risk_manager.reset_emergency() para retomar."
            )

    def reset_emergency(self) -> None:
        """
        Reseta o modo de emergência MANUALMENTE.
        Só deve ser chamado após revisão manual da situação.

        ⚠️ Use com extrema cautela.
        """
        if not self._emergency_mode:
            logger.info("Modo de emergência já estava desativado.")
            return

        self._emergency_mode = False
        # Resetar pico de capital para o nível atual (evitar re-trigger imediato)
        self._peak_capital = self._current_capital
        logger.warning(
            f"⚠️ Modo de emergência RESETADO manualmente. "
            f"Capital atual=${self._current_capital:.2f}. "
            f"Novo pico de referência=${self._peak_capital:.2f}. "
            f"Monitore de perto as próximas operações."
        )

    # ----------------------------------------------------------------
    # Utilitários
    # ----------------------------------------------------------------

    def _reset_daily_counter_if_needed(self) -> None:
        """Reseta o contador diário se o dia mudou."""
        today = date.today()
        if self._last_trade_date != today:
            if self._last_trade_date is not None:
                logger.info(
                    f"Novo dia: resetando contador de trades "
                    f"({self._trades_today} trades ontem)"
                )
            self._trades_today = 0
            self._last_trade_date = today

    def get_status(self) -> dict:
        """Retorna o status atual do RiskManager para monitoramento."""
        self._reset_daily_counter_if_needed()
        return {
            "current_capital": self._current_capital,
            "peak_capital": self._peak_capital,
            "drawdown_pct": self.drawdown * 100,
            "trades_today": self._trades_today,
            "max_trades_per_day": self.max_trades_per_day,
            "emergency_mode": self._emergency_mode,
            "total_trades": self._total_trades,
            "total_pnl": self._total_pnl,
        }

    def print_status(self) -> None:
        """Imprime o status atual formatado."""
        s = self.get_status()
        print("=" * 45)
        print("  RISK MANAGER — STATUS")
        print("=" * 45)
        print(f"  Capital atual:    ${s['current_capital']:.2f}")
        print(f"  Pico de capital:  ${s['peak_capital']:.2f}")
        print(f"  Drawdown atual:   {s['drawdown_pct']:.1f}%")
        print(f"  Trades hoje:      {s['trades_today']}/{s['max_trades_per_day']}")
        print(f"  Total de trades:  {s['total_trades']}")
        print(f"  PnL total:        ${s['total_pnl']:+.2f}")
        mode = "🚨 EMERGÊNCIA" if s['emergency_mode'] else "✅ NORMAL"
        print(f"  Modo:             {mode}")
        print("=" * 45)
