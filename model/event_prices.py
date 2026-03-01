"""
Fetch yes/no prices for a Polymarket event by ID via the server API.

Uses GET /events/{id} to get token IDs from the first market, then
GET /prices with both tokens in one request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:8000"


@dataclass
class EventPrices:
    """Yes and no prices for an event (first market)."""

    yes_price: float | None
    no_price: float | None
    yes_token_id: str | None = None
    no_token_id: str | None = None

    @property
    def both(self) -> tuple[float | None, float | None]:
        """Return (yes_price, no_price)."""
        return (self.yes_price, self.no_price)


def _parse_clob_token_ids(market: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract (yes_token_id, no_token_id) from a market's clobTokenIds."""
    raw = market.get("clobTokenIds")
    if not raw:
        return (None, None)
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(ids, list) and len(ids) >= 2:
            return (str(ids[0]), str(ids[1]))
        return (None, None)
    except (json.JSONDecodeError, TypeError):
        return (None, None)


def get_event_prices(
    event_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    side: str = "BUY",
    timeout: float = 30.0,
    market_index: int = 0,
) -> EventPrices:
    """
    Return yes and no prices for an event by ID.

    Uses the server API: GET /events/{id} then GET /prices with both
    token IDs in a single request. Prices are from the first market
    by default; use market_index for multi-market events.

    Args:
        event_id: Polymarket event ID (e.g. "2890").
        base_url: Server base URL (default http://127.0.0.1:8000).
        side: "BUY" or "SELL" for the price to fetch (default "BUY").
        timeout: Request timeout in seconds.
        market_index: Which market to use when the event has several (default 0).

    Returns:
        EventPrices with yes_price, no_price (float or None if unavailable),
        and yes_token_id, no_token_id when present.
    """
    base = base_url.rstrip("/")
    with httpx.Client(timeout=timeout) as client:
        # 1) Get event and token IDs from first market
        r = client.get(f"{base}/events/{event_id}")
        r.raise_for_status()
        event: dict[str, Any] = r.json()
        markets = event.get("markets") or []
        if not markets:
            return EventPrices(yes_price=None, no_price=None)
        market = markets[market_index] if market_index < len(markets) else markets[0]
        yes_token_id, no_token_id = _parse_clob_token_ids(market)
        if not yes_token_id or not no_token_id:
            return EventPrices(
                yes_price=None,
                no_price=None,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
            )

        # 2) Fetch both prices in one request (CLOB: token_ids and sides comma-separated)
        r = client.get(
            f"{base}/prices",
            params={
                "token_ids": f"{yes_token_id},{no_token_id}",
                "sides": f"{side},{side}",
            },
        )
        if r.status_code != 200:
            return EventPrices(
                yes_price=None,
                no_price=None,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
            )
        data: dict[str, dict[str, float]] = r.json()

        yes_map = data.get(yes_token_id) or {}
        no_map = data.get(no_token_id) or {}
        yes_price = yes_map.get(side) if isinstance(yes_map.get(side), (int, float)) else None
        no_price = no_map.get(side) if isinstance(no_map.get(side), (int, float)) else None

        return EventPrices(
            yes_price=float(yes_price) if yes_price is not None else None,
            no_price=float(no_price) if no_price is not None else None,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
        )


def get_event_yes_price(
    event_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    side: str = "BUY",
    timeout: float = 30.0,
    market_index: int = 0,
) -> float | None:
    """Return the yes price for an event, or None if unavailable."""
    return get_event_prices(
        event_id,
        base_url=base_url,
        side=side,
        timeout=timeout,
        market_index=market_index,
    ).yes_price


def get_event_no_price(
    event_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    side: str = "BUY",
    timeout: float = 30.0,
    market_index: int = 0,
) -> float | None:
    """Return the no price for an event, or None if unavailable."""
    return get_event_prices(
        event_id,
        base_url=base_url,
        side=side,
        timeout=timeout,
        market_index=market_index,
    ).no_price


if __name__ == "__main__":
    print(get_event_yes_price("2890"))
    print(get_event_no_price("2890"))
