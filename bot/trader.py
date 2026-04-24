"""
bot/trader.py — Execução de ordens via py-clob-client.

Responsabilidades:
    - Conectar ao CLOB da Polymarket via py-clob-client
    - Criar e submeter APENAS limit orders (nunca market orders)
    - Cancelar ordens abertas quando necessário
    - Consultar estado do portfólio e ordens ativas
    - Obter preço atual de um mercado

SEGURANÇA:
    - Todas as credenciais vêm de variáveis de ambiente (.env)
    - Nunca hardcode de chaves ou senhas no código
    - Verificar modo (paper vs live) antes de qualquer operação
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.constants import POLYGON

from strategies.base_strategy import Signal, SignalType

load_dotenv()
logger = logging.getLogger(__name__)


class Trader:
    """
    Executa ordens no CLOB da Polymarket via py-clob-client.

    Usa EXCLUSIVAMENTE limit orders (GTC — Good Till Cancelled).
    Nunca submete market orders, que têm risco de slippage não controlado.

    Args:
        token_id:    ID do token do mercado (YES ou NO outcome).
        dry_run:     Se True, simula ordens sem enviá-las (segurança).
    """

    def __init__(self, token_id: str, dry_run: bool = True):
        self.token_id = token_id
        self.dry_run = dry_run
        self._client: Optional[ClobClient] = None
        self.last_error: str = ""  # Último erro para diagnóstico

        if dry_run:
            logger.info(
                "Trader iniciado em modo DRY RUN — nenhuma ordem será enviada à API."
            )
        else:
            logger.warning(
                "Trader iniciado em modo LIVE — ordens serão enviadas à API REAL."
            )

    def connect(self) -> bool:
        """
        Inicializa o cliente ClobClient com as credenciais do .env.

        Returns:
            True se conexão bem-sucedida, False caso contrário.
        """
        private_key = os.getenv("PRIVATE_KEY", "")
        clob_host = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
        chain_id = int(os.getenv("CHAIN_ID", "137"))
        api_key = os.getenv("API_KEY", "")
        api_secret = os.getenv("API_SECRET", "")
        api_passphrase = os.getenv("API_PASSPHRASE", "")

        # Validação básica das credenciais
        if not private_key or private_key == "0xSUA_CHAVE_PRIVADA_AQUI":
            logger.error(
                "PRIVATE_KEY não configurada no .env. "
                "Configure antes de usar o Trader em modo live."
            )
            return False

        if not api_key or api_key == "SUA_API_KEY_AQUI":
            logger.error("API_KEY não configurada no .env.")
            return False

        proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
        signature_type = int(os.getenv("SIGNATURE_TYPE", "0"))

        try:
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )

            client_kwargs = {
                "host": clob_host,
                "chain_id": chain_id,
                "key": private_key,
                "creds": creds,
                "signature_type": signature_type,
            }
            if proxy_address:
                client_kwargs["funder"] = proxy_address

            self._client = ClobClient(**client_kwargs)

            # Teste de conectividade
            _ = self._client.get_ok()
            logger.info(
                f"ClobClient conectado: host={clob_host} chain_id={chain_id}"
            )

            # Verificar saldo USDC disponível
            try:
                from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
                bal = self._client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                balance_usd = int(bal.get("balance", "0")) / 1e6
                logger.info(f"Saldo USDC disponivel: ${balance_usd:.2f}")
            except Exception as be:
                logger.debug(f"Nao foi possivel checar saldo: {be}")

            return True

        except Exception as e:
            logger.error(f"Erro ao conectar ao ClobClient: {e}")
            self._client = None
            return False

    def get_current_price(self) -> Optional[float]:
        """
        Obtém o preço médio atual do mercado via orderbook.

        Usa o midpoint (média entre best bid e best ask) como proxy
        para o preço de mercado atual.

        Returns:
            Preço atual (0.0 a 1.0) ou None em caso de erro.
        """
        if not self._client:
            logger.error("ClobClient não conectado. Chame connect() primeiro.")
            return None

        try:
            orderbook = self._client.get_order_book(self.token_id)

            if not orderbook:
                logger.warning(f"Orderbook vazio para token_id={self.token_id}")
                return None

            # Extrair best bid e best ask do orderbook
            bids = getattr(orderbook, "bids", []) or []
            asks = getattr(orderbook, "asks", []) or []

            if bids and asks:
                best_bid = float(bids[0].price) if bids else 0.0
                best_ask = float(asks[0].price) if asks else 1.0
                midpoint = (best_bid + best_ask) / 2.0
                return midpoint

            # Fallback: usar apenas bid ou ask disponível
            if bids:
                return float(bids[0].price)
            if asks:
                return float(asks[0].price)

            logger.warning(f"Sem bids ou asks no orderbook para {self.token_id}")
            return None

        except Exception as e:
            logger.error(f"Erro ao obter preço atual: {e}")
            return None

    def place_limit_order(
        self,
        signal: Signal,
        size: float,
        price_override: Optional[float] = None,
        token_id_override: Optional[str] = None,
        max_cost_usd: Optional[float] = None,
    ) -> Optional[dict]:
        """
        Submete uma limit order GTC ao CLOB.

        IMPORTANTE: Apenas limit orders são permitidas por este bot.
        Market orders são proibidas devido ao risco de slippage.

        Args:
            signal:            Sinal de trading (BUY ou SELL).
            size:              Tamanho desejado em USDC (convertido para shares internamente).
            price_override:    Se fornecido, usa este preço em vez do preço do sinal.
            token_id_override: Se fornecido, usa este token_id em vez de self.token_id.
                               Permite reutilizar um único Trader para múltiplos tokens.

        Returns:
            Dicionário com detalhes da ordem (inclui actual_cost_usd) ou None em caso de erro.
        """
        if not signal.is_actionable:
            logger.warning("Sinal HOLD — nenhuma ordem submetida.")
            return None

        price = price_override if price_override is not None else signal.price

        # Validar preço (Polymarket: probabilidades entre 0.01 e 0.99)
        price = max(0.01, min(0.99, price))

        # Token ID efetivo — permite reutilizar Trader para múltiplos mercados
        effective_token_id = token_id_override or self.token_id

        # Mapear sinal para o formato do py-clob-client
        if signal.signal_type == SignalType.BUY:
            side = "BUY"
        else:
            side = "SELL"

        # Converter USDC → shares
        # BUY: mínimo de shares calculado para atingir $1.00 notional mínimo do CLOB.
        #   Fórmula: ceil($1.00 / price)  →  garante mínimo USDC sem inflar o trade.
        #   Exemplos:
        #     price=0.65 → ceil(1/0.65)=2 shares = $1.30  ✓  (antes: 5 shares = $3.25)
        #     price=0.75 → ceil(1/0.75)=2 shares = $1.50  ✓  (antes: 5 shares = $3.75)
        #     price=0.40 → ceil(1/0.40)=3 shares = $1.20  ✓  (antes: 5 shares = $2.00)
        # SELL: usar shares exatos fornecidos (para conseguir fechar posições parciais)
        import math
        if side == "BUY":
            MIN_NOTIONAL_USD = 1.0   # $1.00 mínimo Polymarket CLOB
            min_shares_for_notional = math.ceil(MIN_NOTIONAL_USD / max(price, 0.01))
            shares = max(min_shares_for_notional, size / price)
        else:
            # SELL: respeitar o tamanho pedido (pode ser < 5 para fechar parcial)
            shares = size / price
            if shares < 1:
                self.last_error = f"SELL com shares insuficientes: {shares:.2f}"
                logger.warning(self.last_error)
                return None
        actual_cost_usd = round(shares * price, 4)

        # Hard cap: nunca gastar mais do que max_cost_usd (segunda linha de defesa)
        if max_cost_usd is not None and actual_cost_usd > max_cost_usd:
            capped_shares = max_cost_usd / price
            shares = max(1.0, capped_shares)
            actual_cost_usd = round(shares * price, 4)
            logger.warning(
                f"HARD CAP aplicado: custo {actual_cost_usd:.2f} > max {max_cost_usd:.2f} "
                f"→ cortado para {shares:.2f} shares @ ${actual_cost_usd:.2f}"
            )

        logger.info(
            f"{'[DRY RUN] ' if self.dry_run else ''}Submetendo limit order: "
            f"{side} {shares:.2f} shares @ {price:.4f} = ${actual_cost_usd:.2f} USDC"
            f" | token={effective_token_id}"
        )

        if self.dry_run:
            simulated_order = {
                "order_id": f"dry_run_{side}_{price:.4f}",
                "side": side,
                "price": price,
                "shares": shares,
                "actual_cost_usd": actual_cost_usd,
                "token_id": effective_token_id,
                "status": "simulated",
                "dry_run": True,
            }
            logger.info(f"[DRY RUN] Ordem simulada: {simulated_order}")
            return simulated_order

        # Verificar conexão
        if not self._client:
            logger.error("ClobClient não conectado. Chame connect() primeiro.")
            return None

        def _try_submit(fee_rate_bps: int = 0) -> Optional[dict]:
            """Tenta submeter a ordem com um fee_rate específico."""
            order_args = OrderArgs(
                token_id=effective_token_id,
                price=price,
                size=shares,       # SHARES, não USDC
                side=side,
                fee_rate_bps=fee_rate_bps,
            )
            signed_order = self._client.create_order(order_args)
            response = self._client.post_order(signed_order, OrderType.GTC)

            if response:
                # Extrair orderID — response pode ser dict ou objeto
                order_id = ""
                if isinstance(response, dict):
                    order_id = response.get("orderID", "")
                else:
                    order_id = getattr(response, "orderID", "")

                if not order_id:
                    logger.warning(f"orderID não encontrado na resposta: {response}")

                logger.info(
                    f"Limit order submetida com sucesso: "
                    f"{side} @ {price:.4f} | fee_rate={fee_rate_bps} | order_id={order_id[:20]}..."
                )
                return {
                    "order_id": order_id,
                    "side": side,
                    "price": price,
                    "shares": shares,
                    "actual_cost_usd": actual_cost_usd,
                    "token_id": effective_token_id,
                    "status": "submitted",
                    "raw_response": str(response),
                }
            logger.error(f"Resposta inválida do CLOB: {response}")
            return None

        try:
            return _try_submit(fee_rate_bps=0)

        except Exception as e:
            err_str = str(e)
            self.last_error = err_str

            # Auto-retry: fee rate incorreta — extrai o valor exigido e retenta
            if "invalid fee rate" in err_str and "maker fee" in err_str:
                import re
                match = re.search(r"maker fee[:\s]+(\d+)", err_str)
                if match:
                    required_fee = int(match.group(1))
                    logger.info(
                        f"Fee rate ajustado para {required_fee} bps — retentando ordem..."
                    )
                    try:
                        result = _try_submit(fee_rate_bps=required_fee)
                        if result:
                            self.last_error = ""
                        return result
                    except Exception as e2:
                        self.last_error = str(e2)
                        logger.error(f"Erro ao retentar com fee={required_fee}: {e2}")
                        return None

            logger.error(f"Erro ao submeter limit order: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancela uma ordem aberta pelo ID.

        Args:
            order_id: ID da ordem a ser cancelada.

        Returns:
            True se cancelada com sucesso, False caso contrário.
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Cancelando ordem: {order_id}")
            return True

        if not self._client:
            logger.error("ClobClient não conectado.")
            return False

        try:
            response = self._client.cancel_order(order_id)
            logger.info(f"Ordem {order_id} cancelada: {response}")
            return True
        except Exception as e:
            logger.error(f"Erro ao cancelar ordem {order_id}: {e}")
            return False

    def cancel_all_orders(self) -> bool:
        """
        Cancela todas as ordens abertas do token_id configurado.
        Use em emergências ou ao encerrar o bot.

        Returns:
            True se operação concluída (mesmo com erros parciais).
        """
        if self.dry_run:
            logger.info("[DRY RUN] Cancelando todas as ordens.")
            return True

        if not self._client:
            logger.error("ClobClient não conectado.")
            return False

        try:
            response = self._client.cancel_all()
            logger.info(f"Todas as ordens canceladas: {response}")
            return True
        except Exception as e:
            logger.error(f"Erro ao cancelar todas as ordens: {e}")
            return False

    def get_open_orders(self) -> list:
        """
        Retorna lista de ordens abertas.

        Returns:
            Lista de ordens ou lista vazia em caso de erro.
        """
        if self.dry_run:
            return []

        if not self._client:
            return []

        try:
            orders = self._client.get_orders()
            return orders or []
        except Exception as e:
            logger.error(f"Erro ao obter ordens abertas: {e}")
            return []

    def order_exists(self, order_id: str) -> bool:
        """
        Verifica se uma ordem específica ainda está ativa na exchange.

        Usado para detectar cancelamentos manuais feitos fora do bot:
        se o bot possui um order_id registrado mas a ordem não existe
        mais, ela foi cancelada ou executada externamente.

        Args:
            order_id: ID da ordem a verificar.

        Returns:
            True se a ordem ainda existe e está aberta, False caso contrário.
        """
        if self.dry_run:
            return True  # Em dry_run, assume que a ordem existe

        if not self._client:
            return False

        try:
            order = self._client.get_order(order_id)
            if not order:
                return False
            # Status "LIVE" ou "OPEN" = ordem ainda ativa
            status = ""
            if isinstance(order, dict):
                status = order.get("status", "")
            else:
                status = getattr(order, "status", "")
            return status.upper() in ("LIVE", "OPEN", "UNMATCHED")
        except Exception as e:
            logger.debug(f"Não foi possível verificar ordem {order_id[:20]}...: {e}")
            return False

    def get_balance(self) -> Optional[float]:
        """
        Obtém o saldo USDC disponível na carteira.

        Returns:
            Saldo em USDC ou None em caso de erro.
        """
        if self.dry_run:
            logger.debug("[DRY RUN] Saldo: simulado via RiskManager")
            return None

        if not self._client:
            return None

        try:
            balance = self._client.get_balance()
            return float(balance) if balance is not None else None
        except Exception as e:
            logger.error(f"Erro ao obter saldo: {e}")
            return None
