"""
Python REST API for Polymarket.
Proxies to Gamma, Data, and CLOB APIs — no API key, auth, or wallet required for public endpoints.
"""

"""

server url: http://127.0.0.1:8000
start server: python -m uvicorn main:app --reload

"""
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import CLOB_API, DATA_API, GAMMA_API


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Larger pool so many concurrent proxy requests (e.g. from insider_detection --all-events)
    # do not hit PoolTimeout waiting for a free connection to Gamma/Data/CLOB.
    limits = httpx.Limits(
        max_connections=500,
        max_keepalive_connections=100,
    )
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        app.state.http = client
        yield


app = FastAPI(
    title="Polymarket API",
    description="REST API proxy for Polymarket (Gamma, Data, CLOB). No authentication required.",
    version="1.0.0",
    lifespan=lifespan,
)


def _query_params(request: Request) -> dict[str, Any]:
    """Forward query params, excluding internal ones."""
    return {k: v for k, v in request.query_params.items()}


def _gamma_events_params(request: Request) -> dict[str, Any]:
    """
    Build query string for Gamma GET /events.

    Bare /events (no query) on upstream Gamma returns an unhelpful default slice
    (often old resolved markets). Match model.event_cache filters unless the client
    opts out with raw=1 (pass-through to Gamma, raw stripped).
    """
    params = dict(_query_params(request))
    raw_val = str(params.pop("raw", "")).lower()
    if raw_val in ("1", "true", "yes"):
        return params
    if "active" not in params:
        params["active"] = "true"
    if "closed" not in params:
        params["closed"] = "false"
    if "limit" not in params:
        params["limit"] = "100"
    return params


async def _proxy_get(
    request: Request,
    base: str,
    path: str,
    path_param: str | None = None,
) -> JSONResponse:
    url = f"{base}{path}"
    if path_param:
        url = f"{base}{path.replace('{id}', path_param)}"
    params = _query_params(request)
    client: httpx.AsyncClient = request.app.state.http
    r = await client.get(url, params=params)
    try:
        content = r.json()
    except Exception:
        content = {"detail": r.text}
    return JSONResponse(status_code=r.status_code, content=content)


# ---------- Gamma API (events, markets, search, tags, series, sports) ----------


@app.get("/events", tags=["Gamma"])
async def get_events(request: Request):
    """List events with optional filtering and pagination (defaults match event_cache)."""
    params = _gamma_events_params(request)
    client: httpx.AsyncClient = request.app.state.http
    r = await client.get(f"{GAMMA_API}/events", params=params)
    try:
        content = r.json()
    except Exception:
        content = {"detail": r.text}
    return JSONResponse(status_code=r.status_code, content=content)


@app.get("/events/{id}", tags=["Gamma"])
async def get_event(request: Request, id: str):
    """Get a single event by ID."""
    return await _proxy_get(request, GAMMA_API, "/events/{id}", id)


@app.get("/markets", tags=["Gamma"])
async def get_markets(request: Request):
    """List markets with optional filtering and pagination."""
    return await _proxy_get(request, GAMMA_API, "/markets")


@app.get("/markets/{id}", tags=["Gamma"])
async def get_market(request: Request, id: str):
    """Get a single market by ID."""
    return await _proxy_get(request, GAMMA_API, "/markets/{id}", id)


@app.get("/public-search", tags=["Gamma"])
async def public_search(request: Request):
    """Search across events, markets, and profiles."""
    return await _proxy_get(request, GAMMA_API, "/public-search")


@app.get("/tags", tags=["Gamma"])
async def get_tags(request: Request):
    """Ranked tags/categories."""
    return await _proxy_get(request, GAMMA_API, "/tags")


@app.get("/series", tags=["Gamma"])
async def get_series(request: Request):
    """Series (grouped events)."""
    return await _proxy_get(request, GAMMA_API, "/series")


@app.get("/sports", tags=["Gamma"])
async def get_sports(request: Request):
    """Sports metadata."""
    return await _proxy_get(request, GAMMA_API, "/sports")


@app.get("/teams", tags=["Gamma"])
async def get_teams(request: Request):
    """Teams."""
    return await _proxy_get(request, GAMMA_API, "/teams")


# ---------- CLOB API (prices, orderbook) ----------


@app.get("/price", tags=["CLOB"])
async def get_price(request: Request):
    """Price for a single token."""
    return await _proxy_get(request, CLOB_API, "/price")


@app.get("/prices", tags=["CLOB"])
async def get_prices(request: Request):
    """Prices for multiple tokens. Converts GET params to POST body (CLOB expects POST)."""
    token_ids_raw = request.query_params.get("token_ids") or ""
    sides_raw = request.query_params.get("sides") or ""
    token_ids = [s.strip() for s in token_ids_raw.split(",") if s.strip()]
    sides = [s.strip().upper() for s in sides_raw.split(",") if s.strip()]
    if len(token_ids) != len(sides) or not token_ids:
        return JSONResponse(
            status_code=400,
            content={"detail": "token_ids and sides must be comma-separated and same length"},
        )
    body = [
        {"token_id": tid, "side": side if side in ("BUY", "SELL") else "BUY"}
        for tid, side in zip(token_ids, sides)
    ]
    client: httpx.AsyncClient = request.app.state.http
    r = await client.post(f"{CLOB_API}/prices", json=body)
    try:
        content = r.json()
    except Exception:
        content = {"detail": r.text}
    return JSONResponse(status_code=r.status_code, content=content)


@app.get("/book", tags=["CLOB"])
async def get_book(request: Request):
    """Order book for a token."""
    return await _proxy_get(request, CLOB_API, "/book")


@app.post("/books", tags=["CLOB"])
async def get_books(request: Request):
    """Order books for multiple tokens (POST body: token IDs)."""
    body = await request.json()
    client: httpx.AsyncClient = request.app.state.http
    r = await client.post(f"{CLOB_API}/books", json=body)
    try:
        content = r.json()
    except Exception:
        content = {"detail": r.text}
    return JSONResponse(status_code=r.status_code, content=content)


@app.get("/prices-history", tags=["CLOB"])
async def get_prices_history(request: Request):
    """Historical price data for a token."""
    return await _proxy_get(request, CLOB_API, "/prices-history")


@app.get("/midpoint", tags=["CLOB"])
async def get_midpoint(request: Request):
    """Midpoint price for a token."""
    return await _proxy_get(request, CLOB_API, "/midpoint")


@app.get("/spread", tags=["CLOB"])
async def get_spread(request: Request):
    """Spread for a token."""
    return await _proxy_get(request, CLOB_API, "/spread")


# ---------- Data API (positions, trades, activity, analytics) ----------


@app.get("/positions", tags=["Data"])
async def get_positions(request: Request):
    """Current positions for a user (pass user=0x...)."""
    return await _proxy_get(request, DATA_API, "/positions")


@app.get("/closed-positions", tags=["Data"])
async def get_closed_positions(request: Request):
    """Closed positions for a user (pass user=0x...)."""
    return await _proxy_get(request, DATA_API, "/closed-positions")


@app.get("/activity", tags=["Data"])
async def get_activity(request: Request):
    """Onchain activity for a user (pass user=0x...)."""
    return await _proxy_get(request, DATA_API, "/activity")


@app.get("/value", tags=["Data"])
async def get_value(request: Request):
    """Total position value for a user (pass user=0x...)."""
    return await _proxy_get(request, DATA_API, "/value")


@app.get("/oi", tags=["Data"])
async def get_open_interest(request: Request):
    """Open interest for a market."""
    return await _proxy_get(request, DATA_API, "/oi")


@app.get("/holders", tags=["Data"])
async def get_holders(request: Request):
    """Top holders of a market."""
    return await _proxy_get(request, DATA_API, "/holders")


@app.get("/trades", tags=["Data"])
async def get_trades(request: Request):
    """Trade history (optional user=0x...)."""
    return await _proxy_get(request, DATA_API, "/trades")


@app.get("/")
async def root():
    return {
        "message": "Polymarket API proxy",
        "docs": "/docs",
        "gamma": ["/events", "/events/{id}", "/markets", "/markets/{id}", "/public-search", "/tags", "/series", "/sports", "/teams"],
        "clob": ["/price", "/prices", "/book", "/books (POST)", "/prices-history", "/midpoint", "/spread"],
        "data": ["/positions", "/closed-positions", "/activity", "/value", "/oi", "/holders", "/trades"],
    }


@app.get("/health", tags=["Meta"])
async def health():
    """Lightweight readiness check for load balancers (e.g. Render)."""
    return {"status": "ok"}
