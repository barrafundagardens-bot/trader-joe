"""
dashboard_api.py — FastAPI backend para o Trader Joe Dashboard.

Expõe endpoints autenticados para:
- Consultar saldo USDC
- Listar ordens abertas e trades recentes
- Colocar e cancelar limit orders
- Consultar orderbook de um token
- Buscar mercados via Gamma API

Segurança:
- Todas as credenciais Polymarket ficam em variáveis de ambiente no servidor
- Acesso ao dashboard protegido por DASHBOARD_SECRET (header X-Dashboard-Secret)
- NUNCA exponha este servidor sem o DASHBOARD_SECRET configurado
"""

from __future__ import annotations

import math
import os
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


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def verify_secret(x_dashboard_secret: Optional[str] = Header(None)) -> None:
    if DASHBOARD_SECRET and x_dashboard_secret != DASHBOARD_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# CLOB client factory (stateless — nova instância por request)
# ---------------------------------------------------------------------------

def _make_client() -> ClobClient:
    private_key = os.getenv("PRIVATE_KEY", "")
    clob_host = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
    chain_id = int(os.getenv("CHAIN_ID", "137"))
    api_key = os.getenv("API_KEY", "")
    api_secret = os.getenv("API_SECRET", "")
    api_passphrase = os.getenv("API_PASSPHRASE", "")
    proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
    signature_type = int(os.getenv("SIGNATURE_TYPE", "0"))

    creds = ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
    )
    kwargs: dict = {
        "host": clob_host,
        "chain_id": chain_id,
        "key": private_key,
        "creds": creds,
        "signature_type": signature_type,
    }
    if proxy_address:
        kwargs["funder"] = proxy_address
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
# Rotas
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_polymarket.html")
    with open(html_path, encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


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
        return {
            "balance_usdc": round(balance, 2),
            "allowance_usdc": round(allowance, 2),
        }
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
        trades = client.get_trades() or []
        return {"trades": [_trade_to_dict(t) for t in trades[:50]]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/orderbook/{token_id}")
async def get_orderbook(token_id: str, _: None = Depends(verify_secret)):
    try:
        client = _make_client()
        ob = client.get_order_book(token_id)
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
        midpoint = round((best_bid + best_ask) / 2, 4)

        return {
            "token_id": token_id,
            "bids": to_list(bids),
            "asks": to_list(asks),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "midpoint": midpoint,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class OrderRequest(BaseModel):
    token_id: str
    side: str       # "BUY" or "SELL"
    price: float    # 0.01 – 0.99
    size: float     # shares (não USDC)


@app.post("/api/order")
async def place_order(order: OrderRequest, _: None = Depends(verify_secret)):
    side = order.side.upper()
    if side not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="side deve ser BUY ou SELL")

    price = max(0.01, min(0.99, order.price))
    size = max(1.0, math.ceil(order.size))

    try:
        client = _make_client()
        order_args = OrderArgs(
            token_id=order.token_id,
            price=price,
            size=size,
            side=side,
            fee_rate_bps=0,
        )
        signed = client.create_order(order_args)
        response = client.post_order(signed, OrderType.GTC)

        order_id = response.get("orderID", "") if isinstance(response, dict) else getattr(response, "orderID", "")
        cost_usd = round(size * price, 2)
        return {
            "success": True,
            "order_id": order_id,
            "side": side,
            "price": price,
            "size": size,
            "cost_usd": cost_usd,
        }
    except Exception as e:
        err = str(e)
        # Auto-retry com fee rate correto se o CLOB exigir
        if "invalid fee rate" in err and "maker fee" in err:
            import re
            m = re.search(r"maker fee[:\s]+(\d+)", err)
            if m:
                fee_bps = int(m.group(1))
                try:
                    client = _make_client()
                    order_args = OrderArgs(
                        token_id=order.token_id,
                        price=price,
                        size=size,
                        side=side,
                        fee_rate_bps=fee_bps,
                    )
                    signed = client.create_order(order_args)
                    response = client.post_order(signed, OrderType.GTC)
                    order_id = response.get("orderID", "") if isinstance(response, dict) else getattr(response, "orderID", "")
                    return {
                        "success": True,
                        "order_id": order_id,
                        "side": side,
                        "price": price,
                        "size": size,
                        "cost_usd": round(size * price, 2),
                    }
                except Exception as e2:
                    raise HTTPException(status_code=500, detail=str(e2))
        raise HTTPException(status_code=500, detail=err)


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


@app.get("/api/market/search")
async def search_market(slug: str, _: None = Depends(verify_secret)):
    """Busca mercado via Gamma API pelo slug."""
    try:
        resp = req.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug},
            timeout=8,
        )
        resp.raise_for_status()
        markets = resp.json()
        result = []
        for m in markets[:5]:
            prices = m.get("outcomePrices", [])
            if isinstance(prices, str):
                import json
                prices = json.loads(prices)
            result.append({
                "slug": m.get("slug", ""),
                "question": m.get("question", ""),
                "token_id_yes": m.get("clobTokenIds", ["", ""])[0] if m.get("clobTokenIds") else "",
                "token_id_no": m.get("clobTokenIds", ["", ""])[1] if len(m.get("clobTokenIds", [])) > 1 else "",
                "price_yes": float(prices[0]) if prices else None,
                "price_no": float(prices[1]) if len(prices) > 1 else None,
                "end_date": m.get("endDateIso", ""),
                "active": m.get("active", False),
                "closed": m.get("closed", False),
            })
        return {"markets": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("dashboard_api:app", host="0.0.0.0", port=port, reload=False)
