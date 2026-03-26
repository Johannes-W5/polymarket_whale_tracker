"""
Python REST API for Polymarket.
Proxies to Gamma, Data, and CLOB APIs — no API key, auth, or wallet required for public endpoints.
"""

"""

server url: http://127.0.0.1:8000
start server: python -m uvicorn main:app --reload

"""
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Ensure `database/` (repo root sibling of `server/`) is importable even when
# running uvicorn from inside `server/`.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import CLOB_API, DATA_API, GAMMA_API


def get_active_events_from_db(*, limit: int = 10000, offset: int = 0):
    # Lazy import keeps proxy startup robust in environments where only the
    # server app is deployed without the DB module path.
    from database.events import get_active_events

    return get_active_events(limit=limit, offset=offset)


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
    """
    Forward query params, excluding internal ones.

    Important: preserve repeated query keys (e.g. `market=a&market=b`) instead of
    collapsing to a single "last value wins" entry.
    """
    params: dict[str, Any] = {}
    for key, value in request.query_params.multi_items():
        if key in params:
            current = params[key]
            if isinstance(current, list):
                current.append(value)
            else:
                params[key] = [current, value]
        else:
            params[key] = value
    return params


def _gamma_events_params(request: Request) -> dict[str, Any]:
    """
    Build query string for Gamma GET /events.

    Bare /events (no query) on upstream Gamma returns an unhelpful default slice
    (often old resolved markets). Match model.event_cache filters unless the client
    opts out with raw=1 (pass-through to Gamma, raw stripped).
    """
    params = _query_params(request)
    raw_val: Any = params.pop("raw", "")
    # `raw` can appear multiple times (rare), treat "last raw" as the effective one.
    if isinstance(raw_val, list) and raw_val:
        raw_val = raw_val[-1]
    raw_val_str = str(raw_val).lower()
    if raw_val_str in ("1", "true", "yes"):
        return params
    if "active" not in params:
        params["active"] = "true"
    if "closed" not in params:
        params["closed"] = "false"
    if "limit" not in params:
        params["limit"] = "500"
    return params


async def _proxy_get(
    request: Request,
    base: str,
    path: str,
    path_param: str | None = None,
) -> JSONResponse:
    t0 = time.perf_counter()
    url = f"{base}{path}"
    if path_param:
        url = f"{base}{path.replace('{id}', path_param)}"
    params = _query_params(request)
    client: httpx.AsyncClient = request.app.state.http
    try:
        r = await client.get(url, params=params)
    except httpx.ReadError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": "Upstream read error", "error": str(exc)},
        )
    except httpx.TimeoutException as exc:
        return JSONResponse(
            status_code=504,
            content={"detail": "Upstream timeout", "error": str(exc)},
        )
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": "Upstream HTTP error", "error": str(exc)},
        )

    headers = getattr(r, "headers", {}) or {}
    content_type = str(headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        resp = Response(
            status_code=r.status_code,
            content=r.content,
            media_type="application/json",
        )
        if str(request.query_params.get("perf", "")).lower() in {"1", "true", "yes"}:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            resp.headers["X-Proxy-Elapsed-Ms"] = f"{elapsed_ms:.1f}"
        return resp
    try:
        content = r.json()
    except Exception:
        content = {"detail": r.text}
    return JSONResponse(status_code=r.status_code, content=content)


# ---------- Gamma API (events, markets, search, tags, series, sports) ----------


@app.get("/events", tags=["Gamma"])
async def get_events(request: Request):
    """List events with optional filtering and pagination (defaults match event_cache)."""
    use_db_flag = request.query_params.get("use_db") or request.query_params.get("source")
    use_db = str(use_db_flag or "").strip().lower() in ("1", "true", "yes", "db", "database", "postgres")

    if use_db:
        # DB listing mode is intentionally minimal: it returns the subset of
        # fields stored by `model/event_cache` (no markets). Use
        # `GET /events/{id}` for full Gamma event payloads.
        try:
            limit = request.query_params.get("limit")
            offset = request.query_params.get("offset")
            db_limit = int(limit) if limit is not None else 10000
            db_offset = int(offset) if offset is not None else 0
        except (TypeError, ValueError):
            db_limit = 10000
            db_offset = 0
        try:
            return JSONResponse(
                status_code=200,
                content=get_active_events_from_db(limit=db_limit, offset=db_offset),
            )
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "DB-backed events mode unavailable on this deployment.",
                    "error": str(exc),
                },
            )

    params = _gamma_events_params(request)
    client: httpx.AsyncClient = request.app.state.http
    r = await client.get(f"{GAMMA_API}/events", params=params)
    headers = getattr(r, "headers", {}) or {}
    content_type = str(headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        return Response(
            status_code=r.status_code,
            content=r.content,
            media_type="application/json",
        )
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
