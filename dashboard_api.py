"""
dashboard_api.py — FastAPI backend para o Trader Joe Dashboard.

Endpoints:
- Saldo, ordens abertas, trades recentes
- Colocar e cancelar limit orders
- Fila de aprovação manual (bot sugere → você aprova/rejeita no dashboard)
- Buscar mercados e orderbook
"""

from __future__ import annotations

import math
import os
import uuid
from datetime import datetime
from typing import Optional

import requests as req
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

load_dotenv()

app = FastAPI(title="Trader Joe Dashboard", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "")

# Fila de ordens pendentes (em memória — suficiente para uso pessoal)
_pending_orders: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def verify_secret(x_dashboard_secret: Optional[str] = Header(None)) -> None:
    if DASHBOARD_SECRET and x_dashboard_secret != DASHBOARD_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# CLOB client factory
# ---------------------------------------------------------------------------

def _make_client() -> ClobClient:
    creds = ApiCreds(
        api_key=os.getenv("API_KEY", ""),
        api_secret=os.getenv("API_SECRET", ""),
        api_passphrase=os.getenv("API_PASSPHRASE", ""),
    )
    kwargs: dict = {
        "host": os.getenv("CLOB_HOST", "https://clob.polymarket.com"),
        "chain_id": int(os.getenv("CHAIN_ID", "137")),
        "key": os.getenv("PRIVATE_KEY", ""),
        "creds": creds,
        "signature_type": int(os.getenv("SIGNATURE_TYPE", "0")),
    }
    proxy = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
    if proxy:
        kwargs["funder"] = proxy
    return ClobClient(**kwargs)


def _order_to_dict(o) -> dict:
    if isinstance(o, dict):
        return o
    return {
        "id": getattr(o, "id", ""),
        "asset_id": getattr(o, "asset_id", ""),
        "side": getattr(o, "side", ""),
        "price": str(getattr(o, "price", "")),
        "original_size": str(getattr(o, "original_size", "")),
        "size_matched": str(getattr(o, "size_matched", "")),
        "status": getattr(o, "status", ""),
        "created_at": str(getattr(o, "created_at", "")),
    }


def _trade_to_dict(t) -> dict:
    if isinstance(t, dict):
        return t
    return vars(t) if hasattr(t, "__dict__") else {"raw": str(t)}


# ---------------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_polymarket.html")
    with open(html_path, encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ---------------------------------------------------------------------------
# Conta
# ---------------------------------------------------------------------------

@app.get("/api/balance")
async def get_balance(_: None = Depends(verify_secret)):
    try:
        client = _make_client()
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        bal = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        balance = int(bal.get("balance", "0")) / 1e6
        allowance = int(bal.get("allowance", "0")) / 1e6
        return {"balance_usdc": round(balance, 2), "allowance_usdc": round(allowance, 2)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/orders")
async def get_orders(_: None = Depends(verify_secret)):
    try:
        client = _make_client()
        orders = client.get_orders() or []
        return {"orders": [_order_to_dict(o) for o in orders]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/trades")
async def get_trades(_: None = Depends(verify_secret)):
    try:
        client = _make_client()
        wallet = os.getenv("WALLET_ADDRESS", "")

        # Tenta com maker_address (necessário em algumas versões do CLOB)
        trades = []
        try:
            from py_clob_client.clob_types import TradeParams
            params = TradeParams(maker_address=wallet) if wallet else TradeParams()
            trades = client.get_trades(params) or []
        except Exception:
            trades = client.get_trades() or []

        return {"trades": [_trade_to_dict(t) for t in trades[:50]]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Ordens
# ---------------------------------------------------------------------------

def _execute_order(token_id: str, side: str, price: float, size: float) -> dict:
    """Executa uma limit order no CLOB. Retorna dict com order_id."""
    price = max(0.01, min(0.99, price))
    size  = max(1.0, math.ceil(size))

    def _try(fee_bps: int = 0):
        client = _make_client()
        args = OrderArgs(token_id=token_id, price=price, size=size, side=side, fee_rate_bps=fee_bps)
        signed   = client.create_order(args)
        response = client.post_order(signed, OrderType.GTC)
        order_id = response.get("orderID", "") if isinstance(response, dict) else getattr(response, "orderID", "")
        return {"order_id": order_id, "side": side, "price": price, "size": size, "cost_usd": round(size * price, 2)}

    try:
        return _try(0)
    except Exception as e:
        err = str(e)
        if "invalid fee rate" in err and "maker fee" in err:
            import re
            m = re.search(r"maker fee[:\s]+(\d+)", err)
            if m:
                return _try(int(m.group(1)))
        raise


class OrderRequest(BaseModel):
    token_id: str
    side: str
    price: float
    size: float


@app.post("/api/order")
async def place_order(order: OrderRequest, _: None = Depends(verify_secret)):
    side = order.side.upper()
    if side not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="side deve ser BUY ou SELL")
    try:
        result = _execute_order(order.token_id, side, order.price, order.size)
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/order/{order_id}")
async def cancel_order(order_id: str, _: None = Depends(verify_secret)):
    try:
        client = _make_client()
        response = client.cancel_order(order_id)
        return {"success": True, "response": str(response)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/orders/all")
async def cancel_all_orders(_: None = Depends(verify_secret)):
    try:
        client = _make_client()
        response = client.cancel_all()
        return {"success": True, "response": str(response)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Fila de aprovação manual
# ---------------------------------------------------------------------------

class SuggestRequest(BaseModel):
    token_id: str
    side: str
    price: float
    size: float
    strategy: str = ""   # nome da estratégia que sugeriu
    reason: str   = ""   # motivo (ex: "RSI oversold @ 0.42")
    market_name: str = ""


@app.post("/api/order/suggest")
async def suggest_order(order: SuggestRequest, _: None = Depends(verify_secret)):
    """Bot chama este endpoint para sugerir uma ordem — você aprova no dashboard."""
    oid = str(uuid.uuid4())[:8]
    _pending_orders[oid] = {
        "id": oid,
        "token_id": order.token_id,
        "side": order.side.upper(),
        "price": order.price,
        "size": order.size,
        "strategy": order.strategy,
        "reason": order.reason,
        "market_name": order.market_name,
        "suggested_at": datetime.utcnow().isoformat(),
        "status": "pending",
    }
    return {"success": True, "id": oid}


@app.get("/api/orders/pending")
async def get_pending_orders(_: None = Depends(verify_secret)):
    """Lista todas as ordens pendentes de aprovação."""
    return {"pending": list(_pending_orders.values())}


@app.post("/api/order/pending/{order_id}/approve")
async def approve_order(order_id: str, _: None = Depends(verify_secret)):
    """Aprova e executa uma ordem pendente."""
    order = _pending_orders.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Ordem não encontrada")
    try:
        result = _execute_order(order["token_id"], order["side"], order["price"], order["size"])
        _pending_orders.pop(order_id, None)
        return {"success": True, "approved": order_id, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/order/pending/{order_id}")
async def reject_order(order_id: str, _: None = Depends(verify_secret)):
    """Rejeita e descarta uma ordem pendente."""
    if order_id not in _pending_orders:
        raise HTTPException(status_code=404, detail="Ordem não encontrada")
    _pending_orders.pop(order_id)
    return {"success": True, "rejected": order_id}


# ---------------------------------------------------------------------------
# Mercado e Orderbook
# ---------------------------------------------------------------------------

@app.get("/api/orderbook/{token_id}")
async def get_orderbook(token_id: str, _: None = Depends(verify_secret)):
    try:
        client = _make_client()
        ob   = client.get_order_book(token_id)
        bids = getattr(ob, "bids", []) or []
        asks = getattr(ob, "asks", []) or []

        def to_list(levels):
            out = []
            for lv in levels[:10]:
                if isinstance(lv, dict):
                    out.append({"price": str(lv.get("price", 0)), "size": str(lv.get("size", 0))})
                else:
                    out.append({"price": str(getattr(lv, "price", 0)), "size": str(getattr(lv, "size", 0))})
            return out

        best_bid = float(bids[0].price) if bids else 0.0
        best_ask = float(asks[0].price) if asks else 1.0
        return {
            "token_id": token_id,
            "bids": to_list(bids),
            "asks": to_list(asks),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "midpoint": round((best_bid + best_ask) / 2, 4),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/market/search")
async def search_market(slug: str, _: None = Depends(verify_secret)):
    try:
        resp = req.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug},
            timeout=8,
        )
        resp.raise_for_status()
        result = []
        for m in resp.json()[:5]:
            prices = m.get("outcomePrices", [])
            if isinstance(prices, str):
                import json
                prices = json.loads(prices)
            token_ids = m.get("clobTokenIds", [])
            result.append({
                "slug":         m.get("slug", ""),
                "question":     m.get("question", ""),
                "token_id_yes": token_ids[0] if len(token_ids) > 0 else "",
                "token_id_no":  token_ids[1] if len(token_ids) > 1 else "",
                "price_yes":    float(prices[0]) if prices else None,
                "price_no":     float(prices[1]) if len(prices) > 1 else None,
                "end_date":     m.get("endDateIso", ""),
                "active":       m.get("active", False),
            })
        return {"markets": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard_api:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), reload=False)
