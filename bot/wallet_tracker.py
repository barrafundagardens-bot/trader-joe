"""
bot/wallet_tracker.py — Monitoramento de carteiras top do Polymarket.

Busca as carteiras com melhor histórico de PnL via API pública da Polymarket
e detecta quando abrem novas posições num mercado específico.

Modos de operação:
    live:   Faz chamadas HTTP reais à data-api.polymarket.com (sem autenticação)
    paper:  Modo simulado offline — gera sinais sintéticos baseados em volume

API utilizada (pública, sem chave):
    GET https://data-api.polymarket.com/leaderboard
        → lista de carteiras rankeadas por PnL (últimos 7d ou 30d)

    GET https://data-api.polymarket.com/positions?user={address}&market={condition_id}
        → posições abertas de uma carteira num mercado específico

    GET https://data-api.polymarket.com/activity?user={address}&limit=20
        → últimas transações de uma carteira

Nota de segurança:
    Todas as chamadas são GET públicas — nenhuma chave ou assinatura necessária.
    Rate limit estimado: ~100 req/min por IP (não documentado oficialmente).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

_DATA_API = "https://data-api.polymarket.com"

# Carteiras conhecidas de alto desempenho (seed list para cold start)
# Atualizar periodicamente via fetch_top_wallets()
DEFAULT_TOP_WALLETS: List[str] = [
    "0x9e12c2f5c1e5e1ee97db3ff2da2a866b6cc05b91",
    "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be",
    "0xab5801a7d398351b8be11c439e05c5b3259aec9b",
]


@dataclass
class WalletMove:
    """
    Representa uma movimentação detectada numa carteira monitorada.

    Attributes:
        wallet:       Endereço da carteira (0x...)
        direction:    'BUY' ou 'SELL'
        price:        Preço da posição (0.0 a 1.0)
        size_usdc:    Tamanho da posição em USDC
        market_id:    Condition ID do mercado
        token_id:     Token ID específico (YES ou NO)
        timestamp:    Unix timestamp da transação
        wallet_pnl:   PnL histórico da carteira (para ponderação)
        confidence:   Score de confiança calculado (0.0 a 1.0)
    """
    wallet: str
    direction: str
    price: float
    size_usdc: float
    market_id: str
    token_id: str
    timestamp: int
    wallet_pnl: float = 0.0
    confidence: float = 0.5

    def __repr__(self) -> str:
        return (
            f"WalletMove({self.wallet[:8]}… {self.direction} "
            f"@ {self.price:.4f} size={self.size_usdc:.1f} "
            f"conf={self.confidence:.2f})"
        )


class WalletTracker:
    """
    Monitora carteiras top do Polymarket e detecta novas posições.

    Em modo live, faz chamadas à data-api.polymarket.com.
    Em modo paper (simulation=True), gera movimentos sintéticos
    baseados em volume do candle para permitir testes offline.

    Args:
        market_id:      Condition ID do mercado a monitorar.
        token_id:       Token ID (YES/NO) do mercado.
        simulation:     Se True, usa dados sintéticos (paper trading).
        min_pnl:        PnL mínimo histórico para incluir carteira (USDC).
        min_move_size:  Tamanho mínimo de posição para considerar relevante.
        top_n:          Quantas carteiras do top acompanhar.
        cache_ttl_sec:  Tempo em segundos para cache de posições.
    """

    def __init__(
        self,
        market_id: str = "",
        token_id: str = "",
        simulation: bool = True,
        min_pnl: float = 500.0,
        min_move_size: float = 50.0,
        top_n: int = 10,
        cache_ttl_sec: int = 60,
    ):
        self.market_id = market_id
        self.token_id = token_id
        self.simulation = simulation
        self.min_pnl = min_pnl
        self.min_move_size = min_move_size
        self.top_n = top_n
        self.cache_ttl_sec = cache_ttl_sec

        # Cache interno
        self._wallets: List[dict] = []
        self._wallets_fetched_at: float = 0.0
        self._known_tx_ids: set = set()

        # Sessão HTTP reutilizável com timeout conservador
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "polymarket-trading-bot/1.0"})

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def fetch_top_wallets(self) -> List[dict]:
        """
        Busca lista de carteiras top por PnL via leaderboard público.

        Usa cache interno por `cache_ttl_sec` para evitar rate limiting.

        Returns:
            Lista de dicts com 'address' e 'pnl'. Lista vazia em caso de erro.
        """
        if self.simulation:
            return [{"address": w, "pnl": 1000.0} for w in DEFAULT_TOP_WALLETS]

        now = time.time()
        if self._wallets and now - self._wallets_fetched_at < self.cache_ttl_sec:
            return self._wallets

        try:
            resp = self._session.get(
                f"{_DATA_API}/leaderboard",
                params={"window": "1w", "limit": self.top_n},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()

            wallets = []
            for entry in data:
                address = entry.get("proxy_address") or entry.get("address", "")
                pnl = float(entry.get("pnl", 0))
                if address and pnl >= self.min_pnl:
                    wallets.append({"address": address, "pnl": pnl})

            self._wallets = wallets[: self.top_n]
            self._wallets_fetched_at = now
            logger.info(f"[WalletTracker] {len(self._wallets)} carteiras top carregadas")
            return self._wallets

        except requests.RequestException as e:
            logger.warning(f"[WalletTracker] Erro ao buscar leaderboard: {e}")
            # Fallback para seed list
            return [{"address": w, "pnl": 0.0} for w in DEFAULT_TOP_WALLETS]

    def fetch_recent_activity(self, wallet_address: str) -> List[dict]:
        """
        Busca as últimas transações de uma carteira no mercado monitorado.

        Args:
            wallet_address: Endereço da carteira (0x...).

        Returns:
            Lista de transações recentes. Lista vazia em caso de erro.
        """
        if self.simulation:
            return []  # Em simulação, movimentos são gerados sinteticamente

        try:
            resp = self._session.get(
                f"{_DATA_API}/activity",
                params={
                    "user": wallet_address,
                    "limit": 20,
                },
                timeout=5,
            )
            resp.raise_for_status()
            return resp.json() or []
        except requests.RequestException as e:
            logger.debug(f"[WalletTracker] Erro ao buscar atividade de {wallet_address[:8]}…: {e}")
            return []

    def detect_new_moves(self) -> List[WalletMove]:
        """
        Detecta novas movimentações das carteiras monitoradas.

        Filtra transações já vistas via `_known_tx_ids` para evitar
        duplicações entre ciclos.

        Returns:
            Lista de WalletMove com movimentos novos e relevantes.
        """
        if self.simulation:
            return []  # Paper trading usa generate_synthetic_move()

        wallets = self.fetch_top_wallets()
        moves: List[WalletMove] = []

        for wallet_info in wallets:
            address = wallet_info["address"]
            wallet_pnl = wallet_info.get("pnl", 0.0)

            activity = self.fetch_recent_activity(address)
            for tx in activity:
                tx_id = tx.get("id") or tx.get("transaction_hash", "")
                if not tx_id or tx_id in self._known_tx_ids:
                    continue

                # Filtrar pelo mercado monitorado
                if self.market_id and tx.get("condition_id") != self.market_id:
                    continue

                size = float(tx.get("size", 0))
                if size < self.min_move_size:
                    continue

                direction = "BUY" if tx.get("side", "").upper() in ("BUY", "YES") else "SELL"
                price = float(tx.get("price", 0.5))

                # Confidence baseada em PnL histórico e tamanho do trade
                confidence = min(0.95, 0.5 + (wallet_pnl / 10000) * 0.3 + (size / 1000) * 0.2)

                move = WalletMove(
                    wallet=address,
                    direction=direction,
                    price=price,
                    size_usdc=size,
                    market_id=self.market_id,
                    token_id=self.token_id,
                    timestamp=int(tx.get("timestamp", time.time())),
                    wallet_pnl=wallet_pnl,
                    confidence=confidence,
                )
                moves.append(move)
                self._known_tx_ids.add(tx_id)

                logger.info(f"[WalletTracker] Nova movimentação detectada: {move}")

        return moves

    def generate_synthetic_move(
        self,
        price: float,
        volume: float,
        avg_volume: float,
        price_change_pct: float,
    ) -> Optional[WalletMove]:
        """
        Gera um WalletMove sintético para paper trading.

        Heurística: volume anormalmente alto + price change direcional
        sugere que uma carteira grande entrou no mercado.

        Args:
            price:            Preço atual (close do candle).
            volume:           Volume do candle atual.
            avg_volume:       Volume médio dos últimos N candles.
            price_change_pct: Variação de preço em % (positivo = alta).

        Returns:
            WalletMove sintético ou None se não há sinal.
        """
        if avg_volume <= 0:
            return None

        volume_ratio = volume / avg_volume

        # Threshold: volume >= 1.5x a média com movimento de preço > 1%
        if volume_ratio < 1.5 or abs(price_change_pct) < 0.01:
            return None

        direction = "BUY" if price_change_pct > 0 else "SELL"
        # Confidence proporcional ao volume anormal
        confidence = min(0.90, 0.50 + (volume_ratio - 1.5) * 0.10 + abs(price_change_pct) * 2)

        return WalletMove(
            wallet="synthetic_whale",
            direction=direction,
            price=price,
            size_usdc=volume * price,
            market_id=self.market_id,
            token_id=self.token_id,
            timestamp=int(time.time()),
            wallet_pnl=0.0,
            confidence=confidence,
        )
