from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import sys
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database.events import get_events as get_db_events
from database.events import get_event as get_db_event
from database.events import get_latest_assessment_for_event
from database.events import get_latest_whale_spikes
from database.events import get_recent_whale_spikes
from model.event_prices import DEFAULT_BASE_URL, get_event_prices


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


def _sort_spikes_desc(spikes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        spikes,
        key=lambda spike: str(spike.get("to_ts") or ""),
        reverse=True,
    )


def get_default_event_id() -> str | None:
    try:
        rows = get_latest_whale_spikes(limit=1) or []
    except Exception:
        return None
    if not rows:
        return None
    return str(rows[0].get("event_id") or "").strip() or None


def get_recent_spike_feed(limit: int = 25) -> list[dict[str, Any]]:
    try:
        spike_rows = get_latest_whale_spikes(limit=limit) or []
    except Exception:
        return []

    feed: list[dict[str, Any]] = []
    event_cache: dict[str, dict[str, Any]] = {}
    assessment_cache: dict[str, dict[str, Any] | None] = {}

    for row in spike_rows:
        spike = _json_safe(dict(row))
        event_id = str(spike.get("event_id") or "").strip()
        if not event_id:
            continue

        if event_id not in event_cache:
            try:
                event_cache[event_id] = dict(get_db_event(event_id) or {})
            except Exception:
                event_cache[event_id] = {}

        if event_id not in assessment_cache:
            try:
                latest_assessment = get_latest_assessment_for_event(event_id)
                assessment_cache[event_id] = (
                    _json_safe(dict(latest_assessment)) if latest_assessment else None
                )
            except Exception:
                assessment_cache[event_id] = None

        event_row = event_cache[event_id]
        assessment_row = assessment_cache[event_id]
        feed.append(
            {
                "event_id": event_id,
                "event_name": str(event_row.get("name") or event_row.get("description") or event_id),
                "to_ts": spike.get("to_ts"),
                "side": spike.get("side"),
                "from_price": spike.get("from_price"),
                "to_price": spike.get("to_price"),
                "abs_change": spike.get("abs_change"),
                "rel_change": spike.get("rel_change"),
                "probability_insider": (
                    assessment_row.get("probability_insider") if assessment_row else None
                ),
                "confidence": assessment_row.get("confidence") if assessment_row else None,
                "short_summary": assessment_row.get("short_summary") if assessment_row else None,
            }
        )

    return feed


def list_event_options(limit: int = 200) -> list[dict[str, str]]:
    try:
        rows = get_db_events() or []
    except Exception:
        return []

    spike_order: dict[str, dict[str, str]] = {}
    try:
        for row in get_latest_whale_spikes(limit=limit * 3) or []:
            event_id = str(row.get("event_id") or "").strip()
            if event_id and event_id not in spike_order:
                spike_order[event_id] = {
                    "sort_ts": str(row.get("to_ts") or ""),
                    "label": f"{event_id} - recent whale spike",
                }
    except Exception:
        spike_order = {}

    options_by_id: dict[str, dict[str, str]] = {}
    for row in rows:
        event_id = str(row.get("id") or "").strip()
        if not event_id:
            continue
        name = str(row.get("name") or row.get("description") or event_id).strip()
        options_by_id[event_id] = {
            "id": event_id,
            "label": f"{event_id} - {name}",
            "sort_ts": spike_order.get(event_id, {}).get("sort_ts", ""),
        }

    for event_id, spike_info in spike_order.items():
        options_by_id.setdefault(
            event_id,
            {
                "id": event_id,
                "label": spike_info["label"],
                "sort_ts": spike_info["sort_ts"],
            },
        )

    options = list(options_by_id.values())
    options.sort(key=lambda item: (item["sort_ts"], item["label"].lower()), reverse=True)
    return [{"id": item["id"], "label": item["label"]} for item in options[: max(1, limit)]]


def load_dashboard_data(
    event_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    news_path: str = "data/news_events.jsonl",
) -> dict[str, Any]:
    selected_event_id = str(event_id).strip()
    if not selected_event_id:
        raise ValueError("An event ID is required.")

    latest_spike: dict[str, Any] | None = None
    recent_spikes: list[dict[str, Any]] = []
    spikes_error: str | None = None
    try:
        recent_spikes = _sort_spikes_desc(
            [_json_safe(dict(row)) for row in (get_recent_whale_spikes(selected_event_id, limit=20) or [])]
        )
        latest_spike = recent_spikes[0] if recent_spikes else None
    except Exception as exc:
        spikes_error = str(exc)

    assessment: dict[str, Any] | None = None
    assessment_error: str | None = None
    try:
        persisted_assessment = get_latest_assessment_for_event(selected_event_id)
        if persisted_assessment:
            assessment = _json_safe(dict(persisted_assessment))
        else:
            assessment_error = (
                "No persisted assessment is available for this event yet. The dashboard "
                "shows stored detector results only, so wait for the detector to persist one."
            )
    except Exception as exc:
        assessment_error = str(exc)

    event: dict[str, Any] = {}
    market: dict[str, Any] = {}
    event_error: str | None = None
    try:
        event = _api_get_json(f"/events/{selected_event_id}", base_url=base_url, timeout=10.0)
        market = _select_primary_market(event) or {}
    except Exception as exc:
        event_error = str(exc)

    prices_error: str | None = None
    prices = {
        "yes_price": None,
        "no_price": None,
        "yes_token_id": None,
        "no_token_id": None,
    }
    try:
        current_prices = get_event_prices(selected_event_id, base_url=base_url, timeout=10.0)
        prices = {
            "yes_price": current_prices.yes_price,
            "no_price": current_prices.no_price,
            "yes_token_id": current_prices.yes_token_id,
            "no_token_id": current_prices.no_token_id,
        }
    except Exception as exc:
        prices_error = str(exc)

    return {
        "event_id": selected_event_id,
        "event": _json_safe(event),
        "event_error": event_error,
        "market": _json_safe(market or {}),
        "prices": prices,
        "prices_error": prices_error,
        "assessment": assessment,
        "assessment_error": assessment_error,
        "latest_spike": latest_spike,
        "recent_spikes": recent_spikes,
        "spikes_error": spikes_error,
    }
