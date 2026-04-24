"""
bot/manual_override.py — Blacklist de mercados com intervenção manual.

Detecta quando o usuário cancela ou vende uma posição manualmente fora do
bot e bloqueia novas entradas naquele token até o mercado se resolver.

Fluxo:
    1. Bot coloca ordem → rastreia order_id
    2. A cada ciclo, bot verifica se a ordem ainda existe na exchange
    3. Se sumiu sem o bot ter cancelado → intervenção manual detectada
    4. Token é bloqueado com a data de resolução do mercado (via Gamma API)
    5. Quando end_date passa, bloqueio é removido automaticamente
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

OVERRIDE_FILE = "manual_overrides.json"
GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class BlockedToken:
    token_id: str
    reason: str
    blocked_at: str
    market_end_date: Optional[str] = None   # ISO 8601, None = sem expiração automática
    condition_id: Optional[str] = None
    market_question: Optional[str] = None


class ManualOverrideManager:
    """
    Gerencia bloqueios manuais de tokens do Polymarket.

    Um token bloqueado fica assim até a data de resolução do mercado
    (market_end_date). Após essa data, o bloqueio some automaticamente.
    Isso evita que o bot reentrar em posições que você já encerrou
    manualmente por julgá-las perdedoras.

    Args:
        override_file: Caminho do arquivo JSON de persistência.
    """

    def __init__(self, override_file: str = OVERRIDE_FILE):
        self.override_file = override_file
        self._blocks: dict[str, BlockedToken] = {}
        self._load()

    # ----------------------------------------------------------------
    # Persistência
    # ----------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self.override_file):
            return
        try:
            with open(self.override_file) as f:
                data = json.load(f)
            for item in data.get("blocked_tokens", []):
                b = BlockedToken(**item)
                self._blocks[b.token_id] = b
            if self._blocks:
                logger.info(
                    f"ManualOverride: {len(self._blocks)} token(s) bloqueado(s) carregados."
                )
        except Exception as e:
            logger.error(f"Erro ao carregar {self.override_file}: {e}")

    def _save(self) -> None:
        data = {"blocked_tokens": [asdict(b) for b in self._blocks.values()]}
        try:
            with open(self.override_file, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Erro ao salvar {self.override_file}: {e}")

    # ----------------------------------------------------------------
    # Gamma API — busca data de resolução do mercado
    # ----------------------------------------------------------------

    def fetch_market_info(
        self, condition_id: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Busca end_date e question via Gamma API usando condition_id.

        Returns:
            (end_date_iso, question) ou (None, None) se falhar.
        """
        return self._fetch_gamma(params={"condition_id": condition_id})

    def fetch_market_info_by_slug(
        self, market_slug: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Busca end_date e question via Gamma API usando market_slug.

        Usado pelo weather bot, que identifica mercados por slug.

        Returns:
            (end_date_iso, question) ou (None, None) se falhar.
        """
        return self._fetch_gamma(params={"slug": market_slug})

    def _fetch_gamma(self, params: dict) -> Tuple[Optional[str], Optional[str]]:
        """Faz a chamada à Gamma API e extrai end_date + question."""
        try:
            resp = requests.get(f"{GAMMA_API}/markets", params=params, timeout=10)
            resp.raise_for_status()
            payload = resp.json()

            market = payload[0] if isinstance(payload, list) and payload else payload
            if not market:
                return None, None

            end_date = (
                market.get("endDate")
                or market.get("end_date_iso")
                or market.get("endDateIso")
            )
            question = market.get("question", "")
            return end_date, question

        except Exception as e:
            logger.warning(f"Gamma API: não foi possível buscar info ({params}): {e}")
            return None, None

    # ----------------------------------------------------------------
    # Interface pública
    # ----------------------------------------------------------------

    def add_block(
        self,
        token_id: str,
        reason: str,
        condition_id: Optional[str] = None,
    ) -> None:
        """
        Adiciona token à blacklist.

        Se condition_id for fornecido, busca automaticamente a data de
        resolução do mercado para configurar a expiração do bloqueio.

        Args:
            token_id:     Token do mercado a bloquear.
            reason:       Motivo legível (ex: "cancelamento manual detectado").
            condition_id: ID da condição para buscar end_date na Gamma API.
        """
        end_date, question = None, None
        if condition_id:
            end_date, question = self.fetch_market_info(condition_id)

        block = BlockedToken(
            token_id=token_id,
            reason=reason,
            blocked_at=datetime.now(timezone.utc).isoformat(),
            market_end_date=end_date,
            condition_id=condition_id,
            market_question=question,
        )
        self._blocks[token_id] = block
        self._save()

        if end_date:
            expiry_msg = f"expira automaticamente em {end_date}"
        else:
            expiry_msg = "sem expiração automática (remova com remove_block)"

        logger.warning(
            f"🚫 BLOQUEIO ATIVADO | token={token_id[:24]}... | "
            f"motivo={reason} | mercado='{question}' | {expiry_msg}"
        )

    def is_blocked(self, token_id: str) -> Tuple[bool, str]:
        """
        Verifica se um token está bloqueado.

        Expira automaticamente bloqueios cujo mercado já se resolveu
        antes de responder.

        Returns:
            (bloqueado, motivo_legível)
        """
        self._expire_resolved_markets()

        if token_id not in self._blocks:
            return False, ""

        b = self._blocks[token_id]
        msg = (
            f"Bloqueio manual ativo — '{b.market_question or token_id[:20]}' | "
            f"motivo: {b.reason} | bloqueado em {b.blocked_at[:10]}"
        )
        return True, msg

    def remove_block(self, token_id: str) -> bool:
        """Remove bloqueio manualmente antes da data de expiração."""
        if token_id in self._blocks:
            q = self._blocks[token_id].market_question or token_id[:24]
            del self._blocks[token_id]
            self._save()
            logger.info(f"Bloqueio removido manualmente: '{q}'")
            return True
        return False

    def list_blocks(self) -> list[BlockedToken]:
        """Retorna lista de bloqueios ativos (após expirar resolvidos)."""
        self._expire_resolved_markets()
        return list(self._blocks.values())

    def print_blocks(self) -> None:
        """Imprime resumo dos bloqueios ativos no log."""
        blocks = self.list_blocks()
        if not blocks:
            logger.info("ManualOverride: nenhum token bloqueado no momento.")
            return
        logger.info(f"ManualOverride: {len(blocks)} token(s) bloqueado(s):")
        for b in blocks:
            exp = b.market_end_date or "indefinido"
            logger.info(
                f"  • {b.token_id[:24]}... | '{b.market_question}' | "
                f"expira: {exp} | motivo: {b.reason}"
            )

    # ----------------------------------------------------------------
    # Expiração automática
    # ----------------------------------------------------------------

    def _expire_resolved_markets(self) -> None:
        """Remove bloqueios cujo mercado já passou da data de resolução."""
        now = datetime.now(timezone.utc)
        expired = []

        for token_id, block in self._blocks.items():
            if not block.market_end_date:
                continue
            try:
                # Normalizar timezone (a API às vezes devolve sem offset)
                raw = block.market_end_date.replace("Z", "+00:00")
                end_dt = datetime.fromisoformat(raw)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                if now > end_dt:
                    expired.append(token_id)
            except ValueError:
                pass  # Data malformada — manter bloqueio por segurança

        if expired:
            for token_id in expired:
                b = self._blocks.pop(token_id)
                logger.info(
                    f"✅ Bloqueio expirado automaticamente: "
                    f"'{b.market_question or token_id[:24]}' "
                    f"(mercado resolvido em {b.market_end_date})"
                )
            self._save()
