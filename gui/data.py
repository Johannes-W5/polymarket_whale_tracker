from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any

import httpx

from database.events import get_events as get_db_events
from database.events import get_recent_whale_spikes
from model.event_prices import DEFAULT_BASE_URL, get_event_prices
from model.insider_model import assess_insider_probability_for_event


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _api_get_json(path: str, *, base_url: str = DEFAULT_BASE_URL, timeout: float = 30.0) -> dict[str, Any]:
    base = base_url.rstrip("/")
    with httpx.Client(timeout=timeout) as client:
        response = client.get(f"{base}{path}")
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected response type for {path}: {type(payload).__name__}")
    return payload


def _select_primary_market(event: dict[str, Any]) -> dict[str, Any] | None:
    markets = event.get("markets") or []
    if not isinstance(markets, list) or not markets:
        return None
    open_markets = [market for market in markets if isinstance(market, dict) and not market.get("closed", False)]
    candidates = open_markets or [market for market in markets if isinstance(market, dict)]
    return candidates[0] if candidates else None


def list_event_options(limit: int = 200) -> list[dict[str, str]]:
    try:
        rows = get_db_events() or []
    except Exception:
        return []

    options: list[dict[str, str]] = []
    for row in rows[: max(1, limit)]:
        event_id = str(row.get("id") or "").strip()
        if not event_id:
            continue
        name = str(row.get("name") or row.get("description") or event_id).strip()
        options.append({"id": event_id, "label": f"{event_id} - {name}"})

    options.sort(key=lambda item: item["label"].lower())
    return options[: max(1, limit)]


def load_dashboard_data(
    event_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    news_path: str = "data/news_events.jsonl",
) -> dict[str, Any]:
    selected_event_id = str(event_id).strip()
    if not selected_event_id:
        raise ValueError("An event ID is required.")

    event = _api_get_json(f"/events/{selected_event_id}", base_url=base_url)
    market = _select_primary_market(event)
    prices = get_event_prices(selected_event_id, base_url=base_url)

    latest_spike: dict[str, Any] | None = None
    recent_spikes: list[dict[str, Any]] = []
    spikes_error: str | None = None
    try:
        recent_spikes = [_json_safe(dict(row)) for row in (get_recent_whale_spikes(selected_event_id, limit=5) or [])]
        latest_spike = recent_spikes[0] if recent_spikes else None
    except Exception as exc:
        spikes_error = str(exc)

    assessment: dict[str, Any] | None = None
    assessment_error: str | None = None
    try:
        assessment = asdict(
            assess_insider_probability_for_event(
                selected_event_id,
                base_url=base_url,
                news_path=news_path,
            )
        )
    except Exception as exc:
        assessment_error = str(exc)

    return {
        "event_id": selected_event_id,
        "event": _json_safe(event),
        "market": _json_safe(market or {}),
        "prices": {
            "yes_price": prices.yes_price,
            "no_price": prices.no_price,
            "yes_token_id": prices.yes_token_id,
            "no_token_id": prices.no_token_id,
        },
        "assessment": assessment,
        "assessment_error": assessment_error,
        "latest_spike": latest_spike,
        "recent_spikes": recent_spikes,
        "spikes_error": spikes_error,
    }
