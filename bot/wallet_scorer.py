"""
bot/wallet_scorer.py — Módulo de qualidade de whales via Polymarket Analytics.

Substitui o Polyman.fun (que exige autenticação) por Polymarket Analytics
(API pública, sem auth, win rate de posições FECHADAS).

Scoring system (0-100):
  - Win Rate (posições fechadas):  30 pts max
  - PnL total:                     20 pts max
  - Sample Size (trades fechados): 20 pts max
  - Zombie Ratio (baixo = bom):    15 pts max
  - Diversificação (tags):         15 pts max

Manual exige Score >= 80 (Polyman). Nosso equivalente: >= 60 (calibrado).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://polymarketanalytics.com/api"
HEADERS = {
    "User-Agent": "WhaleTraderV3/1.0 (Polymarket Bot)",
    "Accept": "application/json",
}


@dataclass
class WalletScore:
    """Score de qualidade de uma wallet."""
    address: str
    name: str
    win_rate: float          # 0.0–1.0 (posições fechadas)
    wins: int
    losses: int
    total_closed: int
    total_pnl: float         # USDC
    active_positions: int
    zombie_ratio: float      # active / (active + closed), 0=bom, 1=zombie
    tags: str
    score: int               # 0–100 (calculado)
    tier: str                # "TIER1", "TIER2", "BLOCKED"
    reason: str              # Motivo do tier


def fetch_wallet_stats(address: str) -> Optional[Dict]:
    """Busca stats reais de uma wallet via Polymarket Analytics API (grátis, sem auth)."""
    try:
        resp = requests.get(
            f"{API_BASE}/traders-tag-performance",
            params={
                "tag": "Overall",
                "sortDirection": "ASC",
                "limit": 100,
                "offset": 0,
                "searchQuery": address.lower(),
            },
            headers=HEADERS,
            timeout=15,
        )
        data = resp.json()
        if data.get("data") and len(data["data"]) > 0:
            return data["data"][0]
        return None
    except Exception as e:
        logger.warning(f"Erro ao buscar stats de {address[:10]}...: {e}")
        return None


def calculate_score(stats: Dict) -> WalletScore:
    """
    Calcula score 0-100 baseado em dados reais de posições fechadas.

    Componentes (total = 100):
      Win Rate (30 pts): 55%+=30, 53%+=20, 50%+=10, <50%=0
      PnL (20 pts):      >$1M=20, >$500k=15, >$100k=10, >$0=5, <$0=0
      Sample Size (20):  >100 closed=20, >50=15, >20=10, <20=5
      Zombie (15 pts):   <10% zombie=15, <25%=10, <50%=5, >50%=0
      Diversity (15):    Has multi-category tags=15, sports only=10, none=5
    """
    address = stats.get("trader", "")
    name = stats.get("trader_name", address[:10])

    wr = stats.get("win_rate", 0)
    wins = stats.get("win_count", 0)
    total = stats.get("total_positions", 0)
    losses = total - wins
    pnl = stats.get("overall_gain", 0)
    active = stats.get("active_positions", 0)
    tags = stats.get("trader_tags", "")

    # Zombie ratio: active / (active + closed)
    total_all = active + total
    zombie = active / total_all if total_all > 0 else 1.0

    # ── Score: Win Rate (max 30) ──
    if wr >= 0.55:
        s_wr = 30
    elif wr >= 0.53:
        s_wr = 20
    elif wr >= 0.50:
        s_wr = 10
    else:
        s_wr = 0

    # ── Score: PnL (max 20) ──
    if pnl > 1_000_000:
        s_pnl = 20
    elif pnl > 500_000:
        s_pnl = 15
    elif pnl > 100_000:
        s_pnl = 10
    elif pnl > 0:
        s_pnl = 5
    else:
        s_pnl = 0  # PnL negativo = 0 pontos

    # ── Score: Sample Size (max 20) ──
    if total >= 100:
        s_sample = 20
    elif total >= 50:
        s_sample = 15
    elif total >= 20:
        s_sample = 10
    else:
        s_sample = 5  # Amostra pequena, não confiável

    # ── Score: Zombie Ratio (max 15) ──
    if zombie < 0.10:
        s_zombie = 15
    elif zombie < 0.25:
        s_zombie = 10
    elif zombie < 0.50:
        s_zombie = 5
    else:
        s_zombie = 0  # >50% posições abertas = zombie whale

    # ── Score: Diversificação (max 15) ──
    tags_lower = tags.lower()
    has_sports = "sports" in tags_lower
    has_politics = "politic" in tags_lower or "overall" in tags_lower
    has_crypto = "crypto" in tags_lower
    categories = sum([has_sports, has_politics, has_crypto])
    if categories >= 2:
        s_div = 15  # Multi-categoria = melhor sinal
    elif categories == 1:
        s_div = 10
    else:
        s_div = 5

    total_score = s_wr + s_pnl + s_sample + s_zombie + s_div

    # ── Tier assignment ──
    # BLOCKED reasons (hard blocks):
    if pnl < 0:
        tier = "BLOCKED"
        reason = f"PnL NEGATIVO (${pnl:,.0f})"
    elif wr < 0.50:
        tier = "BLOCKED"
        reason = f"WR {wr:.1%} < 50% (pior que moeda)"
    elif total < 5:
        tier = "BLOCKED"
        reason = f"Só {total} trades fechados (amostra insuficiente)"
    elif zombie > 0.90:
        tier = "BLOCKED"
        reason = f"Zombie ratio {zombie:.0%} (quase tudo aberto)"
    elif total_score >= 60:
        tier = "TIER1"
        reason = f"Score {total_score}: WR={s_wr} PnL={s_pnl} Sample={s_sample} Zombie={s_zombie} Div={s_div}"
    elif total_score >= 40:
        tier = "TIER2"
        reason = f"Score {total_score}: WR={s_wr} PnL={s_pnl} Sample={s_sample} Zombie={s_zombie} Div={s_div}"
    else:
        tier = "BLOCKED"
        reason = f"Score {total_score} < 40 (qualidade insuficiente)"

    return WalletScore(
        address=address,
        name=name,
        win_rate=wr,
        wins=wins,
        losses=losses,
        total_closed=total,
        total_pnl=pnl,
        active_positions=active,
        zombie_ratio=zombie,
        tags=tags,
        score=total_score,
        tier=tier,
        reason=reason,
    )


def audit_whale_list(whale_list: List[Dict], verbose: bool = True) -> Dict[str, WalletScore]:
    """
    Audita todas as whales da lista usando Polymarket Analytics.
    Retorna dict {address: WalletScore}.
    """
    results = {}

    if verbose:
        print("\n  🔍 AUDITORIA DE QUALIDADE DAS WHALES (Polymarket Analytics)")
        print("  " + "=" * 75)
        print(f"  {'Whale':<22} {'WR':>6} {'W/L':>10} {'PnL':>12} {'Zombie':>8} {'Score':>6} {'Tier':>8}")
        print("  " + "-" * 75)

    for w in whale_list:
        stats = fetch_wallet_stats(w["address"])
        if stats:
            ws = calculate_score(stats)
        else:
            ws = WalletScore(
                address=w["address"],
                name=w.get("name", w["address"][:10]),
                win_rate=0, wins=0, losses=0, total_closed=0,
                total_pnl=0, active_positions=0, zombie_ratio=1.0,
                tags="", score=0, tier="BLOCKED",
                reason="API sem dados para esta wallet",
            )

        results[w["address"].lower()] = ws

        if verbose:
            tier_icon = {"TIER1": "✅", "TIER2": "⚠️", "BLOCKED": "❌"}.get(ws.tier, "?")
            print(
                f"  {ws.name:<22} {ws.win_rate:>5.1%} "
                f"{ws.wins:>4}/{ws.losses:<4} "
                f"${ws.total_pnl:>10,.0f} "
                f"{ws.zombie_ratio:>7.0%} "
                f"{ws.score:>5}/100 "
                f"{tier_icon} {ws.tier}"
            )

        time.sleep(0.3)  # Rate limit

    if verbose:
        print("  " + "-" * 75)
        t1 = sum(1 for ws in results.values() if ws.tier == "TIER1")
        t2 = sum(1 for ws in results.values() if ws.tier == "TIER2")
        blocked = sum(1 for ws in results.values() if ws.tier == "BLOCKED")
        print(f"  📊 TIER1: {t1} | TIER2: {t2} | BLOCKED: {blocked}")
        print(f"     (Só TIER1 e TIER2 serão seguidos. BLOCKED = eliminado.)")
        print()

    return results


def filter_quality_whales(
    whale_list: List[Dict],
    scores: Dict[str, WalletScore],
    min_tier: str = "TIER2",
) -> List[Dict]:
    """
    Filtra a whale_list mantendo apenas whales com tier >= min_tier.

    min_tier="TIER1" → só as melhores
    min_tier="TIER2" → TIER1 + TIER2 (padrão recomendado)
    """
    allowed = {"TIER1"} if min_tier == "TIER1" else {"TIER1", "TIER2"}

    filtered = []
    for w in whale_list:
        addr = w["address"].lower()
        ws = scores.get(addr)
        if ws and ws.tier in allowed:
            filtered.append(w)

    return filtered
