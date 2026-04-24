"""
deploy/whale_trader.py — Whale Trader v3

VERSÃO 3 — Reescrito com base no PROJETO_REFINADO.pdf (Manual Operacional v2.1)

Mudanças fundamentais vs v1/v2:
  ✅ Wallet Quality: auditoria real via Polymarket Analytics (win rate posições FECHADAS)
  ✅ 3 whales eliminadas: bcda (PnL negativo), Countryside (<50% WR), majorexploiter (3 trades)
  ✅ Stop-loss por trade: DELETADO (manual: "vai te matar por whipsaw")
  ✅ Portfolio SL: semanal -7% → corta size 50%, mensal -15% → pausa total
  ✅ Whale Hunt: entra 10% abaixo do preço sinalizado pela whale
  ✅ Slippage Check: preço dentro de 8% da entrada da whale (F3 do manual)
  ✅ Liquidez: $500k mínimo (era $100k) — manual enfático
  ✅ Kelly Fraction: máx 15% da banca por trade (F7 do manual)
  ✅ Anti-qualifier: filtro rigoroso para tennis qualifiers

Uso:
    python deploy/whale_trader.py                          # paper trading (padrão)
    python deploy/whale_trader.py --live --confirm         # modo real
    python deploy/whale_trader.py --once                   # roda 1 vez e para
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/whale_trader.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)


# ================================================================
# Config
# ================================================================

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Top 7 whales (enderecos reais do leaderboard, atualizado 2026-04-14)
# ✅ NÚCLEO SPORTS + WEATHER — Blend otimizado para máximo lucro
#
# ATUALIZAÇÃO 2026-04-14: Refeito com base em análise comparativa
# (Sports: +$11.8M, +$5.7M, +$7.3M | Weather: +Millions em diversificação)
#
# Seleção:
#   ☑ NÚCLEO (3): RN1, swisstony, 0x2a2C — mais ativa, volume + consistência
#   ☑ TOP 1 SPORTS: kch123 — melhor rentabilidade sport ($11.8M)
#   ☑ TOP 1 WEATHER: reachingthesky — clima + macro (sinergias)
#   ☑ DUAL (sports + weather): HorizonSplendidView — máxima cobertura
#   ☑ WEATHER SPECIALIST: Neobrother — expertise em temperatura (sinérgico com weather_bot)
#
# Whales removidas em 2026-04-14:
#   ✗ beachboy4, majorexploiter, bcda, Countryside, 432614799197, lo34567Taipe, sovereign2013
#     (não estão no top 5 atualizado de suas categorias)
WHALE_LIST = [
    # ── NÚCLEO: Alta atividade + consistência ───────────────────
    {"rank": 1,  "name": "RN1",                  "address": "0x2005d16a84ceefa912d4e380cd32e7ff827875ea", "profit": 7_300_000, "category": "sports"},
    {"rank": 2,  "name": "swisstony",            "address": "0x204f72f35326db932158cba6adff0b9a1da95e14", "profit": 5_700_000, "category": "sports"},
    {"rank": 3,  "name": "0x2a2C",               "address": "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1", "profit": 1_792_049,  "category": "weather"},
    # ── TOP 1 SPORTS ────────────────────────────────────────────
    {"rank": 4,  "name": "kch123",               "address": "0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee", "profit": 11_800_000, "category": "sports"},
    {"rank": 5,  "name": "HorizonSplendidView",  "address": "0x02227b8f5a9636e895607edd3185ed6ee5598ff7", "profit": 4_016_108,  "category": "dual"},
    # ── EXPANSÃO 2026-04-15: substituem reachingthesky (WR 33%) e Neobrother (addr inválido) ──
    {"rank": 6,  "name": "beachboy4",            "address": "0xc2e7800b5af46e6093872b177b7a5e7f0563be51", "profit": 3_047_075,  "category": "sports"},
    {"rank": 7,  "name": "lo34567Taipe",         "address": "0xf195721ad850377c96cd634457c70cd9e8308057", "profit": 1_140_542,  "category": "sports"},
]

# ✅ Sem whales de risco
RISKY_WHALES = set()


# ================================================================
# Data structures
# ================================================================

@dataclass
class Opportunity:
    """Uma oportunidade de trade identificada pelo consenso de whales."""
    title: str
    slug: str
    whale_count: int
    whales: List[str]
    best_whale_rank: int
    total_value: float
    avg_entry_price: float
    avg_pnl_pct: float
    confidence: str          # "ALTA", "MEDIA", "MODERADA"
    outcome_side: str = "YES"  # "YES" ou "NO" — lado que as whales compraram
    condition_id: str = ""
    token_id: str = ""
    end_date: str = ""
    market_liquidity: float = 0.0  # ✅ v3.4: liquidez real do mercado (para tier de size)
    is_value_scan: bool = False    # Scanner direto (1+ whale + preço favorito)

    @property
    def score(self) -> float:
        """Score composto para ranking de oportunidades."""
        whale_score = self.whale_count * 30
        pnl_score = max(0, self.avg_pnl_pct) * 2
        rank_score = max(0, 21 - self.best_whale_rank) * 3
        value_score = min(20, self.total_value / 5000)
        return whale_score + pnl_score + rank_score + value_score


@dataclass
class ActivePosition:
    """Posicao aberta pelo bot."""
    slug: str
    title: str
    side: str               # "BUY_YES" ou "BUY_NO"
    entry_price: float
    size_usd: float
    entry_time: str
    whale_count: int
    whales: List[str]
    status: str = "open"     # "open", "closed", "expired", "cancelled", "stopped"
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    outcome_side: str = "YES"  # "YES" ou "NO"
    end_date: str = ""
    order_id: str = ""       # ID da ordem no CLOB (para sync de cancelamentos)
    token_id: str = ""       # Token ID do outcome (para SELL stop-loss)
    shares: float = 0.0      # Número de shares compradas


# ================================================================
# Core: Whale Data Fetcher
# ================================================================

class WhaleDataFetcher:
    """Busca e processa dados reais de posicoes de whales."""

    def __init__(self, whale_list: List[dict], max_whales: int = 12):
        self.whale_list = whale_list[:max_whales]

    def fetch_all_positions(
        self, min_value: float = 500.0, delay: float = 0.4
    ) -> Dict[str, list]:
        """
        Busca posicoes ativas de todas as whales.
        Returns: {slug: [position_dicts]}
        """
        by_slug: Dict[str, list] = defaultdict(list)

        for whale in self.whale_list:
            try:
                resp = requests.get(
                    f"{DATA_API}/positions",
                    params={"user": whale["address"]},
                    timeout=15,
                )
                resp.raise_for_status()
                positions = resp.json()

                if not isinstance(positions, list):
                    continue

                active = [
                    p for p in positions
                    if isinstance(p.get("currentValue"), (int, float))
                    and p["currentValue"] >= min_value
                ]

                for pos in active:
                    pos["_whale_name"] = whale["name"]
                    pos["_whale_rank"] = whale["rank"]
                    slug = pos.get("slug", "unknown")
                    by_slug[slug].append(pos)

                time.sleep(delay)

            except Exception as e:
                logger.debug(f"Erro ao buscar {whale['name']}: {e}")
                time.sleep(delay)

        return dict(by_slug)

    def find_consensus(
        self, positions_by_slug: Dict[str, list], min_whales: int = 2
    ) -> List[Opportunity]:
        """Encontra mercados onde 2+ whales estao posicionadas NO MESMO LADO (YES ou NO)."""
        opportunities = []

        # ── DIAGNOSTICO EXPANDIDO ─────────────────────────────────
        # Contadores para entender o funil de consenso
        diag_markets_total = len(positions_by_slug)
        diag_markets_with_2plus_whales_total = 0  # 2+ whales no mercado (qualquer lado)
        diag_markets_hedged = 0  # whales em lados opostos
        diag_markets_consensus = 0  # 2+ whales no mesmo lado
        diag_candidates = []  # lista de mercados com 2+ whales para printar

        for slug, positions in positions_by_slug.items():
            # CRITICO: Agrupar por outcome (YES/NO)
            # Whales podem estar em lados opostos do mesmo mercado!
            by_outcome = defaultdict(list)
            for p in positions:
                outcome = str(p.get("outcome", "Yes")).strip()
                by_outcome[outcome].append(p)

            # Diagnostico: contar whales unicas neste mercado
            all_whales_in_market = set(p["_whale_name"] for p in positions)
            if len(all_whales_in_market) >= 2:
                diag_markets_with_2plus_whales_total += 1
                # Montar breakdown por lado
                sides_info = {}
                for out, out_pos in by_outcome.items():
                    unique_whales = set(p["_whale_name"] for p in out_pos)
                    sides_info[out] = sorted(unique_whales)
                diag_candidates.append((slug[:55], sides_info))

                # Verificar se tem consenso em algum lado
                has_consensus = any(
                    len(set(p["_whale_name"] for p in out_pos)) >= min_whales
                    for out_pos in by_outcome.values()
                )
                if has_consensus:
                    diag_markets_consensus += 1
                else:
                    diag_markets_hedged += 1

            # Processar cada lado separadamente
            for outcome, outcome_positions in by_outcome.items():
                whale_names = list(set(p["_whale_name"] for p in outcome_positions))
                if len(whale_names) < min_whales:
                    continue

                total_value = sum(p.get("currentValue", 0) for p in outcome_positions)
                total_size = sum(p.get("size", 0) for p in outcome_positions)

                # Preco medio ponderado
                weighted = sum(
                    p.get("avgPrice", 0) * p.get("size", 0) for p in outcome_positions
                )
                avg_entry = weighted / total_size if total_size > 0 else 0

                # PnL medio
                pnls = [p.get("percentPnl", 0) for p in outcome_positions]
                avg_pnl = sum(pnls) / len(pnls) if pnls else 0

                best_rank = min(p["_whale_rank"] for p in outcome_positions)

                # Confidence
                n = len(whale_names)
                if n >= 4:
                    confidence = "ALTA"
                elif n >= 3:
                    confidence = "MEDIA"
                else:
                    confidence = "MODERADA"

                # Normalizar outcome para YES/NO
                outcome_upper = outcome.upper()
                if outcome_upper in ("YES", "Y", "TRUE"):
                    outcome_side = "YES"
                elif outcome_upper in ("NO", "N", "FALSE"):
                    outcome_side = "NO"
                else:
                    outcome_side = outcome_upper  # Categorico (ex: "DEMOCRATS")

                # Token/condition info
                condition_id = outcome_positions[0].get("conditionId", "")
                token_id = outcome_positions[0].get("asset", "")

                title = outcome_positions[0].get("title", slug)[:80]
                # Adicionar marcador de outcome no titulo
                title_with_side = f"[{outcome_side}] {title}"

                opportunities.append(Opportunity(
                    title=title_with_side,
                    slug=slug,
                    whale_count=n,
                    whales=whale_names,
                    best_whale_rank=best_rank,
                    total_value=round(total_value, 2),
                    avg_entry_price=round(avg_entry, 4),
                    avg_pnl_pct=round(avg_pnl, 1),
                    confidence=confidence,
                    outcome_side=outcome_side,
                    condition_id=condition_id,
                    token_id=token_id,
                ))

        # Ordenar por score
        opportunities.sort(key=lambda o: -o.score)

        # ✅ LADO DOMINANTE: Se mesma slug aparece em YES e NO,
        # manter apenas o lado com mais dinheiro (whale seguindo o maior lado)
        dominant: Dict[str, "Opportunity"] = {}
        for opp in opportunities:
            existing = dominant.get(opp.slug)
            if existing is None or opp.total_value > existing.total_value:
                dominant[opp.slug] = opp

        # ── PRINT DIAGNOSTICO EXPANDIDO ──────────────────────────
        print(f"\n  🔍 FUNIL DE CONSENSO:")
        print(f"     Mercados totais:              {diag_markets_total}")
        print(f"     Mercados com 2+ whales:       {diag_markets_with_2plus_whales_total}")
        print(f"     ├─ Com consenso (mesmo lado): {diag_markets_consensus}")
        print(f"     └─ Em hedge (lados opostos):  {diag_markets_hedged}")
        print(f"     Oportunidades geradas:        {len(dominant)}")

        # ✅ FIX v3.4: Sempre printar breakdown dos mercados hedged com VALORES
        # Antes: mostrava só nomes de whales, sem dinheiro em cada lado
        # Agora: mostra $valor por lado para revelar dominância real
        if diag_candidates:
            # Montar valores por lado a partir das posições originais
            candidate_values: dict = {}
            for slug, positions in positions_by_slug.items():
                by_outcome_val: Dict[str, float] = defaultdict(float)
                by_outcome_whales: Dict[str, set] = defaultdict(set)
                for p in positions:
                    outcome = str(p.get("outcome", "Yes")).strip()
                    by_outcome_val[outcome] += float(p.get("currentValue", 0) or 0)
                    by_outcome_whales[outcome].add(p["_whale_name"])
                candidate_values[slug] = {
                    "val": dict(by_outcome_val),
                    "whales": {k: sorted(v) for k, v in by_outcome_whales.items()},
                }

            label = "HEDGED (sem consenso)" if len(dominant) == 0 else "TODOS COM 2+ WHALES"
            print(f"\n  🔎 {label} — breakdown por lado com valores:")
            for slug, sides in diag_candidates[:15]:
                vals = candidate_values.get(slug, {}).get("val", {})
                whl  = candidate_values.get(slug, {}).get("whales", sides)
                total = sum(vals.values()) or 1
                parts = []
                for side, wnames in sorted(sides.items()):
                    v = vals.get(side, 0)
                    pct = v / total * 100
                    parts.append(f"[{side}] ${v:,.0f} ({pct:.0f}%) {', '.join(wnames)}")
                dominant_side = max(vals, key=vals.get) if vals else "?"
                dominant_val  = max(vals.values()) if vals else 0
                minority_val  = min(vals.values()) if len(vals) > 1 else 0
                ratio = minority_val / total * 100 if total > 0 else 0
                flag = "✅ DOMINÂNCIA" if ratio < 35 and len(vals) > 1 else ("⚠️  SPLIT" if len(vals) > 1 else "")
                print(f"     • {slug[:52]}  {flag}")
                for part in parts:
                    print(f"        {part}")
                if len(vals) > 1:
                    print(f"        → Dominante: {dominant_side} (${dominant_val:,.0f}) | Lado menor: {ratio:.0f}% do total")

        return list(dominant.values())


# ================================================================
# Core: Whale Trader Bot
# ================================================================

class WhaleTrader:
    """
    Bot automatizado de whale following.

    Ciclo:
        1. Buscar posicoes das whales
        2. Encontrar consenso
        3. Filtrar oportunidades ja em carteira
        4. Executar trades (dry-run ou live)
        5. Monitorar posicoes
    """

    def __init__(
        self,
        capital: float = 10.0,
        trade_size: float = 1.20,   # ✅ REDUZIDO: $1.20 por trade (era $2.00)
        max_positions: int = 7,     # ✅ AMPLIADO: 7 slots (era 5) — diversificar mais
        min_whales: int = 3,        # ✅ AUMENTADO: 3 whales mínimo (era 2) — consenso forte
        min_whale_pnl: float = 7.5, # ✅ AUMENTADO: ≥+7.5% (era 4.5%) — whales confiantes
        max_price: float = 0.80,
        dry_run: bool = True,
        whales_to_scan: int = 12,  # ✅ BOT SPORTS: 12 whales para maximizar overlap/consenso
        auto_sync: bool = True,
        stop_loss: float = 0.30,   # ✅ Primeiro stop: 30%
        max_cost: float = 1.50,    # ✅ v3.4: máx $1.50/trade — compatível com liq tier de 50% ($0.75 mín)
        value_scan: bool = True,   # Scanner direto: MLB/NBA com 1+ whale + preço 0.55-0.82
    ):
        self.capital = capital
        self.capital_initial = capital  # ✅ NOVO: guardar inicial para capital limit
        self.trade_size = trade_size
        self.max_positions = max_positions
        self.min_whales = min_whales
        self.min_whale_pnl = min_whale_pnl
        self.max_price = max_price
        self.dry_run = dry_run
        self.auto_sync = auto_sync
        self.stop_loss = stop_loss       # Perda máxima antes de sair (0.20 = 20%)
        self.max_cost = max_cost         # Custo máximo real por trade em USDC
        self.value_scan = value_scan     # Scanner direto MLB/NBA
        self._running = False

        # ═══════════════════════════════════════════════════════════
        # V3: AUDITORIA DE QUALIDADE DAS WHALES (Polymarket Analytics)
        # ═══════════════════════════════════════════════════════════
        # O manual exige: "win rate posições fechadas >68% nos últimos
        # 90 dias + Polyman score ≥80". Como Polyman exige auth,
        # usamos Polymarket Analytics (API pública, gratuita) com
        # scoring equivalente. Whales com PnL negativo, WR<50%, ou
        # zombie ratio alto são ELIMINADAS antes do bot rodar.
        # ═══════════════════════════════════════════════════════════
        try:
            from bot.wallet_scorer import audit_whale_list, filter_quality_whales
            self._wallet_scores = audit_whale_list(WHALE_LIST)
            quality_whales = filter_quality_whales(WHALE_LIST, self._wallet_scores)
            blocked = len(WHALE_LIST) - len(quality_whales)
            if blocked > 0:
                logger.info(f"V3 AUDIT: {blocked} whales BLOQUEADAS por qualidade insuficiente")
            self.fetcher = WhaleDataFetcher(quality_whales, max_whales=min(whales_to_scan, len(quality_whales)))
        except Exception as e:
            logger.warning(f"Auditoria de wallets falhou ({e}). Usando lista completa como fallback.")
            self._wallet_scores = {}
            self.fetcher = WhaleDataFetcher(WHALE_LIST, max_whales=whales_to_scan)

        self.positions: List[ActivePosition] = []
        self.trade_log: List[dict] = []
        self.cycle_count = 0
        self._bad_tokens: set = set()   # ⚠️ DESATIVADO: cache de tokens sem orderbook (live refresh ativo)
        self._blind_positions: set = set()  # Posições sem preço (slug não encontrado)
        self._live_trader = None        # Trader compartilhado — criado uma vez por sessão
        self._live_trader_connected: bool = False
        self._weekly_sl_triggered = False   # V3: Portfolio SL semanal
        self._monthly_sl_triggered = False  # V3: Portfolio SL mensal

        # Carregar posicoes salvas
        self._load_state()

        # Shutdown handler
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    # ----------------------------------------------------------------
    # State management
    # ----------------------------------------------------------------

    def _state_path(self) -> str:
        os.makedirs("data", exist_ok=True)
        return "data/whale_trader_state.json"

    def _load_state(self):
        """Carrega estado anterior se existir."""
        path = self._state_path()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    state = json.load(f)
                # Filtrar campos conhecidos e adicionar defaults para novos campos
                known_fields = {
                    "slug", "title", "side", "entry_price", "size_usd",
                    "entry_time", "whale_count", "whales", "status",
                    "exit_price", "pnl", "outcome_side", "order_id",
                    "token_id", "shares",
                }
                positions = []
                for p in state.get("positions", []):
                    filtered = {k: v for k, v in p.items() if k in known_fields}
                    # Default para posicoes antigas que nao tem outcome_side
                    if "outcome_side" not in filtered:
                        filtered["outcome_side"] = "YES"
                    positions.append(ActivePosition(**filtered))
                self.positions = positions
                self.trade_log = state.get("trade_log", [])
                self.capital = state.get("capital", self.capital)
                # ✅ FIX: Restaurar capital_initial do state para que o SL mensal
                # calcule PnL corretamente. Sem isso, --capital 100 com state=$10
                # gera total_pnl_pct = (10-100)/100 = -90% → Monthly SL dispara imediatamente.
                self.capital_initial = state.get("capital_initial", self.capital)
                # ⚠️ CACHE DESATIVADO: bad_tokens NÃO é mais restaurado do state.
                # O bot agora consulta o orderbook do CLOB em TEMPO REAL a cada
                # ciclo. Tokens que antes estavam bloqueados agora terão nova
                # chance a cada scan (força refresh live do CLOB).
                self._bad_tokens = set()
                logger.info(
                    f"Estado carregado: {len(self.positions)} posicoes, "
                    f"${self.capital:.2f} capital "
                    f"(cache de bad_tokens DESATIVADO — refresh live do CLOB)"
                )
            except Exception as e:
                logger.warning(f"Erro ao carregar estado: {e}")

    def _save_state(self):
        """Salva estado atual."""
        path = self._state_path()
        state = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "capital": round(self.capital, 2),
            "capital_initial": round(self.capital_initial, 2),  # ✅ FIX: salvar para SL mensal correto
            "positions": [
                {
                    "slug": p.slug, "title": p.title, "side": p.side,
                    "entry_price": p.entry_price, "size_usd": p.size_usd,
                    "entry_time": p.entry_time, "whale_count": p.whale_count,
                    "whales": p.whales, "status": p.status,
                    "exit_price": p.exit_price, "pnl": p.pnl,
                    "outcome_side": p.outcome_side, "order_id": p.order_id,
                    "token_id": p.token_id, "shares": p.shares,
                }
                for p in self.positions
            ],
            "trade_log": self.trade_log[-100:],  # Ultimos 100 trades
            # ⚠️ bad_tokens NÃO é mais persistido — refresh live do CLOB a cada ciclo
            "bad_tokens": [],
        }
        with open(path, "w") as f:
            json.dump(state, f, indent=2)

    # ----------------------------------------------------------------
    # Display
    # ----------------------------------------------------------------

    def _display_header(self):
        mode = "DRY RUN (simulado)" if self.dry_run else "LIVE (capital real)"
        mode_icon = "🧪" if self.dry_run else "🔴"

        open_pos = [p for p in self.positions if p.status == "open"]
        invested = sum(p.size_usd for p in open_pos)

        # Calcular PnL atual via _get_market_price (token_id → slug)
        total_open_pnl = 0.0
        for p in open_pos:
            current_price, _ = self._get_market_price(p)
            if current_price is not None and p.entry_price > 0:
                pnl = (current_price - p.entry_price) * (p.size_usd / p.entry_price)
                total_open_pnl += pnl

        total_portfolio = self.capital + invested + total_open_pnl
        wins, losses, win_rate = self._calculate_stats()

        print()
        print("=" * 65)
        print(f"  {mode_icon} WHALE TRADER — {mode}")
        print("=" * 65)
        print(f"  💵 Disponível:  ${self.capital:.2f}  (sync Polymarket)")
        print(f"  📊 Investido:   ${invested:.2f}  ({len(open_pos)} posições abertas)")
        if total_open_pnl != 0:
            pnl_icon = "🟢" if total_open_pnl > 0 else "🔴"
            print(f"  {pnl_icon} PnL (aberto): {total_open_pnl:+.2f}")
        print(f"  💼 Total:       ${total_portfolio:.2f}")
        win_rate_str = f"{win_rate:.0f}%" if wins + losses > 0 else "—"
        print(f"  📈 Win Rate:    {wins}W / {losses}L ({win_rate_str})")
        print(f"  ⚙️  Trade size:  ${self.trade_size:.2f} | Max cost: ${self.max_cost:.2f} | Slots: {len(open_pos)}/{self.max_positions}")
        print(f"  🛑 Stop-loss:   {self.stop_loss:.0%} | Ciclo #{self.cycle_count} | {datetime.now().strftime('%H:%M:%S')}")
        print("=" * 65)

    def _display_opportunities(self, opportunities: List[Opportunity]):
        if not opportunities:
            print("\n  Nenhuma oportunidade encontrada.\n")
            return

        print(f"\n  OPORTUNIDADES ({len(opportunities)} encontradas):\n")

        for i, opp in enumerate(opportunities[:10], 1):
            pnl_icon = "🟢" if opp.avg_pnl_pct > 0 else "🔴"
            conf_icon = {"ALTA": "🟢", "MEDIA": "🟡", "MODERADA": "🟠"}[opp.confidence]

            # Verificar se ja temos posicao
            already_in = any(
                p.slug == opp.slug and p.status in ("open", "pending")
                for p in self.positions
            )
            in_marker = " [JA POSICIONADO]" if already_in else ""

            print(f"  {i:>2}. {conf_icon} {opp.title}{in_marker}")
            print(f"      {opp.whale_count} whales | "
                  f"${opp.total_value:,.0f} investido | "
                  f"entrada {opp.avg_entry_price:.3f} | "
                  f"{pnl_icon} {opp.avg_pnl_pct:+.1f}% PnL")
            print(f"      Whales: {', '.join(opp.whales[:4])}")
            print(f"      Score: {opp.score:.0f} | Conf: {opp.confidence}")
            print()

    def _calculate_stats(self) -> tuple:
        """Calcula wins, losses e win rate % das posicoes realmente concluídas."""
        # Apenas posições fechadas (resolved) ou stopped (stop-loss) contam
        # Canceladas/unfilled NÃO contam (nunca foram trades reais)
        real_trades = [
            p for p in self.positions
            if p.status in ("closed", "stopped")
        ]
        wins = sum(1 for p in real_trades if p.pnl and p.pnl > 0)
        losses = sum(1 for p in real_trades if p.pnl and p.pnl <= 0)
        total = wins + losses
        win_rate = (wins / total * 100) if total > 0 else 0
        return wins, losses, win_rate

    def _display_positions(self):
        open_positions = [p for p in self.positions if p.status == "open"]
        if not open_positions:
            print("  Nenhuma posicao aberta.\n")
            return

        print(f"\n  POSICOES ABERTAS ({len(open_positions)}):\n")

        for p in open_positions:
            current_price, is_resolved = self._get_market_price(p)

            pnl_str = "sem preço"
            if current_price is not None:
                if p.entry_price > 0:
                    loss_pct = (p.entry_price - current_price) / p.entry_price
                    pnl_usd = (current_price - p.entry_price) * (p.size_usd / p.entry_price)
                    icon = "🟢" if pnl_usd >= 0 else "🔴"
                    pnl_str = f"{icon} ${pnl_usd:+.2f} ({loss_pct:+.1%})"
                    if loss_pct >= self.stop_loss:
                        pnl_str += " ⚠️ STOP"
                    if is_resolved:
                        pnl_str += " 🏁 RESOLVIDO"

            print(f"  • {p.title[:55]}")
            price_str = f"{p.entry_price:.3f}"
            if current_price is not None:
                price_str += f" → {current_price:.3f}"
            print(f"    {price_str} | ${p.size_usd:.2f} | PnL: {pnl_str}")
            print()

    def _mark_trade_result(self, slug: str, result: str) -> None:
        """Grava 'win'/'loss' no entry mais recente do trade_log para este slug."""
        for entry in reversed(self.trade_log):
            if entry.get("slug") == slug and entry.get("result") is None:
                entry["result"] = result
                return

    def _display_trade_log(self):
        recent = self.trade_log[-5:]
        if not recent:
            return

        print(f"  ULTIMOS TRADES:\n")
        for t in recent:
            result = t.get("result")  # "win", "loss" ou None (aberto)
            if result == "win":
                icon = "✅"
            elif result == "loss":
                icon = "❌"
            else:
                icon = "⏳"

            print(f"  {icon} {t.get('time', '?')[:19]} | "
                  f"{t.get('action', '?')} | "
                  f"{t.get('title', '?')[:40]} | "
                  f"${t.get('size', 0):.2f}")
        print()

    # ----------------------------------------------------------------
    # Trading logic
    # ----------------------------------------------------------------

    def _verify_portfolio_positions(self) -> None:
        """
        ✅ SYNC REAL: Cruza posições do bot com o portfólio real via Data API.

        Fluxo por posição:
          "pending": ordem foi enviada, ainda não confirmada
            → se token_id encontrado no Data API → promove para "open" com shares reais
            → se order_id ausente do CLOB E não no Data API → marca "cancelled"
          "open" com shares=0 (herdado de estado antigo com bug):
            → aplica a mesma lógica do "pending"
          "open" com shares>0:
            → já confirmada, não retoca

        Usa endpoint:
          GET https://data-api.polymarket.com/positions?user=PROXY_ADDRESS
        """
        if self.dry_run:
            return

        proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
        if not proxy_address:
            logger.debug("POLYMARKET_PROXY_ADDRESS não configurado — pulando verify portfolio")
            return

        # Buscar posições reais
        real_positions_by_token: Dict[str, dict] = {}
        try:
            resp = requests.get(
                f"{DATA_API}/positions",
                params={"user": proxy_address},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    for p in data:
                        token = str(p.get("asset") or p.get("tokenId") or "")
                        if token:
                            real_positions_by_token[token] = p
                    logger.info(
                        f"Data API: {len(real_positions_by_token)} posições reais no portfólio"
                    )
            else:
                logger.warning(f"Data API positions retornou {resp.status_code}")
                return
        except Exception as e:
            logger.warning(f"Erro ao verificar portfólio via Data API: {e}")
            return

        # Verificar cada posição pending ou open-sem-shares
        for pos in self.positions:
            needs_check = (
                pos.status == "pending"
                or (pos.status == "open" and pos.shares == 0)
            )
            if not needs_check:
                continue

            # Checar se token está no portfólio real
            if pos.token_id and pos.token_id in real_positions_by_token:
                real = real_positions_by_token[pos.token_id]
                real_shares = float(real.get("size") or real.get("shares") or 0)
                pos.status = "open"
                pos.shares = real_shares if real_shares > 0 else 5.0
                logger.info(
                    f"✅ CONFIRMADO no portfólio: {pos.slug[:40]} | "
                    f"{pos.shares:.2f} shares reais | promovido para 'open'"
                )
                print(f"  ✅ FILL CONFIRMADO: {pos.title[:50]} ({pos.shares:.1f} shares)")
            else:
                # Não está no portfólio real ainda.
                # GRACE PERIOD: ordens limite abaixo do mercado (whale hunt -10%)
                # podem demorar vários minutos para preencher. Só cancelar após
                # 15 minutos sem fill — evita o loop de múltiplas ordens no
                # mesmo mercado por cancelamento prematuro.
                grace_minutes = 15
                still_in_grace = False
                if pos.entry_time:
                    try:
                        entry_dt = datetime.fromisoformat(
                            pos.entry_time.replace("Z", "+00:00")
                        )
                        age_minutes = (
                            datetime.now(timezone.utc) - entry_dt
                        ).total_seconds() / 60
                        if age_minutes < grace_minutes:
                            still_in_grace = True
                            logger.info(
                                f"⏳ {pos.slug[:40]}: aguardando fill "
                                f"({age_minutes:.1f}min < {grace_minutes}min) — mantendo 'pending'"
                            )
                    except Exception:
                        pass

                if still_in_grace:
                    continue  # Não cancela ainda — ordem pode estar aguardando fill

                was_pending = pos.status == "pending"
                pos.status = "cancelled"
                pos.pnl = 0.0
                logger.info(
                    f"🔄 NÃO encontrado no portfólio: {pos.slug[:40]} → "
                    f"marcado como 'cancelled' (era '{('pending' if was_pending else 'open-0shares')}')"
                )
                print(
                    f"  🔄 CANCELADO (sem fill): {pos.title[:50]} "
                    f"— ordem não preenchida após {grace_minutes}min"
                )

    def _reconcile(self) -> None:
        """
        FULL RECONCILIATION — sincroniza o bot com a realidade do Polymarket.

        Executa a cada ciclo:
        1. Verifica portfólio real via Data API (confirma fills, detecta fantasmas)
        2. Lê saldo USDC real (fonte de verdade)
        3. Lista TODAS ordens abertas no CLOB (com detalhes)
        4. Compara com posições do bot:
           - Ordem no CLOB mas não no bot → aviso (ordem externa)
           - Posição no bot mas não no CLOB → filled ou cancelada
           - Posição no bot E no CLOB → verifica fill parcial
        4. Capital = saldo real do Polymarket (sempre)
        """
        if self.dry_run or not self.auto_sync:
            return

        trader = self._get_trader()
        if trader is None:
            return

        # ── 0. Verificar portfólio real via Data API ──────────────────
        # Isso promove "pending" → "open" (fill confirmado) OU → "cancelled"
        # Também corrige posições "open" com shares=0 (bug do fallback antigo)
        self._verify_portfolio_positions()

        # ── 1. Saldo USDC real ────────────────────────────────────────
        actual_usdc = None
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            bal = trader._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            if bal:
                actual_usdc = int(bal.get("balance", "0")) / 1e6
        except Exception as e:
            logger.debug(f"get_balance_allowance falhou: {e}")

        if actual_usdc is None:
            try:
                raw = trader._client.get_balance()
                actual_usdc = float(raw) if raw is not None else None
            except Exception:
                pass

        if actual_usdc is None:
            logger.warning("Não foi possível ler saldo USDC do Polymarket")
            return

        # ── 2. Ordens abertas no CLOB (com detalhes completos) ────────
        clob_orders_raw = []
        try:
            clob_orders_raw = trader.get_open_orders() or []
        except Exception as e:
            logger.debug(f"Erro ao obter ordens CLOB: {e}")

        # Mapear order_id → detalhes completos
        clob_by_id: Dict[str, dict] = {}
        for o in clob_orders_raw:
            if isinstance(o, dict):
                oid = o.get("id") or o.get("orderID") or o.get("order_id") or ""
            else:
                oid = getattr(o, "id", "") or getattr(o, "orderID", "") or ""
                o = {"id": oid}  # Normalizar para dict
                for attr in ("price", "size", "side", "asset_id",
                             "size_matched", "original_size", "status"):
                    val = getattr(o, attr, None)
                    if val is not None:
                        o[attr] = val
            if oid:
                clob_by_id[str(oid)] = o if isinstance(o, dict) else {"id": oid}

        # ── 3. Reconciliar posições do bot ────────────────────────────
        reconciled = 0
        partially_filled = 0

        for pos in self.positions:
            if pos.status != "open" or not pos.order_id:
                continue

            if pos.order_id in clob_by_id:
                # ✅ Ordem ainda aberta no CLOB — verificar fill parcial
                clob_order = clob_by_id[pos.order_id]
                size_matched = 0.0
                original_size = 0.0
                try:
                    size_matched = float(
                        clob_order.get("size_matched", 0) or 0
                    )
                    original_size = float(
                        clob_order.get("original_size", 0)
                        or clob_order.get("size", 0) or 0
                    )
                except (ValueError, TypeError):
                    pass

                if size_matched > 0 and original_size > 0:
                    fill_pct = size_matched / original_size * 100
                    partially_filled += 1
                    logger.info(
                        f"📊 {pos.slug}: {fill_pct:.0f}% preenchida "
                        f"({size_matched:.1f}/{original_size:.1f} shares)"
                    )
            else:
                # Ordem não está em CLOB open orders.
                # Distinguir filled vs cancelada pelo número de shares:
                #   - pos.shares > 0 → ordem foi FILLED (shares recebidas) → manter "open"
                #   - pos.shares == 0 → ordem CANCELADA antes de preencher → marcar cancelled
                # Esta distinção é confiável porque shares só são setadas após fill confirmado.
                if pos.shares and pos.shares > 0:
                    # Filled: tem shares reais → stop-loss e exits continuam monitorando
                    logger.info(
                        f"📋 {pos.slug}: ordem filled ({pos.shares:.1f} shares) → mantendo 'open'"
                    )
                else:
                    # Cancelada (manual ou automática): sem shares, capital devolvido
                    pos.status = "cancelled"
                    pos.pnl = 0.0
                    reconciled += 1
                    print(f"  🔄 Cancelamento detectado: {pos.title[:50]}")
                    logger.info(
                        f"🔄 {pos.slug}: ordem cancelada (0 shares) → marcando 'cancelled'"
                    )

        # ── 4. Detectar ordens no CLOB que o bot NÃO conhece ─────────
        bot_order_ids = {p.order_id for p in self.positions if p.order_id}
        unknown_orders = [
            oid for oid in clob_by_id if oid not in bot_order_ids
        ]

        # ── 5. Capital = saldo real (SEMPRE) ──────────────────────────
        old_capital = self.capital
        self.capital = actual_usdc
        diff = actual_usdc - old_capital

        # ── 6. Exibir resumo ─────────────────────────────────────────
        open_in_clob = len(clob_by_id)
        open_in_bot = len([p for p in self.positions if p.status == "open"])

        has_changes = abs(diff) > 0.10 or reconciled > 0 or unknown_orders

        if has_changes:
            print()

        if abs(diff) > 0.10:
            direction = f"+${diff:.2f}" if diff > 0 else f"${diff:.2f}"
            print(f"  💰 SYNC: Polymarket ${actual_usdc:.2f} "
                  f"(era ${old_capital:.2f}, {direction})")
            logger.info(f"SYNC: ${old_capital:.2f} → ${actual_usdc:.2f}")

        if reconciled > 0:
            print(f"  🔄 {reconciled} posição(ões) reconciliada(s) (removida do CLOB)")

        if unknown_orders:
            print(f"  ⚠️  {len(unknown_orders)} ordem(ns) no CLOB não rastreada(s) pelo bot")
            for oid in unknown_orders[:3]:
                o = clob_by_id[oid]
                price = o.get("price", "?")
                side = o.get("side", "?")
                logger.warning(f"   Ordem externa: {oid[:20]}... | {side} @ {price}")

        if partially_filled > 0:
            print(f"  📊 {partially_filled} ordem(ns) parcialmente preenchida(s)")

        print(f"  📊 Estado: CLOB {open_in_clob} ordens | "
              f"Bot {open_in_bot} posições | "
              f"Saldo ${actual_usdc:.2f}")

        if has_changes:
            print()

        self._save_state()

    def _analyze_whale_dominance(self, opp: Opportunity) -> dict:
        """
        ✅ FIX V3: Analisa dominância das whales no mercado.

        Estratégia: ao invés de REJEITAR trades com hedging, ENTRAMOS no lado
        dominante — onde as whales têm MAIS dinheiro. Essa é a convicção real.

        Retorna um dict com:
            action: "accept" | "reject" | "flip"
                - "accept": whale claramente no nosso lado, prossegue normal
                - "reject": hedging REAL (ratio > 10%), convicção dividida, pula
                - "flip":   whale dominante no lado OPOSTO, vamos entrar lá
            dominant_outcome: str — nome do outcome dominante agregado
            reason: str — descrição para log
            per_whale: dict — detalhamento por whale (para debug)

        Lógica:
            1. Para cada whale, soma valores por outcome no market
            2. Agrega TODAS as whales → outcome dominante global
            3. Se hedging real (min_ratio > 10%): REJECT
            4. Se dominante != target: FLIP para dominante
            5. Caso contrário: ACCEPT
        """
        result = {
            "action": "accept",
            "dominant_outcome": opp.outcome_side,
            "reason": "sem dados de dominância",
            "per_whale": {},
        }

        if not opp.condition_id:
            return result

        # ✅ FIX v3.4: threshold de 10% era absurdamente restritivo.
        # Ex: $50k YES + $5.6k NO = ratio 10.1% → REJECT (errado).
        # Com 35%: só rejeita se o lado menor tem >35% do total, ou seja,
        # a divisão é quase 65/35 — hedging genuíno e sem sinal claro.
        HEDGING_RATIO_THRESHOLD = 0.35  # 35% = hedge real (era 10%)
        target_outcome = opp.outcome_side.upper()

        try:
            name_to_addr = {
                w["name"]: w["address"].lower()
                for w in WHALE_LIST
            }

            # Agregação global: soma de currentValue por outcome, entre TODAS as whales
            aggregated: Dict[str, float] = {}
            per_whale_info: Dict[str, Dict[str, float]] = {}

            for whale_name in opp.whales:
                whale_addr = name_to_addr.get(whale_name)
                if not whale_addr:
                    continue

                try:
                    resp = requests.get(
                        f"{DATA_API}/positions",
                        params={
                            "user": whale_addr,
                            "market": opp.condition_id,
                        },
                        timeout=5,
                    )
                    if resp.status_code != 200:
                        continue

                    positions = resp.json()
                    if not isinstance(positions, list) or not positions:
                        continue

                    outcome_values: Dict[str, float] = {}
                    for p in positions:
                        outcome = str(p.get("outcome", "")).strip().upper()
                        value = float(p.get("currentValue", 0) or 0)
                        if outcome and value > 0:
                            outcome_values[outcome] = outcome_values.get(outcome, 0) + value
                            aggregated[outcome] = aggregated.get(outcome, 0) + value

                    if outcome_values:
                        per_whale_info[whale_name] = outcome_values

                except Exception as e:
                    logger.debug(f"Erro checando whale {whale_name}: {e}")
                    continue

            result["per_whale"] = per_whale_info

            if not aggregated:
                result["reason"] = "sem posições de whales encontradas"
                return result

            # Ranking global de outcomes
            sorted_outcomes = sorted(aggregated.items(), key=lambda x: -x[1])
            dominant_outcome, dominant_value = sorted_outcomes[0]
            total_value = sum(aggregated.values())

            if len(sorted_outcomes) >= 2:
                second_outcome, second_value = sorted_outcomes[1]
                min_ratio = second_value / total_value if total_value > 0 else 0
            else:
                second_outcome, second_value, min_ratio = None, 0.0, 0.0

            result["dominant_outcome"] = dominant_outcome

            # CASO 1: Hedging REAL global — convicção dividida, não tem lado certo
            if min_ratio > HEDGING_RATIO_THRESHOLD:
                result["action"] = "reject"
                result["reason"] = (
                    f"HEDGING REAL: {dominant_outcome}=${dominant_value:.0f} vs "
                    f"{second_outcome}=${second_value:.0f} (ratio {min_ratio:.0%})"
                )
                return result

            # CASO 2: Determinar se o dominante é OPOSTO ao target
            # Match fuzzy: target pode ser "YES"/"NO" ou nome do time/categoria
            is_opposite = False
            if target_outcome in ("YES", "NO") and dominant_outcome in ("YES", "NO"):
                is_opposite = target_outcome != dominant_outcome
            elif target_outcome and dominant_outcome:
                # Se nem sub-string bate, considera diferente
                if (target_outcome not in dominant_outcome
                        and dominant_outcome not in target_outcome):
                    is_opposite = True

            if is_opposite and dominant_value > 100:
                result["action"] = "flip"
                result["reason"] = (
                    f"FLIP para lado dominante: whales têm ${dominant_value:.0f} "
                    f"em {dominant_outcome} (target era {target_outcome})"
                )
                return result

            # CASO 3: Dominante == target → aceita
            result["action"] = "accept"
            result["reason"] = (
                f"whales convictas em {dominant_outcome} "
                f"(${dominant_value:.0f}, residual {min_ratio:.0%})"
            )
            return result

        except Exception as e:
            logger.debug(f"Erro ao analisar dominância {opp.slug}: {e}")
            result["reason"] = f"erro: {e}"
            return result

    def _fetch_market_tokens(self, opp: Opportunity) -> Optional[dict]:
        """
        Busca tokens e outcomes do mercado via Gamma API.

        Returns:
            dict com 'outcomes' (list[str]) e 'token_ids' (list[str]), ou None.
        """
        import json as _json
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={"slug": opp.slug},
                timeout=5,
            )
            resp.raise_for_status()
            markets = resp.json()
            if not markets:
                return None

            m = markets[0]
            outcomes_raw = m.get("outcomes", [])
            tokens_raw = m.get("clobTokenIds", [])

            if isinstance(outcomes_raw, str):
                try:
                    outcomes_raw = _json.loads(outcomes_raw)
                except Exception:
                    outcomes_raw = []
            if isinstance(tokens_raw, str):
                try:
                    tokens_raw = _json.loads(tokens_raw)
                except Exception:
                    tokens_raw = []

            if not outcomes_raw or not tokens_raw:
                return None

            return {
                "outcomes": [str(o).strip() for o in outcomes_raw],
                "token_ids": [str(t) for t in tokens_raw],
                "prices": m.get("outcomePrices", []),
            }
        except Exception as e:
            logger.debug(f"Erro ao buscar tokens {opp.slug}: {e}")
            return None

    def _flip_opportunity_to_side(
        self, opp: Opportunity, new_outcome: str
    ) -> bool:
        """
        Modifica uma Opportunity IN-PLACE para apontar para o outcome dominante.

        Atualiza:
          - opp.outcome_side → new_outcome
          - opp.token_id     → token correspondente ao novo outcome
          - opp.avg_entry_price → preço atual do novo lado (melhor estimativa)
          - opp.title → prefixo [FLIP → new_outcome]

        Returns:
            True se flip foi bem-sucedido, False se não conseguiu encontrar token.
        """
        import json as _json

        market_info = self._fetch_market_tokens(opp)
        if not market_info:
            logger.warning(
                f"FLIP falhou: não consegui buscar tokens de {opp.slug}"
            )
            return False

        outcomes = market_info["outcomes"]
        token_ids = market_info["token_ids"]
        prices_raw = market_info.get("prices", [])

        if isinstance(prices_raw, str):
            try:
                prices_raw = _json.loads(prices_raw)
            except Exception:
                prices_raw = []

        # Match case-insensitive do novo outcome
        new_outcome_upper = new_outcome.strip().upper()
        target_idx = None
        for i, oc in enumerate(outcomes):
            if oc.strip().upper() == new_outcome_upper:
                target_idx = i
                break

        if target_idx is None:
            # Fallback: se o target é YES/NO mas outcomes são teams, tenta match fuzzy
            for i, oc in enumerate(outcomes):
                oc_upper = oc.strip().upper()
                if new_outcome_upper in oc_upper or oc_upper in new_outcome_upper:
                    target_idx = i
                    break

        if target_idx is None or target_idx >= len(token_ids):
            logger.warning(
                f"FLIP falhou: outcome '{new_outcome}' não encontrado em "
                f"{outcomes} para {opp.slug}"
            )
            return False

        old_side = opp.outcome_side
        old_token = opp.token_id
        opp.outcome_side = outcomes[target_idx]  # Usa o nome oficial
        opp.token_id = token_ids[target_idx]

        # Atualizar preço de entrada estimado com o preço atual do novo lado
        try:
            if target_idx < len(prices_raw):
                new_price = float(prices_raw[target_idx])
                if 0 < new_price < 1:
                    opp.avg_entry_price = round(new_price, 4)
        except Exception:
            pass

        # Atualizar título para mostrar o flip
        opp.title = f"[FLIP→{opp.outcome_side}] {opp.title}"

        logger.info(
            f"✅ FLIP executado: {opp.slug[:40]} — "
            f"{old_side} → {opp.outcome_side} "
            f"(novo token {opp.token_id[:16]}..., preço ~{opp.avg_entry_price})"
        )
        return True

    def _filter_opportunities(
        self, opportunities: List[Opportunity]
    ) -> List[Opportunity]:
        """Filtra oportunidades que atendem aos criterios de qualidade.

        🔴 ORDEM CRÍTICA DE FILTROS (reordenado em v3.2):
           1. Já posicionado? (cache fast path)
           2. MERCADO RESOLVIDO? (API call aqui, rejeita mortos antes de tudo)
           3. Data passada? (data < hoje)
           4. Categoria (esporte/e-sports)
           5. Score ≥ 140
           6. Confiança ≠ BAIXA
           7. PnL ≥ min_whale_pnl
           8. Preço 0.20-0.90
           9. Liquidez ≥ $500k

        Antes (v3.1): API call era o PENÚLTIMO filtro → processava 30+ mortos.
        Agora (v3.2):  API call é o SEGUNDO filtro → descarta mortos imediatamente.
        """
        import json as _json
        from datetime import timedelta

        # ✅ NOVAS REGRAS DE QUALIDADE
        MIN_SCORE = 140              # Score composto mínimo (≥140 = qualidade, evita lixo)
        # ✅ FIX v3.4: Liquidez mínima reduzida de $500k → $150k
        # $500k matava 90%+ das oportunidades: só finais de campeonato passavam.
        # $150k cobre mercados ATP/NBA regulares com orderbook funcional.
        # Compensação: max_cost limitado por tier de liquidez (ver abaixo).
        MIN_LIQUIDITY = 5_000        # era $500k → $150k → $50k → agora $5k (paper trading)
        PRICE_MIN = 0.65             # Apenas favoritos ≥65% — sem underdogs
        PRICE_MAX = 0.92             # Permite entradas como Dodgers 84%

        # ✅ PREFIXOS DE LIGA (matching por primeiro segmento da slug)
        # Polymarket usa códigos de 3 letras: nba-lac-por, fl1-pfc-asm, lal-rea-gir
        SPORT_PREFIXES = {
            # Esportes americanos
            "nba", "nhl", "mlb", "nfl", "mls",
            "cbb", "cfb", "ncaa",  # College
            # Tênis
            "atp", "wta",
            # Futebol europeu (códigos Polymarket)
            "epl", "efl",                    # English Premier/League
            "bl1", "bl2", "bun",              # Bundesliga 1/2 (Alemanha) — 'bun' é código alternativo
            "fl1", "fl2",                     # Ligue 1/2 (França)
            "lal", "lal2", "es2", "esp",      # La Liga + Segunda División (Espanha)
            "sea", "seb", "ita",              # Serie A/B (Itália)
            "eri", "ere",                     # Eredivisie (Holanda)
            "pri",                            # Primeira Liga (Portugal)
            "tur",                            # Turkish Super Lig
            "den",                            # Danish Superliga
            "scl", "sco",                     # Scottish Premiership
            "nor", "sve",                     # Norwegian, Swedish
            "bel",                            # Belgian Pro League
            "swi", "aus",                     # Swiss, Austrian
            "gre",                            # Greek Super League
            "rus",                            # Russian Premier
            "ukr",                            # Ukrainian Premier
            "jpn", "jap",                     # Japanese J-League
            "kor",                            # Korean K-League
            "chn",                            # Chinese Super League
            "bra",                            # Brasileirão
            "arg",                            # Argentina
            "mex",                            # Liga MX
            "usl",                            # USL
            "ucl", "uel", "ufl",              # UEFA Champions/Europa/Conf League
            "cup", "cop", "copa",             # Cups/Copa
            # E-sports
            "cs2", "csg", "csgo",             # Counter-Strike
            "dot", "dota",                    # Dota 2
            "lol", "lcs", "lck", "lec",       # League of Legends
            "val", "vct",                     # Valorant
            "owl", "ow2",                     # Overwatch
            "rl",                             # Rocket League
            # Outros esportes
            "ufc", "mma", "box",              # Combat
            "f1", "mgp", "nas",               # Motorsports
            "pga", "lpg",                     # Golf
            "rug", "cri",                     # Rugby, Cricket
            "oly",                            # Olympics
            "wsl",                            # Women's Super League
            "wnb",                            # WNBA
        }

        # ✅ KEYWORDS (fallback por substring para títulos descritivos)
        SPORT_KEYWORDS = (
            "tennis", "soccer", "futebol", "fifa",
            "champions", "bundesliga", "premier", "laliga", "la-liga",
            "ligue", "serie-a", "serie-b", "eredivisie",
            "brasileir", "libertadores", "sudamericana",
            "rugby", "cricket", "olympic", "olimpi",
            "boxing", "boxe", "formula", "motogp",
            "esports", "e-sports", "tournament",
            "sports", "sport-",
        )

        # ── DIAGNÓSTICO: contadores por filtro ─────────────────────────
        dbg = {"total": len(opportunities), "already_in": 0, "categoria": 0,
               "score": 0, "confianca": 0, "hedge_real": 0, "flip": 0,
               "pnl": 0, "preco": 0,
               "market_api": 0, "closed": 0, "ended": 0, "decided": 0,
               "timing": 0, "passou": 0}
        rejeitadas = []  # ✅ Rastrear rejeitadas com motivo

        # ✅ FIX v3.3: Set de slugs já negociados HOJE (evita recomprar mesmo mercado)
        # Bug anterior: após WIN/LOSS, status vira "closed"/"stopped" mas bot reentrava
        # no próximo ciclo. Ex: atp-buse-moutet comprado 84x no mesmo dia!
        import re as _re_slug
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        traded_today = {
            p.slug for p in self.positions
            if p.status in ("open", "pending", "closed", "stopped", "cancelled")
            and today_str in p.slug   # slug contém a data do evento
        }

        filtered = []
        for opp in opportunities:
            # 🔴 FILTRO #1: Já negociado hoje? (open, pending, closed ou stopped)
            if opp.slug in traded_today:
                dbg["already_in"] += 1
                continue

            # Também bloqueia se ainda está aberto/pendente (slug sem data)
            already_in = any(
                p.slug == opp.slug and p.status in ("open", "pending")
                for p in self.positions
            )
            if already_in:
                dbg["already_in"] += 1
                continue

            # 🔴 FILTRO #2: MERCADO RESOLVIDO? (API call aqui, rejeita mortos imediatamente)
            # Antes (v3.1): this was the LAST filter → processed 30+ dead markets
            # Agora (v3.2): this is the SECOND filter → reject dead markets immediately
            market_is_dead = False
            try:
                resp = requests.get(
                    f"{GAMMA_API}/markets",
                    params={"slug": opp.slug},
                    timeout=5,
                )
                resp.raise_for_status()
                markets = resp.json()

                if markets and len(markets) > 0:
                    market_data = markets[0]

                    # ✅ Hard block: Mercado já resolvido (closed=True)
                    if market_data.get("closed", False):
                        dbg["closed"] += 1
                        rejeitadas.append((opp.slug[:60], "CLOSED=TRUE"))
                        logger.info(f"Pulando {opp.slug}: mercado já resolvido (closed=True)")
                        market_is_dead = True

                    # ✅ Hard block: Preços decididos (0.95+ ou 0.05-)
                    # = mercado essencialmente resolvido, oportunidade morta
                    if not market_is_dead:
                        prices_raw = market_data.get("outcomePrices", [])
                        if isinstance(prices_raw, str):
                            try:
                                prices_raw = _json.loads(prices_raw)
                            except Exception:
                                prices_raw = []
                        if isinstance(prices_raw, list) and len(prices_raw) >= 1:
                            try:
                                all_prices = [float(p) for p in prices_raw]
                                # Mercado decidido: um lado >= 95% OU <= 5%
                                if max(all_prices) >= 0.95 or min(all_prices) <= 0.05:
                                    dbg["decided"] += 1
                                    rejeitadas.append((opp.slug[:60], f"PREÇOS DECIDIDOS {[f'{p:.2f}' for p in all_prices]}"))
                                    logger.info(
                                        f"Pulando {opp.slug}: preços decididos "
                                        f"{[f'{p:.2f}' for p in all_prices]} — mercado resolvido"
                                    )
                                    market_is_dead = True
                            except Exception:
                                pass

                    # Se mercado está morto, pula todo o resto do processamento
                    if market_is_dead:
                        continue

                    # ✅ Guardar dados do mercado na opp (para uso posterior)
                    # Não rejeita, mas enriquece a opp com informações API
                    end_iso = market_data.get("endDateIso") or market_data.get("endDate")
                    if end_iso:
                        opp.end_date = end_iso

                    liquidity = market_data.get("liquidity") or 0
                    if isinstance(liquidity, str):
                        try:
                            liquidity = float(liquidity)
                        except Exception:
                            liquidity = 0

                    # ✅ LIQUIDEZ check (depois de decidir que mercado tá vivo)
                    if liquidity > 0 and liquidity < MIN_LIQUIDITY:
                        dbg["market_api"] += 1
                        logger.info(
                            f"[LIQUIDEZ ${liquidity:,.0f}<${MIN_LIQUIDITY:,}] {opp.slug[:50]}"
                        )
                        continue

                    # Salvar liquidez real para calibrar tamanho no execute
                    if liquidity > 0:
                        opp.market_liquidity = liquidity

            except Exception as e:
                logger.debug(f"Erro ao verificar mercado {opp.slug} (filtro crítico): {e}")
                # Se falhar a API, continua processando com cautela
                # Melhor perder oportunidade do que entrar em mercado morto

            # 🔴 FILTRO #3: DATA PASSADA (rejeitar slugs com data anterior a hoje)
            import re as _re
            _date_match = _re.search(r'(\d{4}-\d{2}-\d{2})', opp.slug)
            if _date_match:
                from datetime import date as _date
                slug_date = _date.fromisoformat(_date_match.group(1))
                if slug_date < datetime.now(timezone.utc).date():
                    dbg["categoria"] += 1
                    rejeitadas.append((opp.slug[:60], f"DATA PASSADA ({slug_date})"))
                    continue

            # 🔴 FILTRO #4: CATEGORIA (prefixo de liga OU substring keyword)
            slug_lower = opp.slug.lower()
            slug_prefix = slug_lower.split("-", 1)[0] if "-" in slug_lower else slug_lower
            is_sport = (
                slug_prefix in SPORT_PREFIXES
                or any(kw in slug_lower for kw in SPORT_KEYWORDS)
            )
            if not is_sport:
                dbg["categoria"] += 1
                rejeitadas.append((opp.slug[:60], f"NÃO É ESPORTE (prefix='{slug_prefix}')"))
                continue

            # 🔴 FILTRO #5: SCORE composto ≥ MIN_SCORE (value scan passa direto)
            if not getattr(opp, 'is_value_scan', False) and opp.score < MIN_SCORE:
                dbg["score"] += 1
                logger.info(f"[SCORE {opp.score:.0f}<{MIN_SCORE}] {opp.slug[:50]}")
                continue

            # ✅ REATIVADO v3.4: Dominance check com threshold corrigido (35%, era 10%)
            # Causa raiz da falha anterior: threshold 10% rejeitava mesmo $50k vs $5.6k.
            # Agora só rejeita hedging genuíno (lado menor ≥35% do total = divisão ~65/35).
            dominance = self._analyze_whale_dominance(opp)
            action = dominance["action"]

            if action == "reject":
                dbg["hedge_real"] += 1
                rejeitadas.append((opp.slug[:60], f"HEDGE REAL: {dominance['reason']}"))
                logger.info(f"REJEITANDO {opp.slug[:50]}: {dominance['reason']}")
                continue

            if action == "flip":
                logger.warning(f"🔄 FLIP {opp.slug[:50]}: {dominance['reason']}")
                if not self._flip_opportunity_to_side(opp, dominance["dominant_outcome"]):
                    dbg["flip"] += 1
                    rejeitadas.append((opp.slug[:60], f"FLIP FALHOU: {dominance['reason']}"))
                    logger.info(f"REJEITANDO {opp.slug[:50]}: flip falhou, não entra no lado residual")
                    continue
                else:
                    dbg["flip"] += 1  # flip bem-sucedido — conta mas não rejeita
            else:
                logger.debug(f"ACEITO {opp.slug[:50]}: {dominance['reason']}")

            # 🔴 FILTRO #6: CONFIANÇA ≠ BAIXA
            if opp.confidence == "BAIXA":
                dbg["confianca"] += 1
                continue

            # 🔴 FILTRO #7: PnL das whales (value scan: aceita pnl ≥ 0%)
            pnl_threshold = 0.0 if getattr(opp, 'is_value_scan', False) else self.min_whale_pnl
            if opp.avg_pnl_pct < pnl_threshold:
                dbg["pnl"] += 1
                logger.info(f"[PNL {opp.avg_pnl_pct:.1f}%<{pnl_threshold}%] {opp.slug[:50]}")
                continue

            # 🔴 FILTRO #8: FAIXA DE ODDS (PRICE_MIN–PRICE_MAX)
            if opp.avg_entry_price > PRICE_MAX or opp.avg_entry_price < PRICE_MIN:
                dbg["preco"] += 1
                logger.info(f"[PREÇO {opp.avg_entry_price:.3f}] {opp.slug[:50]}")
                continue

            # ✅ PASSOU TODOS OS FILTROS
            dbg["passou"] += 1
            filtered.append(opp)

        # ── RELATÓRIO DIAGNÓSTICO ────────────────────────────────────
        print(f"\n  📊 DIAGNÓSTICO DE FILTROS ({dbg['total']} oportunidades brutas):")
        if rejeitadas:
            print(f"\n  REJEITADAS ({len(rejeitadas)}):")
            for slug, razao in rejeitadas:
                print(f"     • {slug:50s} → {razao}")
        print(f"\n  CONTADORES POR FILTRO (ordem v3.2: mercado resolvido PRIMEIRO):")
        print(f"     ❌ Já posicionado:               {dbg['already_in']}")
        print(f"     ❌ Mercado closed:               {dbg['closed']}")
        print(f"     ❌ Preços decididos (>95%/<5%):  {dbg['decided']}")
        print(f"     ❌ Data passada / Não é esporte: {dbg['categoria']}")
        print(f"     ❌ Score      (<{MIN_SCORE}):           {dbg['score']}")
        print(f"     ❌ Confiança (BAIXA):            {dbg['confianca']}")
        print(f"     ❌ Hedge real (≥35% no lado menor): {dbg['hedge_real']}")
        print(f"     🔄 Flip executado (lado oposto):    {dbg['flip']}")
        print(f"     ❌ PnL        (<{self.min_whale_pnl}%):          {dbg['pnl']}")
        print(f"     ❌ Preço      (fora 0.20-0.90):  {dbg['preco']}")
        print(f"     ❌ Liquidez   (<${MIN_LIQUIDITY:,}):   {dbg['market_api']}")
        print(f"     ✅ Passou todos os filtros:      {dbg['passou']}")

        return filtered

    def _get_market_price(self, pos) -> tuple:
        """
        Retorna (current_price, is_resolved) para uma posição.

        Estratégia de lookup em cascata (da mais confiável para fallback):
          1. token_id via Gamma API  → mais confiável
          2. slug via Gamma API      → fallback
          3. Nenhum dos dois         → (None, False)

        current_price: float ou None
        is_resolved:   True se o mercado foi resolvido (preço == 0 ou 1)
        """
        import json as _json

        def _extract_price(market_data: dict, outcome_side: str):
            """Extrai o preço correto dado o outcome side."""
            prices_raw = market_data.get("outcomePrices", [])
            if isinstance(prices_raw, str):
                try:
                    prices_raw = _json.loads(prices_raw)
                except Exception:
                    return None

            if not isinstance(prices_raw, list) or len(prices_raw) == 0:
                return None

            # Caso especial: mercado resolvido com preço 0 (total loss)
            # NÃO ignorar price == 0 — é informação crítica!
            if len(prices_raw) == 1:
                try:
                    return float(prices_raw[0])
                except Exception:
                    return None

            outcome = outcome_side.upper()
            outcomes_list = market_data.get("outcomes", [])
            if isinstance(outcomes_list, str):
                try:
                    outcomes_list = _json.loads(outcomes_list)
                except Exception:
                    outcomes_list = []

            # Tentar match exato por nome
            for idx, oc in enumerate(outcomes_list):
                if str(oc).upper() == outcome:
                    try:
                        return float(prices_raw[idx])
                    except Exception:
                        return None

            # Fallback YES/NO padrão
            if outcome in ("NO", "N", "FALSE"):
                try:
                    return float(prices_raw[1])
                except Exception:
                    return None
            try:
                return float(prices_raw[0])
            except Exception:
                return None

        # ── Busca em cascata: /markets → /events (fallback para mercados arquivados) ──
        # Polymarket arquiva mercados resolvidos: /markets retorna [] mas /events retorna dados.
        # Cascata: tenta /markets primeiro, se vazio tenta /events.
        if pos.slug:
            # --- Tentativa 1: /markets endpoint ---
            mkt_data = None
            try:
                resp = requests.get(
                    f"{GAMMA_API}/markets",
                    params={"slug": pos.slug},
                    timeout=5,
                )
                if resp.status_code == 200:
                    markets = resp.json()
                    if markets and len(markets) > 0:
                        mkt_data = markets[0]
            except Exception:
                pass

            # --- Tentativa 2: /events endpoint (fallback para mercados arquivados) ---
            # Quando /markets retorna vazio (mercado arquivado após resolução),
            # /events ainda contém os dados completos incluindo outcomePrices finais.
            if mkt_data is None:
                try:
                    resp = requests.get(
                        f"{GAMMA_API}/events",
                        params={"slug": pos.slug},
                        timeout=5,
                    )
                    if resp.status_code == 200:
                        events = resp.json()
                        if events and len(events) > 0:
                            # Dentro do evento, procurar o mercado exato pelo slug
                            for m in events[0].get("markets", []):
                                if m.get("slug") == pos.slug:
                                    mkt_data = m
                                    break
                except Exception:
                    pass

            # --- Extrair preço do market_data encontrado ---
            if mkt_data is not None:
                price = _extract_price(mkt_data, pos.outcome_side)
                if price is not None:
                    # Threshold alinhado com o filtro (0.95, não 0.99)
                    is_resolved = (price >= 0.95 or price <= 0.05)
                    return price, is_resolved

                # Fallback: mercado closed sem preço extraível → tentar resolvedBy/winner
                if mkt_data.get("closed", False):
                    winner = mkt_data.get("resolvedBy") or mkt_data.get("winner") or ""
                    if winner and winner.upper() == pos.outcome_side.upper():
                        return 1.0, True
                    elif winner:
                        return 0.0, True

        return None, False

    def _check_orderbook_exists(
        self, trader, token_id: str, slug: str
    ) -> bool:
        """
        Verifica se um token tem orderbook ativo no CLOB.

        Returns:
            True se orderbook existe (mercado pode receber ordem)
            False se não existe (pula silenciosamente, sem cachear como bad)
        """
        ba = self._get_best_ask(trader, token_id, slug)
        return ba is not None

    def _get_best_ask(
        self, trader, token_id: str, slug: str
    ) -> Optional[float]:
        """
        Retorna o MELHOR ASK (preço mais baixo de venda) do orderbook.

        Usado para precificar ORDENS DE COMPRA que devem fillar rapidamente:
        comprar AT THE ASK garante execução imediata (se houver liquidez).

        Returns:
            Best ask price (0.01-0.99) ou None se sem orderbook/liquidez.
        """
        try:
            orderbook = trader._client.get_order_book(token_id) if trader._client else None

            if not orderbook:
                logger.debug(
                    f"Mercado não tem orderbook no CLOB (ainda): {slug}"
                )
                return None

            bids = getattr(orderbook, "bids", []) or []
            asks = getattr(orderbook, "asks", []) or []

            if not bids and not asks:
                logger.debug(f"Orderbook vazio (sem bids/asks): {slug}")
                return None

            # Preferência: best ask (lado vendedor) — garante fill rápido ao comprar
            # Polymarket SDK: asks são ordenados do maior para o menor preço,
            # então o último é o menor = best ask
            if asks:
                ask_prices = []
                for a in asks:
                    try:
                        ask_prices.append(float(a.price))
                    except Exception:
                        continue
                if ask_prices:
                    best_ask = min(ask_prices)  # menor preço = melhor para compra
                    logger.debug(
                        f"✅ Orderbook: {slug} "
                        f"({len(bids)} bids, {len(asks)} asks) — best_ask={best_ask:.4f}"
                    )
                    return best_ask

            # Fallback: se só tem bids (sem asks), usa best bid + pequeno buffer
            if bids:
                bid_prices = []
                for b in bids:
                    try:
                        bid_prices.append(float(b.price))
                    except Exception:
                        continue
                if bid_prices:
                    best_bid = max(bid_prices)
                    # Adicionar 2 cents para ficar acima do bid (limit to 0.99)
                    fallback_price = min(0.99, best_bid + 0.02)
                    logger.debug(
                        f"⚠️  Sem asks para {slug}, usando best_bid+0.02 = {fallback_price:.4f}"
                    )
                    return fallback_price

            return None

        except Exception as e:
            err_str = str(e)
            if "does not exist" in err_str or "orderbook" in err_str.lower():
                logger.debug(f"Orderbook não encontrado: {slug}")
                return None
            logger.debug(f"Erro ao verificar orderbook: {slug} — {e}")
            return None

    def _get_trader(self):
        """
        Retorna (e cria se necessário) o Trader compartilhado para a sessão.
        Evita criar uma nova conexão CLOB a cada trade.

        Returns:
            Trader conectado ou None em caso de falha.
        """
        from bot.trader import Trader

        if self._live_trader is None:
            self._live_trader = Trader(token_id="", dry_run=False)
            self._live_trader_connected = False

        if not self._live_trader_connected:
            logger.info("Conectando ao ClobClient (sessão compartilhada)...")
            if not self._live_trader.connect():
                logger.error("Falha ao conectar ClobClient — verifique .env")
                self._live_trader = None
                self._live_trader_connected = False
                return None
            self._live_trader_connected = True
            logger.info("✅ ClobClient conectado (será reutilizado nesta sessão)")

        return self._live_trader

    def _scan_direct_sports(
        self, positions_by_slug: Dict[str, list]
    ) -> List["Opportunity"]:
        """
        Scanner direto de mercados esportivos ativos.

        Lógica:
          1. Busca todos os mercados MLB/NBA/NHL/etc no Gamma API (active=true)
          2. Filtra: preço do favorito entre 0.55-0.82 (edge real, não extremo)
          3. Data: hoje ou futuro, mercado não decidido (< 95%)
          4. Cross com posições de whales: exige ao menos 1 whale no mesmo lado
          5. Retorna oportunidades marcadas como is_value_scan=True

        Por que 0.55-0.82?
          - Abaixo de 0.55: resultado incerto, sem vantagem clara
          - Acima de 0.82: caro demais, risco/retorno ruim
          - Ex: Yankees a 0.60 com 78% win rate real = +18% edge
        """
        import json as _json
        import re as _re
        from datetime import date as _date_cls

        VALUE_MIN = 0.55
        VALUE_MAX = 0.92

        SPORT_PREFIXES_VALUE = {
            "nba", "mlb", "nhl", "nfl", "mls", "cbb", "cfb", "ncaa",
            "atp", "wta",
            "epl", "bl1", "fl1", "lal", "sea", "ita", "ere",
            "ucl", "uel", "ufl",
            "ufc", "box",
            "f1", "mgp", "nas",
            "wnb", "wsl",
        }

        now = datetime.now(timezone.utc)
        opportunities: List[Opportunity] = []
        covered_slugs = set(positions_by_slug.keys())

        # Paginar mercados ativos
        all_markets: list = []
        for offset_val in range(0, 1000, 100):
            try:
                resp = requests.get(
                    f"{GAMMA_API}/markets",
                    params={
                        "active": "true", "closed": "false",
                        "limit": 100, "offset": offset_val,
                    },
                    timeout=25,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not isinstance(batch, list) or not batch:
                    break
                all_markets.extend(batch)
                if len(batch) < 100:
                    break
                time.sleep(0.15)
            except Exception as e:
                logger.debug(f"Value scan paginação: {e}")
                break

        logger.info(f"Value scan: {len(all_markets)} mercados ativos")

        for m in all_markets:
            try:
                slug = m.get("slug", "")
                if not slug or slug in covered_slugs:
                    continue

                # Prefixo de esporte
                slug_lower = slug.lower()
                prefix = slug_lower.split("-", 1)[0] if "-" in slug_lower else slug_lower
                if prefix not in SPORT_PREFIXES_VALUE:
                    continue

                # Mercado fechado?
                if m.get("closed", False):
                    continue

                # Data: hoje ou futuro
                date_match = _re.search(r"(\d{4}-\d{2}-\d{2})", slug)
                if date_match:
                    try:
                        slug_date = _date_cls.fromisoformat(date_match.group(1))
                        if slug_date < now.date():
                            continue
                    except Exception:
                        pass

                # Parse prices
                prices_raw = m.get("outcomePrices", [])
                if isinstance(prices_raw, str):
                    try:
                        prices_raw = _json.loads(prices_raw)
                    except Exception:
                        continue
                if not isinstance(prices_raw, list) or len(prices_raw) < 2:
                    continue
                try:
                    prices = [float(p) for p in prices_raw]
                except Exception:
                    continue

                # Mercado decidido? Skip
                if max(prices) >= 0.95 or min(prices) <= 0.05:
                    continue

                # Favorito na faixa de valor?
                max_price = max(prices)
                if not (VALUE_MIN <= max_price <= VALUE_MAX):
                    continue

                max_idx = prices.index(max_price)

                # Parse outcomes
                outcomes_raw = m.get("outcomes", ["Yes", "No"])
                if isinstance(outcomes_raw, str):
                    try:
                        outcomes_raw = _json.loads(outcomes_raw)
                    except Exception:
                        outcomes_raw = ["Yes", "No"]
                if max_idx >= len(outcomes_raw):
                    continue
                outcome_side = str(outcomes_raw[max_idx])

                # Token IDs
                tokens_raw = m.get("clobTokenIds", [])
                if isinstance(tokens_raw, str):
                    try:
                        tokens_raw = _json.loads(tokens_raw)
                    except Exception:
                        tokens_raw = []
                token_id = tokens_raw[max_idx] if max_idx < len(tokens_raw) else ""
                condition_id = m.get("conditionId", "")
                liquidity = float(m.get("liquidityNum", 0) or m.get("liquidity", 0) or 0)

                # Cross com whales: procura 1+ whale no mesmo lado
                whale_names: List[str] = []
                whale_pnl_total = 0.0
                whale_value_total = 0.0
                our_side_upper = outcome_side.strip().upper()

                if slug in positions_by_slug:
                    for p in positions_by_slug[slug]:
                        p_outcome = str(p.get("outcome", "")).strip().upper()
                        if p_outcome == our_side_upper or our_side_upper in p_outcome or p_outcome in our_side_upper:
                            wname = p.get("_whale_name", "?")
                            if wname not in whale_names:
                                whale_names.append(wname)
                                whale_pnl_total += float(p.get("percentPnl", 0) or 0)
                                whale_value_total += float(p.get("currentValue", 0) or 0)

                n_whales = len(whale_names)
                if n_whales == 0:
                    continue  # Sem confirmação de smart money

                avg_pnl = whale_pnl_total / n_whales

                # Confidence
                if n_whales >= 3:
                    confidence = "ALTA"
                elif n_whales >= 2:
                    confidence = "MEDIA"
                else:
                    confidence = "MODERADA"

                title = m.get("question", slug)[:80]
                opp = Opportunity(
                    title=f"[{outcome_side}] {title}",
                    slug=slug,
                    whale_count=n_whales,
                    whales=whale_names,
                    best_whale_rank=7,
                    total_value=max(whale_value_total, 100.0),
                    avg_entry_price=round(max_price, 4),
                    avg_pnl_pct=round(avg_pnl, 1),
                    confidence=confidence,
                    outcome_side=outcome_side,
                    condition_id=condition_id,
                    token_id=token_id,
                    end_date=m.get("endDate", ""),
                    market_liquidity=liquidity,
                    is_value_scan=True,
                )
                opportunities.append(opp)

            except Exception as e:
                logger.debug(f"Value scan erro em {m.get('slug', '?')}: {e}")
                continue

        # Ordenar: maior preço = mais favorito = mais edge
        opportunities.sort(key=lambda o: -o.avg_entry_price)
        logger.info(f"Value scan: {len(opportunities)} oportunidades diretas (1+ whale)")
        return opportunities

    def _execute_trade(self, opp: Opportunity) -> bool:
        """Executa um trade (dry-run ou live)."""
        # ✅ NOVO: Capital limit global — para se perder 50% do capital inicial
        # Proteção contra série de perdas
        capital_limit_threshold = self.capital_initial * 0.50  # 50% da inicial
        if self.capital < capital_limit_threshold:
            logger.warning(
                f"🚨 CAPITAL LIMIT ACIONADO: ${self.capital:.2f} < ${capital_limit_threshold:.2f} (50% da inicial)"
            )
            print(f"  🚨 PARADO: Capital abaixo de 50% da inicial. Nenhum novo trade.")
            return False

        # ═══════════════════════════════════════════════════════════
        # 🎾 TENNIS QUALIFIER/CHALLENGER FILTER
        # ═══════════════════════════════════════════════════════════
        # Qualifiers e challengers têm VARIÂNCIA EXTREMA — uma surpresa
        # no placar (tiebreak, lesão) inverte tudo.
        # Filtro rigoroso: 4 whales mín, trade size $0.80, PnL ≥15%
        # ═══════════════════════════════════════════════════════════
        slug_lower = opp.slug.lower()
        is_tennis_qualifier = (
            ("qualifier" in slug_lower or "qualif" in slug_lower) and
            ("atp" in slug_lower or "wta" in slug_lower or "itf" in slug_lower)
        )

        if is_tennis_qualifier:
            # Filtro rigoroso para qualifiers
            if opp.whale_count < 4:
                print(f"  ⏭️  QUALIF BLOQUEADO: {opp.slug[:50]} — só {opp.whale_count} whales (precisa ≥4)")
                logger.info(f"[TENNIS QUAL BLOCKED] {opp.slug}: {opp.whale_count} whales < 4 min")
                return False
            if opp.avg_pnl_pct < 15.0:
                print(f"  ⏭️  QUALIF BLOQUEADO: {opp.slug[:50]} — PnL {opp.avg_pnl_pct:.1f}% < 15% mín")
                logger.info(f"[TENNIS QUAL BLOCKED] {opp.slug}: PnL {opp.avg_pnl_pct:.1f}% < 15%")
                return False
            dynamic_trade_size = 0.80  # Máximo $0.80 em qualifiers
            print(f"  🎾 QUALIF RIGOROSO: {opp.slug[:50]} | {opp.whale_count} whales | PnL {opp.avg_pnl_pct:.1f}%")
        else:
            # Padrão para futebol/NBA/esportes com mais liquidez
            # ✅ Trade size dinâmico por confiança — calibrado para conta de $10-$11
            # ALTA    = $1.50  (sinal forte, 3+ whales — apostar um pouco mais)
            # MEDIA   = $1.20  (bom consenso — padrão)
            # MODERADA= $1.00  (ok, mas com ressalvas — cautela)
            # BAIXA   = $0.70  (whale de risco — mínimo viável)
            import math
            confidence_sizes = {
                "ALTA":     1.50,
                "MEDIA":    1.20,
                "MODERADA": 1.00,
                "BAIXA":    0.70,
            }
            dynamic_trade_size = confidence_sizes.get(opp.confidence, 1.00)

            # ✅ FIX v3.4: Tier de liquidez — mercados menos líquidos recebem size menor
            # Com liquidez $150k-$500k agora permitida, calibrar posição pelo risco de
            # impacto de mercado e slippage. Quanto menos líquido, menor o tamanho.
            liq = opp.market_liquidity
            if liq > 0:
                if liq >= 500_000:
                    liq_mult = 1.00   # mercado profundo — size normal
                elif liq >= 300_000:
                    liq_mult = 0.80   # bom — 80% do size
                elif liq >= 200_000:
                    liq_mult = 0.65   # ok — 65% do size
                else:
                    liq_mult = 0.50   # $150-200k — mínimo seguro, 50% do size
                if liq_mult < 1.0:
                    dynamic_trade_size = round(dynamic_trade_size * liq_mult, 2)
                    logger.info(
                        f"[LIQ TIER ${liq:,.0f}] Size ajustado: ×{liq_mult} → ${dynamic_trade_size:.2f}"
                    )

            # V3: Portfolio SL semanal → corta size 50%
            if getattr(self, "_weekly_sl_triggered", False):
                dynamic_trade_size *= 0.50
                logger.info(f"[WEEKLY SL] Trade size cortado 50%: ${dynamic_trade_size:.2f}")

            # V3: Kelly Fraction F7 (Manual v2.1, Seção 07)
            # "Position size ≤ Kelly 0.15 (máximo 15% da banca por trade)"
            kelly_max = self.capital * 0.15
            if dynamic_trade_size > kelly_max:
                dynamic_trade_size = round(kelly_max, 2)
                logger.info(f"[KELLY F7] Trade size capped: ${dynamic_trade_size:.2f} (15% de ${self.capital:.2f})")

        # Custo mínimo real: usa o HUNT PRICE (10% abaixo da whale)
        # pois é o preço que realmente vamos usar na ordem.
        WHALE_HUNT_DISCOUNT_PRE = 0.10
        whale_price_pre = max(opp.avg_entry_price, 0.01)
        hunt_price_pre  = max(0.20, whale_price_pre * (1 - WHALE_HUNT_DISCOUNT_PRE))
        min_shares_for_notional = math.ceil(1.0 / hunt_price_pre)
        min_actual_cost = max(dynamic_trade_size, min_shares_for_notional * hunt_price_pre)

        # ✅ STOP: Custo real excede limite máximo por trade
        if min_actual_cost > self.max_cost:
            print(f"  ❌ CUSTO ALTO: ${min_actual_cost:.2f} > limite ${self.max_cost:.2f}")
            logger.info(
                f"Custo mín ${min_actual_cost:.2f} > max permitido "
                f"${self.max_cost:.2f} — pulando {opp.slug}"
            )
            return False

        # Verificar capital disponível
        if self.capital < min_actual_cost:
            print(f"  ❌ CAPITAL INSUFICIENTE: ${self.capital:.2f} < ${min_actual_cost:.2f} necessários")
            logger.warning(
                f"Capital insuficiente: ${self.capital:.2f} < "
                f"${min_actual_cost:.2f} (mín CLOB: 5 shares @ {opp.avg_entry_price:.2f})"
            )
            return False

        # Verificar max posicoes (pending + open contam como slots ocupados)
        open_count = sum(1 for p in self.positions if p.status in ("open", "pending"))
        if open_count >= self.max_positions:
            print(f"  ❌ SLOT CHEIO: {open_count}/{self.max_positions} posições abertas (máximo atingido)")
            logger.info(f"Max posicoes atingido: {open_count}/{self.max_positions}")
            return False

        now = datetime.now(timezone.utc).isoformat()

        if self.dry_run:
            # Simular
            print(f"  🧪 [DRY RUN] Comprando {opp.outcome_side}: {opp.title[:50]}")
            print(f"     Preco: {opp.avg_entry_price:.3f} | Size: ${dynamic_trade_size:.2f} (dynamic, raw={self.trade_size:.2f})")
            print(f"     Whales: {', '.join(opp.whales[:3])} ({opp.whale_count} total)")
            actual_cost = dynamic_trade_size
            placed_order_id = ""
        else:
            # ✅ LIVE TRADING — integrado com bot/trader.py
            print(f"  🔴 [LIVE] Comprando {opp.outcome_side}: {opp.title[:50]}")
            print(f"     Preco: {opp.avg_entry_price:.3f} | Size: ${self.trade_size:.2f}")
            print(f"     Token ID: {opp.token_id or 'N/A'}")

            if not opp.token_id:
                print(f"  ❌ TOKEN ID AUSENTE: não consegui encontrar mercado no Polymarket")
                logger.error(f"Token ID ausente para {opp.slug} — abortando ordem real")
                return False

            # ⚠️ CACHE DESATIVADO: não pulamos mais tokens baseados em cache.
            # Toda oportunidade é consultada contra o orderbook do CLOB em tempo
            # real via _get_best_ask(). Markets que ganharam liquidez desde o
            # último ciclo agora terão nova chance de execução.

            try:
                from strategies.base_strategy import Signal, SignalType
                import pandas as pd

                # Usar trader compartilhado (uma única conexão por sessão)
                trader = self._get_trader()
                if trader is None:
                    return False

                # ═══════════════════════════════════════════════════════════
                # F3: SLIPPAGE CHECK (Manual v2.1, Seção 07)
                # ═══════════════════════════════════════════════════════════
                # "O preço atual está dentro de 5-8% do preço médio de
                #  entrada da whale? Se moveu mais: edge desapareceu."
                # Este filtro SOZINHO teria evitado a maioria das perdas.
                # ═══════════════════════════════════════════════════════════
                best_ask = self._get_best_ask(trader, opp.token_id, opp.slug)
                if best_ask is None:
                    print(f"  ❌ SEM ORDERBOOK: {opp.slug} — sem liquidez no CLOB (tentará de novo no próximo ciclo)")
                    return False

                MAX_SLIPPAGE = 0.08  # 8% máximo (F3 do manual)
                whale_entry = opp.avg_entry_price
                if whale_entry > 0:
                    slippage = abs(best_ask - whale_entry) / whale_entry
                    if slippage > MAX_SLIPPAGE:
                        print(
                            f"  ⏭️  SLIPPAGE F3: {opp.slug[:45]} — "
                            f"whale={whale_entry:.3f} ask={best_ask:.3f} "
                            f"(slippage {slippage:.1%} > {MAX_SLIPPAGE:.0%})"
                        )
                        logger.info(f"[SLIPPAGE F3] {opp.slug}: {slippage:.1%} > {MAX_SLIPPAGE:.0%}")
                        return False

                # ═══════════════════════════════════════════════════════════
                # 🎣 WHALE HUNTING MODE
                # ═══════════════════════════════════════════════════════════
                # Whales usam "hook": compram agressivamente a preço X para
                # atrair bots/varejo, depois VENDEM causando queda de 5-20%.
                # Solução: entrar 10% ABAIXO do preço que a whale sinalizou.
                # ═══════════════════════════════════════════════════════════
                WHALE_HUNT_DISCOUNT = 0.10   # 10% abaixo do preço da whale

                whale_price = opp.avg_entry_price
                hunt_price  = round(whale_price * (1 - WHALE_HUNT_DISCOUNT), 3)

                # Hunt price deve estar na faixa operacional
                execution_price = max(0.20, min(0.90, hunt_price))

                # Se o hunt price ficou muito abaixo do ask atual, o mercado já
                # andou muito — risco de nunca preencher. Ignorar se gap > 35%.
                if best_ask > 0 and (best_ask - execution_price) / best_ask > 0.35:
                    print(
                        f"  ⏭️  GAP GRANDE: {opp.slug} — "
                        f"hunt={execution_price:.3f} vs ask={best_ask:.3f} "
                        f"(gap {(best_ask - execution_price)/best_ask:.0%} > 35%)"
                    )
                    logger.info(
                        f"[HUNT GAP] {opp.slug}: hunt={execution_price:.3f} "
                        f"ask={best_ask:.3f} gap={(best_ask-execution_price)/best_ask:.0%}"
                    )
                    return False

                # Log: whale price vs hunt price vs ask atual
                logger.info(
                    f"🎣 WHALE HUNT {opp.slug}: whale={whale_price:.3f} "
                    f"hunt={execution_price:.3f} (−{WHALE_HUNT_DISCOUNT:.0%}) "
                    f"ask_atual={best_ask:.3f}"
                )
                print(
                    f"  🎣 WHALE HUNT: whale={whale_price:.3f} → "
                    f"entrada={execution_price:.3f} (−{WHALE_HUNT_DISCOUNT:.0%} desconto)"
                )

                sig = Signal(
                    signal_type=SignalType.BUY,
                    price=execution_price,  # ← hunt price: 10% abaixo da whale
                    size=dynamic_trade_size,  # ← dynamic (Kelly + confiança + liquidez)
                    confidence=0.75,
                    strategy="WhaleHunter",
                    reason=(
                        f"Whale hunt: {opp.whale_count} whales @ {whale_price:.3f} "
                        f"→ hunt @ {execution_price:.3f} (−{WHALE_HUNT_DISCOUNT:.0%})"
                    ),
                    timestamp=pd.Timestamp.now(),
                )

                # Registrar hunt price como entry (não o preço da whale)
                opp.avg_entry_price = execution_price

                order = trader.place_limit_order(
                    sig,
                    size=dynamic_trade_size,  # ← dynamic (era self.trade_size = bug)
                    token_id_override=opp.token_id,
                    max_cost_usd=self.max_cost,  # ← hard cap no trader
                )
                if not order:
                    # ⚠️ CACHE DESATIVADO: não adicionamos mais ao bad_tokens.
                    # Próximo ciclo vai re-consultar o orderbook do CLOB.
                    error_msg = trader.last_error or "Erro desconhecido"
                    if "does not exist" in error_msg or "orderbook" in error_msg.lower():
                        print(f"  ❌ ORDERBOOK NÃO ENCONTRADO: {opp.slug} (retentará no próximo ciclo)")
                        logger.warning(f"⚠️  Sem orderbook CLOB: {opp.slug} — live refresh no próximo ciclo")
                    else:
                        print(f"  ❌ ORDEM REJEITADA: {error_msg[:60]}")
                        logger.error(f"Ordem rejeitada pelo CLOB para {opp.slug}: {error_msg}")
                    return False

                # Custo real da ordem (shares * price, pode diferir do trade_size pelo mínimo de 5 shares)
                actual_cost = order.get("actual_cost_usd", self.trade_size)
                shares_filled = order.get("shares", 0)
                placed_order_id = order.get("order_id", "")
                print(f"  ✅ Ordem submetida: ID={placed_order_id[:20]}... | "
                      f"{shares_filled:.1f} shares @ ${opp.avg_entry_price:.4f} = ${actual_cost:.2f}")
                logger.info(
                    f"LIVE ORDER: {opp.slug} | {opp.outcome_side} | "
                    f"{shares_filled:.1f} shares @ {opp.avg_entry_price:.4f} = ${actual_cost:.2f} | "
                    f"order_id={placed_order_id}"
                )

            except Exception as e:
                err_str = str(e)
                # ⚠️ CACHE DESATIVADO: orderbook inexistente é apenas skip do ciclo atual
                if "does not exist" in err_str or "orderbook" in err_str.lower():
                    print(f"  ❌ ORDERBOOK NÃO ENCONTRADO: {opp.slug} (retentará no próximo ciclo)")
                    logger.warning(f"⚠️  Sem orderbook CLOB: {opp.slug} — live refresh no próximo ciclo")
                    return False
                # Conexão caiu: marcar como desconectado para reconectar no próximo trade
                if "connection" in err_str.lower() or "timeout" in err_str.lower():
                    self._live_trader_connected = False
                    print(f"  ❌ CONEXÃO PERDIDA: reconectando...")
                    logger.warning("Conexão CLOB perdida — será reconectada no próximo trade")
                    return False
                print(f"  ❌ ERRO: {err_str[:60]}")
                logger.error(f"Erro ao submeter ordem live: {e}")
                return False

        # Registrar posição:
        #   - dry_run: simula como "open" com 5 shares para poder monitorar
        #   - live:    status="pending" com shares=0 até Data API confirmar fill
        #              O reconcile() vai verificar via Data API e promover p/ "open"
        #              Se a ordem for cancelada antes de preencher → status="cancelled"
        if self.dry_run:
            initial_status = "open"
            filled_shares = 5.0
        else:
            initial_status = "pending"
            # shares = 0 explicitamente: a ordem está no book mas NÃO preenchida ainda
            # O reconcile _verify_portfolio() vai confirmar e setar as shares reais
            filled_shares = 0.0
        position = ActivePosition(
            slug=opp.slug,
            title=opp.title,
            side=f"BUY_{opp.outcome_side}",
            entry_price=opp.avg_entry_price,
            size_usd=actual_cost,          # Custo real, não estimado
            entry_time=now,
            whale_count=opp.whale_count,
            whales=opp.whales[:5],
            status=initial_status,
            outcome_side=opp.outcome_side,
            end_date=opp.end_date,
            order_id=placed_order_id,      # Para detectar cancelamentos
            token_id=opp.token_id,         # Para SELL stop-loss
            shares=filled_shares,          # 0 = pendente; >0 = preenchida
        )
        self.positions.append(position)
        self.capital -= actual_cost        # Deduz custo real após confirmação

        # Log
        self.trade_log.append({
            "time": now,
            "action": "BUY",
            "title": opp.title,
            "slug": opp.slug,
            "price": opp.avg_entry_price,
            "size": actual_cost,
            "whale_count": opp.whale_count,
            "whales": opp.whales[:5],
            "confidence": opp.confidence,
            "dry_run": self.dry_run,
        })

        self._save_state()
        return True

    # ----------------------------------------------------------------
    # Main cycle
    # ----------------------------------------------------------------

    def _check_stop_losses(self) -> None:
        """
        V3: PORTFOLIO-LEVEL STOP LOSS (Manual Operacional v2.1, Seção 05)

        ⚠️ PER-TRADE STOP LOSS FOI DELETADO.
        O manual é categórico: "stop loss por trade automático vai te matar
        por whipsaw. Uma posição que cai 25% pode estar correta — é só
        ruído temporal."

        Novo sistema (3 camadas):
          1. PORTFOLIO SL SEMANAL: -7% → corta size pela metade
          2. PORTFOLIO SL MENSAL: -15% → pausa total
          3. POSIÇÃO RESOLVIDA: mercado resolveu contra nós → registra loss

        NÃO FAZ: sair de posição individual por queda de preço.
        """
        open_positions = [p for p in self.positions if p.status == "open"]

        # ═══════════════════════════════════════════════════════════
        # CAMADA 1: Verificar posições RESOLVIDAS (preço = 0 ou 1)
        # ═══════════════════════════════════════════════════════════
        for pos in open_positions:
            current_price, is_resolved = self._get_market_price(pos)

            if current_price is None:
                continue

            if pos.entry_price <= 0:
                continue

            # Só age se o mercado RESOLVEU (preço = 0 ou 1)
            if is_resolved:
                loss_pct = (pos.entry_price - current_price) / pos.entry_price
                if loss_pct > 0.50:  # Resolveu contra nós
                    pos.status = "stopped"
                    pos.exit_price = current_price
                    pos.pnl = round(-pos.size_usd * loss_pct, 2)
                    self.capital += pos.size_usd + pos.pnl
                    self._mark_trade_result(pos.slug, "loss")
                    print(f"  📉 RESOLVIDO (LOSS): {pos.title[:45]} | -{loss_pct:.0%}")
                    logger.info(f"RESOLVED LOSS: {pos.slug} | loss={loss_pct:.0%}")
                elif loss_pct < -0.10:  # Resolveu a nosso favor (lucro > 10%)
                    pos.status = "closed"
                    pos.exit_price = current_price
                    pos.pnl = round(pos.size_usd * abs(loss_pct), 2)
                    self.capital += pos.size_usd + pos.pnl
                    self._mark_trade_result(pos.slug, "win")
                    print(f"  📈 RESOLVIDO (WIN): {pos.title[:45]} | +{abs(loss_pct):.0%}")
                    logger.info(f"RESOLVED WIN: {pos.slug} | gain={abs(loss_pct):.0%}")

        # ═══════════════════════════════════════════════════════════
        # CAMADA 2: PORTFOLIO SL SEMANAL (-7%)
        # ═══════════════════════════════════════════════════════════
        # Calcula PnL total do portfólio esta semana
        week_trades = [
            t for t in self.trade_log
            if t.get("time", "") >= (datetime.now(timezone.utc).isoformat()[:10])
        ]
        week_pnl = sum(t.get("pnl", 0) for t in week_trades)
        week_pnl_pct = week_pnl / self.capital_initial if self.capital_initial > 0 else 0

        if week_pnl_pct <= -0.07:
            if not getattr(self, "_weekly_sl_triggered", False):
                self._weekly_sl_triggered = True
                print(f"\n  🚨 PORTFOLIO SL SEMANAL: PnL semanal {week_pnl_pct:.1%} <= -7%")
                print(f"     → Trade size REDUZIDO 50% até próxima semana")
                logger.warning(f"PORTFOLIO SL SEMANAL: {week_pnl_pct:.1%} — size cortado 50%")

        # ═══════════════════════════════════════════════════════════
        # CAMADA 3: PORTFOLIO SL MENSAL (-15%)
        # ═══════════════════════════════════════════════════════════
        # ✅ FIX v3.2: usar VALOR TOTAL do portfólio (capital + investido),
        # não só capital disponível. Antes: ter $1 investido com $8 livre
        # parecia -20% de loss quando era só -10% real.
        open_value = sum(p.size_usd for p in self.positions if p.status == "open")
        total_portfolio = self.capital + open_value
        total_pnl = total_portfolio - self.capital_initial
        total_pnl_pct = total_pnl / self.capital_initial if self.capital_initial > 0 else 0

        if total_pnl_pct <= -0.15:
            if not getattr(self, "_monthly_sl_triggered", False):
                self._monthly_sl_triggered = True
                print(f"\n  🛑 PORTFOLIO SL MENSAL: PnL total {total_pnl_pct:.1%} <= -15%")
                print(f"     → Novas entradas BLOQUEADAS. Bot continua monitorando posições abertas.")
                print(f"     → Para resetar: reinicie com --reset-state")
                logger.critical(f"PORTFOLIO SL MENSAL: {total_pnl_pct:.1%} — novas entradas bloqueadas")
                if not self.dry_run:
                    self._running = False  # Só para em modo live

        self._save_state()

    def _check_exits(self) -> None:
        """
        Verifica se posições abertas resolveram e libera capital.

        TAMBÉM limpa "posições zumbis": se uma posição fica aberta > 6 horas
        e não conseguimos confirmar resolução, força fechamento (evita acúmulo).
        """
        open_positions = [p for p in self.positions if p.status == "open"]
        if not open_positions:
            return

        print(f"\n  Verificando resolucoes de {len(open_positions)} posicoes abertas...")

        # ── LIMPEZA: Posições zumbis abertas > 8 horas ────────────────
        # 8h: NBA/NHL podem durar 3-4h desde abertura da posição + delays
        zombie_cleaned = 0
        for pos in open_positions:
            try:
                entry_dt = datetime.fromisoformat(pos.entry_time)
                age_hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
                if age_hours > 8:
                    pos.status = "closed"
                    pos.pnl = 0.0  # Neutro (não sabemos o resultado real)
                    pos.exit_price = pos.entry_price  # Assume break-even
                    zombie_cleaned += 1
                    print(f"  🧟 ZOMBIE CLEANUP: {pos.title[:50]} ({age_hours:.1f}h aberta)")
                    logger.warning(f"Posição zumbis limpa: {pos.slug} ({age_hours:.1f}h)")
            except Exception:
                pass

        if zombie_cleaned > 0:
            print(f"  → {zombie_cleaned} zumbis limpas\n")

        from deploy.exit_handler import resolve_position

        wins = 0
        losses = 0
        returned = 0.0  # Fix: variável não inicializada causava NameError
        closed_titles = []

        for pos in self.positions:
            if pos.status != "open":
                continue

            # ✅ Detecção via _get_market_price (token_id → slug)
            current_price, is_resolved = self._get_market_price(pos)

            if current_price is None:
                # Não conseguimos a resolução — será detectada no próximo ciclo
                self._blind_positions.add(pos.slug)
                continue

            if is_resolved and current_price is not None:
                we_won = current_price >= 0.99
                pnl = pos.size_usd if we_won else -pos.size_usd
                pos.status = "closed"
                pos.pnl = pnl
                pos.exit_price = current_price
                self._mark_trade_result(pos.slug, "win" if we_won else "loss")
                if we_won:
                    wins += 1
                    closed_titles.append(f"  ✅ WIN: {pos.title[:50]} ${pnl:+.2f}")
                else:
                    losses += 1
                    closed_titles.append(f"  ❌ LOSS: {pos.title[:50]} ${pnl:+.2f}")
                logger.info(f"Mercado resolvido: {pos.slug} | preço={current_price} | pnl={pnl}")
                continue

            # Verificar via exit_handler (método original)
            pos_dict = {
                "slug": pos.slug,
                "title": pos.title,
                "entry_price": pos.entry_price,
                "size_usd": pos.size_usd,
                "side": pos.side,
                "outcome_side": pos.outcome_side,
            }
            resolved = resolve_position(pos_dict)

            if resolved.get("status") == "closed":
                pnl = resolved.get("pnl", 0)
                final_val = resolved.get("final_value", 0)
                exit_price = resolved.get("exit_price", 0)

                pos.status = "closed"
                pos.exit_price = exit_price
                pos.pnl = pnl
                self._mark_trade_result(pos.slug, "win" if pnl > 0 else "loss")

                returned += final_val
                if pnl > 0:
                    wins += 1
                    icon = "✅ WIN"
                else:
                    losses += 1
                    icon = "❌ LOSS"
                closed_titles.append(f"  {icon} {pos.title[:50]}: {pnl:+.2f}")

            time.sleep(0.2)

        if wins + losses > 0:
            # NÃO adicionar capital aqui — _reconcile() já define capital = saldo real
            print(f"  Fechadas: {wins} win(s), {losses} loss(es)")
            for line in closed_titles:
                print(line)
            print(f"  Capital atual: ${self.capital:.2f} (sincronizado com Polymarket)")
            self._save_state()
        else:
            print(f"  Nenhuma posicao resolvida ainda.")

            # AVISO: posicoes antigas travando novas entradas
            still_open = [p for p in self.positions if p.status == "open"]
            open_count = len(still_open)
            if open_count >= self.max_positions:
                logger.warning(f"⚠️  ATENCAO: {open_count}/{self.max_positions} slots ocupados e nenhum resolveu.")
                for pos in still_open:
                    try:
                        entry_dt = datetime.fromisoformat(pos.entry_time)
                        age_hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
                        if age_hours > 12:
                            logger.warning(f"   🕐 {age_hours:.0f}h aberta: {pos.title[:55]}")
                    except Exception:
                        pass

            # SEMPRE salvar mesmo se nenhuma posicao fechou (pode ter aberto novas)
            self._save_state()

    def run_cycle(self) -> int:
        """
        Executa um ciclo completo.
        Returns: numero de trades executados.
        """
        self.cycle_count += 1

        # 0. Sincronizar ANTES de exibir (valores corretos no header)
        self._reconcile()            # Full reconciliation com Polymarket
        self._check_stop_losses()   # 🛑 STOP-LOSS automático
        self._check_exits()         # Posições resolvidas

        # Agora exibir com dados atualizados
        self._display_header()

        # 1. Buscar posicoes das whales
        print("\n  Buscando posicoes das whales...\n")
        positions_by_slug = self.fetcher.fetch_all_positions(
            min_value=500.0, delay=0.4
        )

        total_pos = sum(len(v) for v in positions_by_slug.values())
        print(f"\n  {total_pos} posicoes em {len(positions_by_slug)} mercados\n")

        # 2. Encontrar consenso
        opportunities = self.fetcher.find_consensus(
            positions_by_slug, min_whales=self.min_whales
        )

        # 2b. Value scan direto (MLB/NBA com 1+ whale + preço 0.55-0.82)
        if self.value_scan:
            print("\n  📡 VALUE SCAN: buscando favoritos diretos (1+ whale, 0.55-0.82)...")
            value_opps = self._scan_direct_sports(positions_by_slug)
            consensus_slugs = {o.slug for o in opportunities}
            new_value = [o for o in value_opps if o.slug not in consensus_slugs]
            if new_value:
                print(f"  📡 VALUE SCAN: +{len(new_value)} mercado(s) direto(s) encontrado(s)")
                opportunities = opportunities + new_value
            else:
                print("  📡 VALUE SCAN: nenhum mercado direto qualificado")

        # 3. Exibir
        self._display_opportunities(opportunities)
        self._display_positions()

        # 4. Filtrar e executar
        actionable = self._filter_opportunities(opportunities)

        trades_executed = 0
        if actionable:
            print(f"  EXECUTANDO ({len(actionable)} oportunidades filtradas):\n")
            for opp in actionable[:3]:  # Max 3 trades por ciclo
                if self._execute_trade(opp):
                    trades_executed += 1
                    print()
        else:
            print("  Nenhuma nova oportunidade para executar.\n")

        # 5. Log
        self._display_trade_log()

        # 6. Sumario
        open_count = sum(1 for p in self.positions if p.status == "open")
        total_invested = sum(
            p.size_usd for p in self.positions if p.status == "open"
        )
        wins, losses, win_rate = self._calculate_stats()

        win_rate_str = f"{win_rate:.1f}%" if wins + losses > 0 else "—"
        print(f"  RESUMO: {open_count} posicoes abertas | "
              f"${total_invested:.2f} investido | "
              f"${self.capital:.2f} disponivel")
        print(f"  📈 ESTATISTICAS: {wins}W / {losses}L ({win_rate_str} win rate)")

        # 🚨 ALERTA: Posições cegas (sem preço)
        if self._blind_positions:
            blind_count = len(self._blind_positions)
            print(f"\n  🚨 ALERTA CRITICO: {blind_count} posição(ões) SEM PREÇO (slug não sincronizando)")
            for slug in sorted(self._blind_positions)[:3]:
                print(f"     • {slug}")
            if blind_count > 3:
                print(f"     ... e mais {blind_count - 3}")
            print(f"  → Bot rodando a {100 * open_count / max(1, open_count + blind_count):.0f}% capacidade")
        else:
            # Limpar blind_positions do ciclo anterior
            self._blind_positions.clear()
        print()

        self._save_state()
        return trades_executed

    def run_loop(self, interval_sec: int = 600):
        """Roda o bot em loop continuo."""
        self._running = True
        logger.info(f"Bot iniciado. Ciclo a cada {interval_sec}s.")

        while self._running:
            try:
                self.run_cycle()

                if not self._running:
                    break

                # Countdown
                print(f"  Proximo ciclo em {interval_sec}s... (Ctrl+C para parar)")
                for remaining in range(interval_sec, 0, -30):
                    if not self._running:
                        break
                    time.sleep(min(30, remaining))

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Erro no ciclo: {e}", exc_info=True)
                time.sleep(30)

        self._shutdown()

    def _handle_shutdown(self, signum, frame):
        logger.info("\nShutdown solicitado...")
        self._running = False

    def _shutdown(self):
        self._save_state()
        print()
        print("=" * 65)
        print("  BOT ENCERRADO")
        print("=" * 65)

        open_positions = [p for p in self.positions if p.status == "open"]
        if open_positions:
            print(f"\n  Posicoes abertas ({len(open_positions)}):")
            for p in open_positions:
                print(f"  • {p.title[:55]} | ${p.size_usd:.2f} @ {p.entry_price:.3f}")

        total_invested = sum(p.size_usd for p in open_positions)
        print(f"\n  Capital disponivel: ${self.capital:.2f}")
        print(f"  Total investido:    ${total_invested:.2f}")
        print(f"  Total trades:       {len(self.trade_log)}")
        print(f"\n  Estado salvo em: {self._state_path()}")
        print()


# ================================================================
# CLI
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Whale Trader — Bot automatizado de whale following",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  Dry-run (simulado, seguro):
    python deploy/whale_trader.py

  Rodar uma vez e parar:
    python deploy/whale_trader.py --once

  Ciclo a cada 5 minutos:
    python deploy/whale_trader.py --interval 300

  Trade size menor:
    python deploy/whale_trader.py --trade-size 1 --capital 5

  Modo live (requer --confirm):
    python deploy/whale_trader.py --live --confirm --capital 10 --trade-size 2
        """,
    )

    parser.add_argument(
        "--capital", type=float,
        default=float(os.getenv("INITIAL_CAPITAL", "10.0")),
        help="Capital total disponivel em USDC (padrao: $10)",
    )
    parser.add_argument(
        "--trade-size", type=float, dest="trade_size",
        default=float(os.getenv("MAX_TRADE_SIZE", "1.2")),
        help="Tamanho por trade em USDC (padrao: $1.20)",
    )
    parser.add_argument(
        "--max-positions", type=int, dest="max_positions", default=7,
        help="Maximo de posicoes abertas simultaneas (padrao: 7)",
    )
    parser.add_argument(
        "--min-whales", type=int, dest="min_whales", default=3,
        help="Minimo de whales para consenso (padrao: 3 = consenso forte)",
    )
    parser.add_argument(
        "--min-whale-pnl", type=float, dest="min_whale_pnl", default=7.5,
        help="PnL minimo das whales para considerar (padrao: 7.5%% = whales confiantes)",
    )
    parser.add_argument(
        "--max-price", type=float, dest="max_price", default=0.80,
        help="Preco maximo de entrada (padrao: 0.80 = evitar comprar caro)",
    )
    parser.add_argument(
        "--interval", type=int, default=120,
        help="Intervalo entre ciclos em segundos (padrao: 120 = 2 min)",
    )
    parser.add_argument(
        "--whales", type=int, default=12,
        help="Quantas whales analisar (padrao: 12 whales BOT SPORTS)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Rodar apenas 1 ciclo e parar",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Modo live (capital real)",
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="Confirmar modo live",
    )
    parser.add_argument(
        "--stop-loss", type=float, dest="stop_loss", default=0.30,
        help="Perda maxima antes de sair automaticamente (padrao: 0.20 = 20%%)",
    )
    parser.add_argument(
        "--max-cost", type=float, dest="max_cost", default=2.00,
        help="Custo maximo real por trade em USDC (padrao: $2.00)",
    )
    parser.add_argument(
        "--reset-state", action="store_true", dest="reset_state",
        help="Apagar estado salvo e comecar do zero (capital e posicoes)",
    )
    parser.add_argument(
        "--auto-sync", action="store_true", dest="auto_sync", default=True,
        help="Sincronizar automaticamente saldo com Polymarket (padrao: true)",
    )
    parser.add_argument(
        "--no-auto-sync", action="store_false", dest="auto_sync",
        help="Desabilitar sincronizacao automatica de saldo",
    )
    parser.add_argument(
        "--value-scan", dest="value_scan", action="store_true", default=True,
        help="Ativar scanner direto: MLB/NBA com 1+ whale + preço 0.55-0.82 [padrão: ativado]",
    )
    parser.add_argument(
        "--no-value-scan", dest="value_scan", action="store_false",
        help="Desativar scanner direto de valor",
    )

    args = parser.parse_args()

    # ── LOCK FILE: impede segunda instância simultânea ───────────────
    # Causa raiz das "ordens disparadas": 2 bots rodando ao mesmo tempo
    # competindo pelo mesmo state.json e colocando ordens independentes.
    import fcntl
    os.makedirs("data", exist_ok=True)
    lock_path = os.path.join("data", "whale_trader.lock")
    try:
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
    except BlockingIOError:
        print("=" * 60)
        print("  ERRO: Outro whale_trader já está rodando.")
        print("  Para parar a instância anterior:")
        print(f"    kill $(cat {lock_path})")
        print("=" * 60)
        sys.exit(1)

    # Validacao de seguranca
    if args.live and not args.confirm:
        print("=" * 60)
        print("  AVISO: --live requer --confirm para usar capital real.")
        print("  Use --live --confirm para confirmar.")
        print("  Ou rode sem --live para modo dry-run (simulado).")
        print("=" * 60)
        lock_fd.close()
        sys.exit(0)

    # Reset de estado (antes de criar o bot)
    if args.reset_state:
        state_path = os.path.join("data", "whale_trader_state.json")
        if os.path.exists(state_path):
            # Alertar sobre ordens abertas no CLOB antes de apagar
            try:
                with open(state_path) as _sf:
                    _old_state = json.load(_sf)
                open_orders = [
                    p for p in _old_state.get("positions", [])
                    if p.get("status") in ("open", "pending") and p.get("order_id")
                ]
                if open_orders and args.live:
                    print(f"  ⚠️  ATENÇÃO: {len(open_orders)} ordem(ns) abertas no CLOB serão ÓRFÃS após reset!")
                    for p in open_orders:
                        print(f"     • {p.get('slug', '?')} | order_id={p.get('order_id', '?')[:20]}...")
                    print("  → Cancele-as manualmente em app.polymarket.com antes de continuar.")
                    print("  → Ou use Ctrl+C para abortar e cancelar as ordens primeiro.")
                    time.sleep(5)  # Pausa para o usuário ler o aviso
            except Exception:
                pass
            os.remove(state_path)
            print(f"✅ Estado apagado: {state_path}")
        else:
            print("ℹ️  Nenhum estado anterior encontrado.")
        print(f"   Bot iniciará do zero com capital=${args.capital:.2f} | trade=${args.trade_size:.2f}")

    dry_run = not args.live

    bot = WhaleTrader(
        capital=args.capital,
        trade_size=args.trade_size,
        max_positions=args.max_positions,
        min_whales=args.min_whales,
        min_whale_pnl=args.min_whale_pnl,
        max_price=args.max_price,
        dry_run=dry_run,
        whales_to_scan=args.whales,
        auto_sync=args.auto_sync,
        stop_loss=args.stop_loss,
        max_cost=args.max_cost,
        value_scan=args.value_scan,
    )

    try:
        if args.once:
            bot.run_cycle()
        else:
            bot.run_loop(interval_sec=args.interval)
    finally:
        # Liberar lock ao sair (garante que próxima instância possa iniciar)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            os.remove(lock_path)
        except Exception:
            pass


if __name__ == "__main__":
    main()
