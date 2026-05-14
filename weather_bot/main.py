"""
weather_bot/main.py
Ponto de entrada do Weather Bot — Polymarket Temperature Trading.

Uso:
  python -m weather_bot.main            # dry run, 4 ciclos/dia alinhados ao GFS
  python -m weather_bot.main --live     # modo live (requer .env com credenciais)
  python -m weather_bot.main --once     # roda 1 ciclo e encerra
  python -m weather_bot.main --interval 120  # ciclo a cada 2min (override manual)
  python -m weather_bot.main --align-model   # 4 ciclos/dia nos horários GFS (padrão)
"""

import os
import sys
import csv
import json
import time
import logging
import argparse
import requests
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple, Optional

# Garantir que o projeto raiz está no path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from weather_bot.fetcher     import fetch_weather_events, parse_event
from weather_bot.forecast    import get_ensemble_forecast, build_probability_distribution, get_model_consensus
from weather_bot.edge_finder import find_opportunities, rank_opportunities, MIN_EDGE
from weather_bot.executor    import execute_opportunity, kelly_size, _is_dry_run, TRADE_SIZE_USD, MAX_COST_USD
from weather_bot.smart_money import summarize_smart_money
from weather_bot.calibrator  import record_resolution, update_bias_report, get_calibration_summary
from bot.manual_override     import ManualOverrideManager

os.makedirs("data", exist_ok=True)
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/weather_bot.log", mode="a", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

LOG_FILE            = "data/weather_opportunities.csv"
STATE_FILE          = "data/weather_bot_state.json"
MAX_DAILY_LOSS         = float(os.getenv("WEATHER_MAX_DAILY_LOSS",         "15.00"))  # kill switch diário
MAX_OPEN_POSITIONS     = int(os.getenv("WEATHER_MAX_OPEN_POSITIONS",         "20"))     # cap de posições abertas
QUICK_SCAN_INTERVAL_S  = int(os.getenv("WEATHER_QUICK_SCAN_INTERVAL",        "2700"))   # 45 min entre scans intraday
CITY_WR_MIN_TRADES     = int(os.getenv("WEATHER_CITY_WR_MIN_TRADES",         "10"))     # trades mínimos p/ aplicar filtro WR
CITY_WR_PENALTY_THR    = float(os.getenv("WEATHER_CITY_WR_PENALTY_THR",      "0.35"))   # WR abaixo → penalidade
CITY_WR_BONUS_THR      = float(os.getenv("WEATHER_CITY_WR_BONUS_THR",        "0.60"))   # WR acima → bônus

GAMMA_API = "https://gamma-api.polymarket.com"


# ─── State management ────────────────────────────────────────────────────────

def load_state() -> Dict:
    """Carrega estado persistido (posições abertas + histórico de W/L + daily loss)."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            # Garante compatibilidade com estados mais antigos (sem campos daily)
            state.setdefault("daily_loss", 0.0)
            state.setdefault("day_started", "")
            return state
        except Exception as e:
            logger.warning(f"Erro ao carregar state: {e}")
    return {"positions": [], "wins": 0, "losses": 0, "daily_loss": 0.0, "day_started": ""}


def save_state(state: Dict):
    """Persiste estado em JSON."""
    state["saved_at"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def calculate_stats(state: Dict) -> Tuple[int, int, float]:
    """Retorna (wins, losses, win_rate%)."""
    wins   = state.get("wins", 0)
    losses = state.get("losses", 0)
    total  = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0.0
    return wins, losses, win_rate


def _check_daily_reset(state: Dict):
    """
    Reseta daily_loss se começou um novo dia (UTC).
    Chamado no início de cada ciclo para garantir janela de 24h correta.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("day_started") != today:
        prev_loss = state.get("daily_loss", 0.0)
        if state.get("day_started") and prev_loss > 0:
            logger.info(f"Novo dia ({today}) — resetando daily_loss (era ${prev_loss:.2f})")
        state["day_started"] = today
        state["daily_loss"]  = 0.0


# ─── Verificar desfechos de posições abertas ─────────────────────────────────

def _fetch_market_price(market_slug: str) -> Optional[float]:
    """
    Busca yes_price atual de um mercado pelo slug.
    Retorna None se não conseguir.
    """
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"slug": market_slug},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            market = data[0]
        elif isinstance(data, dict):
            market = data
        else:
            return None

        prices = market.get("outcomePrices", [])
        if isinstance(prices, str):
            import json as _json
            prices = _json.loads(prices)
        if prices:
            return float(prices[0])
    except Exception as e:
        logger.debug(f"Erro ao buscar preço {market_slug}: {e}")
    return None


def _get_portfolio_positions(client) -> Tuple[Dict[str, float], bool]:
    """
    Retorna ({token_id: size_shares}, sucesso) para posições abertas no CLOB.

    sucesso=False significa que a chamada falhou — não confundir com carteira vazia.
    Usado para detectar vendas manuais com precisão (vs. só verificar status da ordem).
    """
    try:
        raw = client.get_positions()
        result: Dict[str, float] = {}
        for pos in (raw or []):
            if isinstance(pos, dict):
                token_id = pos.get("asset_id") or pos.get("token_id", "")
                size = float(pos.get("size", 0) or 0)
            else:
                token_id = getattr(pos, "asset_id", "") or getattr(pos, "token_id", "")
                size = float(getattr(pos, "size", 0) or 0)
            if token_id and size > 0:
                result[token_id] = size
        return result, True
    except Exception as e:
        logger.debug(f"Portfolio CLOB não disponível: {e}")
        return {}, False


def check_position_outcomes(state: Dict) -> int:
    """
    Verifica posições abertas e marca WIN/LOSS quando o mercado resolve.

    Lógica de resolução:
      BUY_YES → yes_price ≥ 0.95 = WIN   | yes_price ≤ 0.05 = LOSS
      BUY_NO  → yes_price ≤ 0.05 = WIN   | yes_price ≥ 0.95 = LOSS

    Retorna número de posições resolvidas neste ciclo.
    """
    open_positions = [p for p in state["positions"] if p["status"] == "open"]
    if not open_positions:
        return 0

    resolved = 0
    for pos in open_positions:
        yes_price = _fetch_market_price(pos["market_slug"])
        if yes_price is None:
            continue

        action = pos["action"]
        outcome = None

        if action == "BUY_YES":
            if yes_price >= 0.95:
                outcome = "WIN"
            elif yes_price <= 0.05:
                outcome = "LOSS"
        else:  # BUY_NO
            if yes_price <= 0.05:
                outcome = "WIN"
            elif yes_price >= 0.95:
                outcome = "LOSS"

        if outcome:
            pos["status"]      = "closed"
            pos["outcome"]     = outcome
            pos["exit_price"]  = round(yes_price, 4)
            pos["closed_at"]   = datetime.now(timezone.utc).isoformat()

            if outcome == "WIN":
                state["wins"] += 1
            else:
                state["losses"] += 1
                loss_size = pos.get("size_usd", TRADE_SIZE_USD)
                state["daily_loss"] = round(state.get("daily_loss", 0.0) + loss_size, 2)

            # Ponto 1: registrar resolução para auto-calibração de bias
            record_resolution(pos)

            icon = "✅" if outcome == "WIN" else "❌"
            print(f"  {icon} RESOLVIDO: {pos['city']} {pos['label']} "
                  f"({action}) → {outcome} (yes={yes_price:.3f})")
            resolved += 1

    return resolved


def detect_manual_cancellations(state: Dict, override_manager: ManualOverrideManager) -> int:
    """
    Verifica posições abertas cujas ordens foram canceladas manualmente.

    Para cada posição "open" com order_id registrado, consulta o CLOB.
    Se a ordem não existe mais e o mercado ainda não resolveu (preço entre
    0.05 e 0.95), significa que o usuário cancelou/vendeu manualmente.
    Nesse caso, bloqueia o market_slug até a data de resolução do mercado.

    Returns:
        Número de cancelamentos manuais detectados neste ciclo.
    """
    if _is_dry_run():
        logger.debug(
            "detect_manual_cancellations: ignorado em dry run — "
            "use --live para ativar detecção de cancelamentos manuais"
        )
        return 0

    open_positions = [
        p for p in state["positions"]
        if p["status"] == "open" and p.get("order_id") and not p.get("order_id", "").startswith("dry_run_")
    ]
    if not open_positions:
        return 0

    # Inicializar CLOB client apenas uma vez
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        from py_clob_client.constants import POLYGON

        key            = os.getenv("PRIVATE_KEY")
        api_key        = os.getenv("API_KEY")
        api_secret     = os.getenv("API_SECRET")
        api_passphrase = os.getenv("API_PASSPHRASE")

        if not all([key, api_key, api_secret, api_passphrase]):
            return 0

        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=POLYGON,
            key=key,
            creds=ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase),
            signature_type=int(os.getenv("SIGNATURE_TYPE", "0")),
            funder=os.getenv("POLYMARKET_PROXY_ADDRESS") or None,
        )
    except Exception as e:
        logger.debug(f"CLOB não disponível para verificar ordens: {e}")
        return 0

    detected = 0

    # Tenta obter portfolio real do CLOB — método mais preciso para detectar vendas manuais.
    # Se falhar, cai no fallback de verificação de status da ordem.
    portfolio, portfolio_ok = _get_portfolio_positions(client)
    if portfolio_ok:
        logger.debug(f"Portfolio CLOB: {len(portfolio)} posição(ões) ativa(s)")
    else:
        logger.debug("Portfolio CLOB indisponível — fallback para verificação de ordem")

    for pos in open_positions:
        order_id   = pos["order_id"]
        slug       = pos["market_slug"]
        event_slug = pos.get("event_slug", "")
        city       = pos["city"]
        label      = pos.get("label", "")
        token_id   = pos.get("token_id", "")

        manual_sell_detected = False

        if portfolio_ok and token_id:
            # Método preciso: verifica se o usuário ainda possui shares do token.
            # Distingue corretamente: ordem preenchida (MATCHED) ≠ posição vendida.
            if portfolio.get(token_id, 0) > 0:
                continue  # Ainda possui shares → nenhuma intervenção manual

            # 0 shares: venda manual ou ordem nunca preenchida
            yes_price = _fetch_market_price(slug)
            if yes_price is not None and (yes_price >= 0.95 or yes_price <= 0.05):
                continue  # Mercado resolveu — check_position_outcomes vai tratar
            manual_sell_detected = True

        else:
            # Fallback: verificar status da ordem no CLOB.
            # MATCHED = ordem preenchida, posição ainda aberta → não é cancelamento.
            order_active = False
            try:
                order = client.get_order(order_id)
                if order:
                    status = (order.get("status", "") if isinstance(order, dict)
                              else getattr(order, "status", ""))
                    order_active = status.upper() in ("LIVE", "OPEN", "UNMATCHED", "MATCHED")
            except Exception:
                order_active = False

            if order_active:
                continue

            yes_price = _fetch_market_price(slug)
            if yes_price is not None and (yes_price >= 0.95 or yes_price <= 0.05):
                continue

            manual_sell_detected = True

        if not manual_sell_detected:
            continue

        print(
            f"\n  ⚠️  INTERVENÇÃO MANUAL detectada: {city} {label} ({slug})\n"
            f"  Posição não encontrada no CLOB/portfolio.\n"
            f"  Bloqueando re-entrada até o mercado resolver.\n"
        )

        # Bloquear market_slug
        override_manager.add_block(
            token_id=slug,
            reason=f"cancelamento/venda manual — {city} {label}",
            condition_id=None,
        )
        end_date, question = override_manager.fetch_market_info_by_slug(slug)
        if slug in override_manager._blocks:
            if end_date:
                override_manager._blocks[slug].market_end_date = end_date
            override_manager._blocks[slug].market_question = question or f"{city} {label}"
            override_manager._save()

        # Bloquear event_slug — previne reentrada em datas adjacentes do mesmo evento.
        # Ex: vendeu Madrid April 20 → bot não compra Madrid April 21, 22, etc.
        if event_slug and event_slug != slug:
            override_manager.add_block(
                token_id=event_slug,
                reason=f"bloqueio de evento — {city} (intervenção manual detectada)",
                condition_id=None,
            )
            end_date_ev, question_ev = override_manager.fetch_market_info_by_slug(event_slug)
            if event_slug in override_manager._blocks:
                if end_date_ev:
                    override_manager._blocks[event_slug].market_end_date = end_date_ev
                override_manager._blocks[event_slug].market_question = question_ev or city
                override_manager._save()
            print(f"  🚫 Evento inteiro bloqueado: {event_slug}")

        pos["status"]    = "closed"
        pos["outcome"]   = "MANUAL_CANCEL"
        pos["closed_at"] = datetime.now(timezone.utc).isoformat()
        detected += 1

    if detected:
        save_state(state)

    return detected


# ─── Ciclo principal ──────────────────────────────────────────────────────────

def _trade_icon(outcome: Optional[str], status: str) -> str:
    if status == "open":
        return "⏳"
    if outcome == "WIN":
        return "✅"
    if outcome == "LOSS":
        return "❌"
    return "⬜"


def _compute_city_edge_overrides(state: Dict) -> Dict[str, float]:
    """
    Calcula ajustes de edge mínimo por cidade com base no histórico de W/L.

    Cidades com WR < CITY_WR_PENALTY_THR após CITY_WR_MIN_TRADES fechados
    → +10% de edge mínimo (mais difícil entrar).

    Cidades com WR >= CITY_WR_BONUS_THR após CITY_WR_MIN_TRADES fechados
    → -3% de edge mínimo (leve recompensa para cidades confiáveis).
    """
    from collections import defaultdict
    city_outcomes: Dict[str, list] = defaultdict(list)

    for pos in state["positions"]:
        if pos["status"] != "closed" or pos.get("outcome") not in ("WIN", "LOSS"):
            continue
        city_outcomes[pos["city"]].append(pos["outcome"])

    overrides: Dict[str, float] = {}
    for city, outcomes in city_outcomes.items():
        n = len(outcomes)
        if n < CITY_WR_MIN_TRADES:
            continue
        wins = sum(1 for o in outcomes if o == "WIN")
        wr = wins / n
        if wr < CITY_WR_PENALTY_THR:
            overrides[city] = 0.10
            logger.info(f"City filter: {city} WR={wr:.0%} ({wins}/{n}) → +10% edge mín")
        elif wr >= CITY_WR_BONUS_THR:
            overrides[city] = -0.03
            logger.debug(f"City filter: {city} WR={wr:.0%} ({wins}/{n}) → -3% edge mín")

    return overrides


def _calculate_pnl(positions: list) -> Tuple[float, float, float]:
    """
    Retorna (pnl_total, avg_win, avg_loss) para posições fechadas com WIN ou LOSS.
    WIN profit  = size_usd / bet_price - size_usd  (payout - custo)
    LOSS profit = -size_usd
    """
    wins_pnl:  list = []
    losses_pnl: list = []
    for p in positions:
        if p["status"] != "closed":
            continue
        outcome   = p.get("outcome")
        size_usd  = p.get("size_usd", 0.0) or 0.0
        bet_price = p.get("bet_price", 0.0) or 0.0
        if outcome == "WIN" and bet_price > 0:
            wins_pnl.append(size_usd / bet_price - size_usd)
        elif outcome == "LOSS" and size_usd > 0:
            losses_pnl.append(-size_usd)

    total   = sum(wins_pnl) + sum(losses_pnl)
    avg_win = sum(wins_pnl)  / len(wins_pnl)  if wins_pnl  else 0.0
    avg_loss= sum(losses_pnl)/ len(losses_pnl) if losses_pnl else 0.0
    return round(total, 2), round(avg_win, 2), round(avg_loss, 2)


def _seconds_until_next_model_run() -> int:
    """
    Calcula segundos até o próximo slot de atualização do GFS/ECMWF.
    Slots: 00:30, 06:30, 12:30, 18:30 UTC (30 min após cada run de 6h do GFS).
    O offset de 30min dá tempo para os dados chegarem ao Open-Meteo.
    """
    now    = datetime.now(timezone.utc)
    slots  = [0, 6, 12, 18]  # horas UTC dos runs do GFS
    offset = 30              # minutos após o run para dados estarem disponíveis

    candidates = []
    for h in slots:
        slot_today = now.replace(hour=h, minute=offset, second=0, microsecond=0)
        if slot_today <= now:
            slot_today += timedelta(days=1)
        candidates.append(slot_today)

    next_slot = min(candidates)
    wait_secs = int((next_slot - now).total_seconds())
    logger.info(f"Próximo ciclo alinhado ao GFS: {next_slot.strftime('%Y-%m-%d %H:%M UTC')} "
                f"(em {wait_secs//3600}h {(wait_secs%3600)//60}min)")
    return wait_secs


def _print_dashboard(state: Dict, min_edge: float, cycle_start: datetime):
    """Imprime cabeçalho visual com stats, portfolio real e últimos trades."""
    wins, losses, win_rate = calculate_stats(state)
    daily_loss    = state.get("daily_loss", 0.0)
    open_pos      = [p for p in state["positions"] if p["status"] == "open"]
    closed_pos    = [p for p in state["positions"] if p["status"] == "closed"]

    # Win rate por direção (YES vs NO)
    yes_trades = [p for p in closed_pos if p["action"] == "BUY_YES"]
    no_trades  = [p for p in closed_pos if p["action"] == "BUY_NO"]
    yes_wins   = sum(1 for p in yes_trades if p.get("outcome") == "WIN")
    no_wins    = sum(1 for p in no_trades  if p.get("outcome") == "WIN")
    yes_wr     = f"{yes_wins}/{len(yes_trades)} ({yes_wins/len(yes_trades)*100:.0f}%)" if yes_trades else "—"
    no_wr      = f"{no_wins}/{len(no_trades)} ({no_wins/len(no_trades)*100:.0f}%)"   if no_trades  else "—"

    # Alerta se BUY_YES estiver abaixo de 40% win rate
    yes_alert = ""
    if len(yes_trades) >= 3 and yes_wins / len(yes_trades) < 0.40:
        yes_alert = "  ⚠️  BUY_YES abaixo de 40% WR — considere desativar"

    win_rate_str  = f"{win_rate:.0f}%" if wins + losses > 0 else "—"
    loss_bar      = f"${daily_loss:.2f}/${MAX_DAILY_LOSS:.2f}"
    loss_color    = "🔴" if daily_loss >= MAX_DAILY_LOSS * 0.75 else "📉"
    mode_str      = "🧪 DRY RUN" if _is_dry_run() else "🔴 LIVE"

    # P&L realizado (calculado a partir das posições fechadas)
    pnl_total, avg_win, avg_loss = _calculate_pnl(state["positions"])
    pnl_sign_str = "+" if pnl_total >= 0 else ""

    # Portfolio real (sincronizado manualmente após cada sessão)
    portfolio_usd = state.get("portfolio_usd", 0.0)
    cash_usd      = state.get("cash_usd", 0.0)
    alltime_pnl   = state.get("alltime_pnl", 0.0)
    pnl_sign      = "+" if alltime_pnl >= 0 else ""

    print(f"\n{'='*72}")
    print(f"  🌡️  WEATHER BOT | {mode_str} | {cycle_start.strftime('%H:%M:%S UTC')}")
    print(f"  Trade size: ${TRADE_SIZE_USD} | Max cost: ${MAX_COST_USD} | Edge mín: {min_edge*100:.0f}%")
    if portfolio_usd > 0:
        print(f"  💼 Portfolio: ${portfolio_usd:.2f} | 💵 Cash: ${cash_usd:.2f} | "
              f"PnL all-time: {pnl_sign}${alltime_pnl:.2f}")
    print(f"  {'─'*66}")
    print(f"  📈 Win Rate:  {wins}W / {losses}L ({win_rate_str})   "
          f"| {loss_color} Perda hoje: {loss_bar}")
    print(f"  🟢 BUY_YES:  {yes_wr:<20}  🔵 BUY_NO: {no_wr}{yes_alert}")
    print(f"  📂 Posições:  {len(open_pos)} abertas / {len(closed_pos)} fechadas "
          f"(cap: {MAX_OPEN_POSITIONS})")
    if wins + losses > 0:
        print(f"  💰 P&L:  {pnl_sign_str}${pnl_total:.2f}  "
              f"|  {wins}W avg +${avg_win:.2f}  "
              f"|  {losses}L avg -${abs(avg_loss):.2f}")

    # Calibração ativa por cidade
    calib_line = get_calibration_summary()
    if calib_line:
        print(calib_line)

    # Cidades com edge penalizado por WR baixo
    city_overrides = _compute_city_edge_overrides(state)
    penalty_cities = [c for c, v in city_overrides.items() if v > 0]
    if penalty_cities:
        print(f"  ⚠️  Edge elevado (WR < {CITY_WR_PENALTY_THR:.0%}): {', '.join(penalty_cities)}")

    print(f"  {'─'*66}")

    # Últimos 10 trades (fechados + abertos mais recentes)
    recent = sorted(
        state["positions"],
        key=lambda p: p.get("closed_at") or p.get("opened_at") or "",
        reverse=True
    )[:10]

    if recent:
        print(f"  ÚLTIMOS TRADES:")
        for p in recent:
            icon      = _trade_icon(p.get("outcome"), p["status"])
            city      = p["city"][:14]
            label     = p.get("label", "")[:7]
            action    = "YES" if p["action"] == "BUY_YES" else "NO "
            bet_price = p.get("bet_price") or 0.0
            size_usd  = p.get("size_usd")  or 0.0
            prize     = (size_usd / bet_price) if bet_price > 0 else 0.0
            ts        = (p.get("closed_at") or p.get("opened_at") or "")[:16].replace("T", " ")
            status_str = "ABERTA" if p["status"] == "open" else ""
            print(f"    {icon} {city:<14} {label:<7} BUY_{action}"
                  f" @ ${bet_price:.2f} → ${prize:.2f}"
                  f"   {ts}  {status_str}")

    print(f"{'='*72}")


def run_cycle(state: Dict, min_edge: float = 0.12, override_manager: Optional[ManualOverrideManager] = None) -> int:
    """
    Executa um ciclo completo: resolve posições → busca → forecast → edge → executa.
    Retorna número de trades executados.
    """
    cycle_start = datetime.now(timezone.utc)
    _check_daily_reset(state)
    _print_dashboard(state, min_edge, cycle_start)

    if override_manager is None:
        override_manager = ManualOverrideManager()

    # ── 0. Verificar desfechos de posições abertas ────────────────────────────
    # Ponto 3: calcular overrides de edge por cidade antes de qualquer trade
    city_edge_overrides = _compute_city_edge_overrides(state)

    open_count = sum(1 for p in state["positions"] if p["status"] == "open")
    if open_count > 0:
        print(f"\n  🔍 Verificando {open_count} posição(ões) abertas...")
        resolved = check_position_outcomes(state)
        if resolved:
            save_state(state)
            # Ponto 1: atualizar bias_report com novas resoluções
            update_bias_report()
            w, l, wr = calculate_stats(state)
            print(f"  📊 Atualizado: {w}W / {l}L ({wr:.0f}% win rate)")

        # ── 0a. Detectar cancelamentos manuais ────────────────────────────────
        manual_cancelled = detect_manual_cancellations(state, override_manager)
        if manual_cancelled:
            print(f"  🚫 {manual_cancelled} posição(ões) bloqueada(s) por intervenção manual.")

    # ── 0b. Daily kill switch ─────────────────────────────────────────────────
    daily_loss = state.get("daily_loss", 0.0)
    if daily_loss >= MAX_DAILY_LOSS:
        print(f"\n  🛑 KILL SWITCH DIÁRIO ativado!")
        print(f"  Perda acumulada hoje: ${daily_loss:.2f} ≥ limite ${MAX_DAILY_LOSS:.2f}")
        print(f"  Trading suspenso até amanhã (UTC). Use --reset-state para forçar reinício.\n")
        save_state(state)
        return 0

    # ── 0c. Cap de posições abertas ───────────────────────────────────────────
    current_open = sum(1 for p in state["positions"] if p["status"] == "open")
    if current_open >= MAX_OPEN_POSITIONS:
        print(f"\n  ⏸️  CAP DE POSIÇÕES: {current_open}/{MAX_OPEN_POSITIONS} abertas.")
        print(f"  Aguardando resolução antes de abrir novas ordens.\n")
        save_state(state)
        return 0

    # ── 1. Buscar eventos ─────────────────────────────────────────────────────
    print("\n  Buscando eventos de temperatura no Polymarket (tag_id=84)...")
    raw_events = fetch_weather_events()
    events     = [e for e in (parse_event(r) for r in raw_events) if e]
    print(f"  {len(events)} eventos de temperatura parseados\n")

    if not events:
        print("  Nenhum evento disponível. Tentando novamente no próximo ciclo.")
        return 0

    # ── 2. Forecast + edge por cidade ────────────────────────────────────────
    all_opportunities: List[Dict] = []
    forecast_ok    = 0
    forecast_fail  = 0
    consensus_high = 0
    consensus_med  = 0
    consensus_low  = 0
    consensus_na   = 0
    missing_cities: set = set()

    for event in events:
        city = event["city"]
        date = event["date"]

        forecast = get_ensemble_forecast(city, date)
        if forecast is None:
            forecast_fail += 1
            missing_cities.add(city)
            continue

        # v8: cross-check multi-modelo (GFS + ECMWF + ICON)
        consensus = get_model_consensus(city, date)
        if consensus is None:
            consensus_na += 1
        elif consensus["agreement"] == "HIGH":
            consensus_high += 1
        elif consensus["agreement"] == "MEDIUM":
            consensus_med += 1
        else:
            consensus_low += 1

        forecast_ok += 1
        market_temps = [m["temp_value"] for m in event["markets"]]
        prob_dist    = build_probability_distribution(forecast, market_temps)
        opps         = find_opportunities(event, forecast, prob_dist, min_edge=min_edge, consensus=consensus, city_edge_overrides=city_edge_overrides)
        all_opportunities.extend(opps)

    print(f"  Forecasts: ✅ {forecast_ok} cidades | ❌ {forecast_fail} sem coordenadas")
    print(f"  Cross-check: 🟢 {consensus_high} HIGH | 🟡 {consensus_med} MEDIUM | 🔴 {consensus_low} LOW | ⚪ {consensus_na} indisponível")
    if missing_cities:
        logger.debug(f"Cidades sem coordenadas: {sorted(missing_cities)}")

    # ── 3. Ranking e display ──────────────────────────────────────────────────
    ranked = rank_opportunities(all_opportunities)

    print(f"\n  📊 OPORTUNIDADES (edge > {min_edge*100:.0f}%): {len(ranked)}")

    if not ranked:
        print("  Nenhuma oportunidade com edge suficiente neste ciclo.\n")
        return 0

    # Slugs já posicionados (para marcar na tabela)
    open_slugs = {
        p["market_slug"] for p in state["positions"]
        if p["status"] == "open"
    }

    # Tabela de oportunidades (com cross-check multi-modelo, distância e horas)
    header = (
        f"  {'Cidade':<20} {'Data':<12} {'Temp':<10} {'Forecast':>9} {'Mercado':>9} "
        f"{'Edge':>8}  {'Ação':<10} {'Dist':>5} {'Hrs':>5}     {'Sinal':<7} Score"
    )
    print(f"\n{header}")
    print(f"  {'-'*116}")
    agreement_icon = {"HIGH": "🟢 OK", "MEDIUM": "🟡 med"}
    for opp in ranked[:20]:
        marker    = " 🔒 JÁ POSICIONADO" if opp["market_slug"] in open_slugs else ""
        signal    = agreement_icon.get(opp.get("model_agreement", ""), "—")
        dist_str  = f"{opp.get('forecast_dist', 0):.1f}°"
        hrs_str   = f"{opp.get('hours_to_resolution', 0):.0f}h"
        sm        = opp.get("smart_money", {})
        sm_icon   = sm.get("icon", "⚪")
        print(
            f"  {opp['city']:<20} {opp['date']:<12} {opp['label']:<10} "
            f"{opp['forecast_prob']*100:>7.1f}%  {opp['market_prob']*100:>7.1f}%  "
            f"{opp['edge']*100:>+6.1f}%  {opp['action']:<10} "
            f"{dist_str:>5} {hrs_str:>5} {sm_icon:<5} {signal:<7} {opp['score']:.1f}{marker}"
        )

    # Sumário de smart money para o ciclo
    sm_summary = summarize_smart_money(ranked)
    if sm_summary:
        print(sm_summary)

    # ── 4. Executar top N oportunidades ──────────────────────────────────────
    print(f"\n  Executando melhores oportunidades...")
    trades_done    = 0
    trades_skipped = 0
    traded_slugs   = set()

    # Slugs já abertos (não re-entrar no mesmo mercado)
    already_open = {
        p["market_slug"] for p in state["positions"]
        if p["status"] == "open"
    }

    # Proteção contra duplicatas: consulta CLOB real antes de qualquer ordem.
    # Cobre o caso de reinício do bot com state vazio mas posições já existentes.
    clob_token_ids: set = set()
    if not _is_dry_run():
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            from py_clob_client.constants import POLYGON
            _clob = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=POLYGON,
                key=os.getenv("PRIVATE_KEY"),
                creds=ApiCreds(
                    api_key=os.getenv("API_KEY"),
                    api_secret=os.getenv("API_SECRET"),
                    api_passphrase=os.getenv("API_PASSPHRASE"),
                ),
                signature_type=int(os.getenv("SIGNATURE_TYPE", "0")),
                funder=os.getenv("POLYMARKET_PROXY_ADDRESS") or None,
            )
            portfolio, ok = _get_portfolio_positions(_clob)
            if ok:
                clob_token_ids = set(portfolio.keys())
                if clob_token_ids:
                    logger.info(f"CLOB portfolio: {len(clob_token_ids)} token(s) com shares — skip duplicatas")
        except Exception as _e:
            logger.debug(f"Não foi possível checar portfolio CLOB: {_e}")

    # Item 2: um único trade por cidade+data por ciclo.
    # Buckets do mesmo evento são mutuamente exclusivos — operar mais de um
    # cria posições correlacionadas e infla o portfolio artificialmente.
    # A oportunidade de melhor score para cada evento já está no topo do ranking.
    traded_events: set = set()

    # Também bloquear eventos que já têm posição aberta no state (cobre reinícios).
    open_events = {
        (p["city"], p["date"])
        for p in state["positions"]
        if p["status"] == "open"
    }

    for opp in ranked:
        slug       = opp["market_slug"]
        event_key  = (opp["city"], opp["date"])

        # Nunca duas ordens no mesmo mercado por ciclo
        if slug in traded_slugs:
            continue

        # Item 2: nunca dois trades no mesmo evento (cidade+data) por ciclo
        if event_key in traded_events:
            logger.debug(
                f"SKIP {opp['city']} {opp['date']} {opp['label']} — "
                f"evento já operado neste ciclo (1 trade/evento)"
            )
            trades_skipped += 1
            continue

        # Não operar evento onde já temos posição aberta
        if event_key in open_events:
            logger.debug(
                f"SKIP {opp['city']} {opp['date']} {opp['label']} — "
                f"evento já tem posição aberta no state"
            )
            trades_skipped += 1
            continue

        # Não re-entrar em mercado já aberto (state local)
        if slug in already_open:
            print(f"  ⏭️  Pulando {opp['city']} {opp['label']} — já posicionado (state)")
            trades_skipped += 1
            continue

        # Não re-entrar se token já está no CLOB (proteção contra reinício com state vazio)
        token_id_check = opp.get("token_id", "")
        if token_id_check and token_id_check in clob_token_ids:
            print(f"  ⏭️  Pulando {opp['city']} {opp['label']} — shares já existem no CLOB")
            already_open.add(slug)
            trades_skipped += 1
            continue

        # Não re-entrar em mercado bloqueado por intervenção manual.
        # Verifica market_slug E event_slug — bloquear o evento inteiro cobre datas adjacentes.
        blocked, block_reason = override_manager.is_blocked(slug)
        if not blocked:
            event_slug_check = opp.get("event_slug", "")
            if event_slug_check:
                blocked, block_reason = override_manager.is_blocked(event_slug_check)
        if blocked:
            print(f"  🚫 Pulando {opp['city']} {opp['label']} — {block_reason}")
            trades_skipped += 1
            continue

        order_id = execute_opportunity(opp)
        if order_id:
            trades_done += 1
            traded_slugs.add(slug)
            traded_events.add(event_key)
            open_events.add(event_key)
            already_open.add(slug)
            _log_to_csv(opp)
            _register_position(state, opp, order_id=order_id)

        if trades_done >= int(os.getenv("WEATHER_MAX_TRADES_CYCLE", "5")):
            break

    if trades_done > 0:
        save_state(state)

    elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
    skip_str = f" | {trades_skipped} pulado(s)" if trades_skipped > 0 else ""
    print(f"\n  ✅ {trades_done} trade(s) novo(s){skip_str} | ciclo em {elapsed:.1f}s\n")
    return trades_done


# ─── Registrar posição aberta no state ───────────────────────────────────────

def _register_position(state: Dict, opp: Dict, order_id: Optional[str] = None):
    """Adiciona posição ao state para rastreamento de W/L, daily kill switch e detecção manual."""
    clean_order_id = order_id or ""
    if not clean_order_id and not _is_dry_run():
        logger.warning(
            f"Posição registrada sem order_id: {opp.get('city')} {opp.get('label')} "
            f"({opp.get('market_slug')}) — detecção de venda manual via CLOB não funcionará"
        )
    state["positions"].append({
        "market_slug":    opp["market_slug"],
        "event_slug":     opp["event_slug"],
        "city":           opp["city"],
        "date":           opp["date"],
        "label":          opp.get("label"),
        "temp_value":     opp.get("temp_value"),
        "action":         opp["action"],
        "bet_price":      opp["bet_price"],
        "size_usd":       kelly_size(opp),
        "forecast_prob":  opp["forecast_prob"],
        "market_prob":    opp["market_prob"],
        "edge":           opp["edge"],
        "forecast_mean":  opp.get("forecast_mean"),   # para auto-calibração de bias
        "order_id":       clean_order_id,
        "token_id":       opp.get("token_id", ""),    # para detecção precisa via portfolio CLOB
        "status":         "open",
        "outcome":        None,
        "exit_price":     None,
        "opened_at":      datetime.now(timezone.utc).isoformat(),
        "closed_at":      None,
    })


# ─── Log CSV ─────────────────────────────────────────────────────────────────

def _log_to_csv(opp: Dict):
    """Salva oportunidade no CSV para análise e backtesting posterior."""
    file_exists = os.path.exists(LOG_FILE)
    fieldnames = [
        "timestamp", "city", "date", "label", "temp_type",
        "action", "bet_price", "forecast_prob", "market_prob", "edge",
        "forecast_mean", "forecast_stdev", "forecast_p10", "forecast_p50", "forecast_p90",
        "forecast_members", "liquidity", "volume", "score",
        "city_reliability", "city_reliability_original", "model_agreement", "explore_mode",
        "event_slug", "market_slug",
    ]
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **{k: opp.get(k) for k in fieldnames if k != "timestamp"},
        })


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Weather Bot — Polymarket Temperature Trading")
    parser.add_argument("--live",        action="store_true", help="Modo live (padrão: dry run)")
    parser.add_argument("--once",        action="store_true", help="Roda 1 ciclo e encerra")
    parser.add_argument("--interval",     type=int,   default=None, help="Intervalo fixo entre ciclos (segundos); ignora --align-model")
    parser.add_argument("--align-model",  action="store_true", default=True, help="4 ciclos/dia nos horários GFS: 00:30, 06:30, 12:30, 18:30 UTC (padrão)")
    parser.add_argument("--min-edge",     type=float, default=MIN_EDGE, help=f"Edge mínimo (padrão: {MIN_EDGE*100:.0f}%%)")
    parser.add_argument("--reset-state", action="store_true", help="Apaga state e começa do zero")
    args = parser.parse_args()

    if args.live:
        from weather_bot.edge_finder import EXPLORE_MODE
        if EXPLORE_MODE:
            print("\n" + "!"*68)
            print("  🚫 EXPLORE MODE está ativo (WEATHER_EXPLORE_MODE=true).")
            print("  Thresholds relaxados são incompatíveis com trading real.")
            print("  Remova WEATHER_EXPLORE_MODE do .env antes de usar --live.")
            print("!"*68)
            sys.exit(1)
        # Proteção contra live acidental: exige confirmação explícita do usuário.
        print("\n" + "!"*68)
        print("  ⚠️  ATENÇÃO: você está prestes a entrar em LIVE TRADING REAL.")
        print("  Ordens reais serão colocadas. Dinheiro real será arriscado.")
        print("  Portfolio atual: verifique polymarket.com antes de confirmar.")
        print("!"*68)
        confirm = input("\n  Digite CONFIRMO para prosseguir (qualquer outra coisa cancela): ").strip()
        if confirm != "CONFIRMO":
            print("  Cancelado. Rodando em DRY RUN (seguro).")
            os.environ["WEATHER_DRY_RUN"] = "true"
            os.environ["WEATHER_LIVE_CONFIRMED"] = "false"
        else:
            os.environ["WEATHER_DRY_RUN"] = "true"        # mantém true
            os.environ["WEATHER_LIVE_CONFIRMED"] = "true"  # só este flag ativa live
            print("  ✅ Modo LIVE ativado.\n")
    else:
        # Sem --live: sempre dry run, independente do .env
        os.environ["WEATHER_DRY_RUN"] = "true"
        os.environ["WEATHER_LIVE_CONFIRMED"] = "false"

    if args.reset_state and os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
        print(f"✅ State apagado: {STATE_FILE}")

    state = load_state()

    wins, losses, win_rate = calculate_stats(state)
    open_pos = sum(1 for p in state["positions"] if p["status"] == "open")

    # Portfolio real (salvo no state após cada sync manual)
    portfolio_usd = state.get("portfolio_usd", 0.0)
    cash_usd      = state.get("cash_usd", 0.0)
    alltime_pnl   = state.get("alltime_pnl", 0.0)
    pnl_sign      = "+" if alltime_pnl >= 0 else ""

    print("\n" + "="*68)
    print("  🌡️  WEATHER BOT — Polymarket Temperature Trading")
    print(f"  Modo:       {'🧪 DRY RUN (simulado)' if _is_dry_run() else '🔴 LIVE TRADING'}")
    cycle_mode = f"alinhado GFS (4x/dia)" if args.interval is None else f"{args.interval}s"
    print(f"  Trade size: ${TRADE_SIZE_USD} | Max cost: ${MAX_COST_USD}")
    print(f"  Edge mín:   {args.min_edge*100:.0f}% | Ciclo: {cycle_mode}")
    if portfolio_usd > 0:
        print(f"  💼 Portfolio: ${portfolio_usd:.2f} | 💵 Cash: ${cash_usd:.2f} | "
              f"📊 PnL all-time: {pnl_sign}${alltime_pnl:.2f}")
    if wins + losses > 0:
        print(f"  📈 Win Rate: {wins}W / {losses}L ({win_rate:.0f}%) | {open_pos} aberta(s)")
    else:
        print(f"  📈 Win Rate: — (sem histórico) | {open_pos} aberta(s)")
    print("="*68)

    # Instância única do override_manager — persiste bloqueios entre ciclos
    override_manager = ManualOverrideManager()
    active_blocks = override_manager.list_blocks()
    if active_blocks:
        print(f"\n  🚫 {len(active_blocks)} mercado(s) bloqueado(s) por intervenção manual:")
        for b in active_blocks:
            exp = b.market_end_date[:10] if b.market_end_date else "indefinido"
            print(f"     • {b.market_question or b.token_id} — expira: {exp}")

    if args.once:
        run_cycle(state, min_edge=args.min_edge, override_manager=override_manager)
        return

    while True:
        try:
            run_cycle(state, min_edge=args.min_edge, override_manager=override_manager)

            if args.interval is not None:
                # Modo legado: intervalo fixo manual
                print(f"  Próximo ciclo em {args.interval}s... (Ctrl+C para parar)")
                time.sleep(args.interval)
            else:
                # Ponto 2: quick scan intraday
                # Dorme o menor valor entre: próximo GFS ou QUICK_SCAN_INTERVAL_S.
                # O forecast fica em cache por 6h → quick scans usam preços frescos
                # do Polymarket sem consumir cota da API de forecast.
                wait_gfs  = _seconds_until_next_model_run()
                wait_next = min(wait_gfs, QUICK_SCAN_INTERVAL_S) if QUICK_SCAN_INTERVAL_S > 0 else wait_gfs
                next_dt     = datetime.now(timezone.utc) + timedelta(seconds=wait_next)
                next_gfs_dt = datetime.now(timezone.utc) + timedelta(seconds=wait_gfs)

                if wait_next < wait_gfs:
                    print(
                        f"  🔍 Quick scan (preços frescos): {next_dt.strftime('%H:%M UTC')} "
                        f"({wait_next//60}min)  |  GFS: {next_gfs_dt.strftime('%H:%M UTC')} — Ctrl+C para parar"
                    )
                else:
                    print(
                        f"  Próximo ciclo GFS: {next_gfs_dt.strftime('%H:%M UTC')} "
                        f"(em {wait_gfs//3600}h {(wait_gfs%3600)//60}min) — Ctrl+C para parar"
                    )
                deadline = datetime.now(timezone.utc) + timedelta(seconds=wait_next)
                while True:
                    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
                    if remaining <= 0:
                        break
                    time.sleep(min(remaining, 60))

        except KeyboardInterrupt:
            print("\n\n  Weather Bot encerrado.")
            break
        except Exception as e:
            logger.error(f"Erro no ciclo: {e}", exc_info=True)
            time.sleep(30)


if __name__ == "__main__":
    main()
