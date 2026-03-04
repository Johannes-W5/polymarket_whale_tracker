from __future__ import annotations

"""
Utilities for trigger-time "fresh market data".

This module keeps the model pipeline agnostic to whether fresh data comes from:
- API polling, or
- a websocket-fed in-memory cache.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, MutableMapping, Optional

from .event_prices import DEFAULT_BASE_URL, get_event_prices


def _isoformat_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class FreshMarketData:
    event_id: str
    captured_at: str
    source: str
    yes_price: float | None
    no_price: float | None
    yes_token_id: str | None
    no_token_id: str | None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "captured_at": self.captured_at,
            "source": self.source,
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "yes_token_id": self.yes_token_id,
            "no_token_id": self.no_token_id,
        }


def fetch_fresh_market_data_from_api(
    event_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    side: str = "BUY",
) -> Dict[str, Any]:
    """
    Fetch latest yes/no prices from the API for trigger-time model context.
    """
    prices = get_event_prices(event_id, base_url=base_url, side=side)
    payload = FreshMarketData(
        event_id=event_id,
        captured_at=_isoformat_utc(datetime.now(timezone.utc)),
        source="api_poll",
        yes_price=prices.yes_price,
        no_price=prices.no_price,
        yes_token_id=prices.yes_token_id,
        no_token_id=prices.no_token_id,
    )
    return payload.as_dict()


class InMemoryFreshDataStore:
    """
    Optional in-memory store for websocket-fed fresh data.

    A websocket consumer can call `update(event_id, payload)` whenever new data
    arrives. Detection/model code can then call `get(event_id)` at trigger time.
    """

    def __init__(self) -> None:
        self._by_event: MutableMapping[str, Dict[str, Any]] = {}

    def update(self, event_id: str, payload: Dict[str, Any]) -> None:
        self._by_event[str(event_id)] = dict(payload)

    def get(self, event_id: str) -> Optional[Dict[str, Any]]:
        data = self._by_event.get(str(event_id))
        return dict(data) if data is not None else None

