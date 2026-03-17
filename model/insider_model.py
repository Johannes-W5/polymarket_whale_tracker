from __future__ import annotations

"""
Ollama-based heuristic for detecting likely insider / informed trading.

This module takes:
- Polymarket event metadata (via the local proxy `/events/{id}`)
- Derived market activity signals from `model.market_signals`

and asks an Ollama model to produce:
- A probability in [0, 1] that current activity is driven by materially
  informed / insider trading rather than ordinary speculative flow.
- A short natural-language summary explaining the reasoning.

The result is NOT a legal determination and should only be used as a
research / analytics signal.
"""

import json
import os
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from database.events import (
    get_event as get_event_from_db,
    get_recent_whale_spikes,
    insert_event as insert_event_to_db,
)
from .event_prices import DEFAULT_BASE_URL
from .fresh_data import fetch_fresh_market_data_from_api
from .market_signals import (
    VolumeStats,
    OrderbookImbalance,
    OpenInterestSnapshot,
    PriceHistoryStats,
    NewsTiming,
    compute_volume_stats,
    compute_orderbook_imbalance_for_event,
    fetch_open_interest_for_event,
    compute_price_history_stats_for_event,
    find_nearest_news_for_event,
)


@dataclass
class InsiderAssessment:
    """Structured result from the Ollama insider-risk classifier."""

    probability_insider: float
    confidence: str
    short_summary: str


def _parse_iso8601_utc(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _get_ollama_config(
    host: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> tuple[str, str, str]:
    resolved_host = (host or os.getenv("OLLAMA_HOST") or "https://ollama.com").rstrip("/")
    resolved_model = model or os.getenv("OLLAMA_MODEL") or "qwen3.5:cloud"
    resolved_api_key = api_key or os.getenv("OLLAMA_API_KEY")
    if not resolved_api_key:
        raise RuntimeError(
            "OLLAMA_API_KEY is not set. Create a key at https://ollama.com/settings/keys "
            "and export it before running cloud models."
        )
    return resolved_host, resolved_model, resolved_api_key


def _ollama_api_url(host: str, path: str) -> str:
    base = host.rstrip("/")
    if base.endswith("/api"):
        return f"{base}/{path.lstrip('/')}"
    return f"{base}/api/{path.lstrip('/')}"


def _fetch_event_raw(
    event_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    base = base_url.rstrip("/")
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{base}/events/{event_id}")
        r.raise_for_status()
        return r.json()


def _fetch_event_db(event_id: str) -> Dict[str, Any] | None:
    """
    Load event metadata from PostgreSQL if available.
    """
    event = get_event_from_db(event_id)
    if not event:
        return None
    if isinstance(event, dict):
        return dict(event)
    # psycopg2 RealDictRow behaves like a mapping, normalize to plain dict.
    try:
        return dict(event)
    except Exception as exc:
        print(f"[insider-model] Failed to normalize event from DB: {exc}", flush=True)
        return None


def _is_event_active(event: Dict[str, Any]) -> bool:
    return bool(event.get("active")) and not bool(event.get("closed", False))


def _cache_event_in_db(event: Dict[str, Any]) -> None:
    """
    Best-effort cache of slow-changing event metadata in PostgreSQL.
    """
    if not _is_event_active(event):
        return
    try:
        insert_event_to_db(event)
    except Exception as exc:
        # Do not fail scoring if DB cache is temporarily unavailable.
        print(f"[insider-model] Failed to cache event in DB: {exc}", flush=True)


def _fetch_recent_spikes_db(event_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Load recently detected spikes for this event from PostgreSQL.
    """
    try:
        rows = get_recent_whale_spikes(event_id, limit=limit) or []
    except Exception as exc:
        print(f"[insider-model] Failed to fetch recent whale spikes for event {event_id}: {exc}", flush=True)
        return []

    result: List[Dict[str, Any]] = []
    for row in rows:
        try:
            result.append(dict(row))
        except Exception as exc:
            print(f"[insider-model] Failed to convert spike row to dict: {exc}", flush=True)
            continue
    return result


def _simplify_event(event: Dict[str, Any]) -> Dict[str, Any]:
    markets = event.get("markets") or []
    simple_markets: List[Dict[str, Any]] = []
    for m in markets:
        if not isinstance(m, dict):
            continue
        simple_markets.append(
            {
                "id": m.get("id"),
                "slug": m.get("slug"),
                "title": m.get("title") or m.get("question"),
                "closed": m.get("closed"),
                "end_date": m.get("endDate") or m.get("end_date"),
                "volume": m.get("volume"),
                "liquidity": m.get("liquidity"),
            }
        )

    return {
        "id": event.get("id"),
        "slug": event.get("slug"),
        "title": event.get("title") or event.get("name"),
        "description": event.get("description"),
        "category": event.get("category"),
        "sub_category": event.get("subCategory") or event.get("sub_category"),
        "created_at": event.get("created_at") or event.get("createdAt"),
        "resolution_source": event.get("resolutionSource")
        or event.get("resolution_source"),
        "markets": simple_markets,
    }


def _isoformat(dt: datetime | None) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _resolve_signal_time(
    trigger_context: Dict[str, Any] | None,
    fresh_market_data: Dict[str, Any] | None,
) -> tuple[datetime, str]:
    """
    Pick the timestamp that best represents when the suspicious activity happened.

    Preference order:
    1. Explicit trigger timestamp passed by caller.
    2. Trigger payload spike end timestamp (`to_ts`) from insider_detection.
    3. Fresh market-data capture time near the trigger.
    4. Current time as a last-resort fallback.
    """
    if isinstance(trigger_context, dict):
        explicit_signal_time = _parse_iso8601_utc(trigger_context.get("signal_time"))
        if explicit_signal_time is not None:
            return explicit_signal_time, "trigger_context.signal_time"

        trigger_payload = trigger_context.get("trigger_payload")
        if isinstance(trigger_payload, dict):
            for key in ("to_ts", "captured_at", "timestamp", "triggered_at"):
                parsed = _parse_iso8601_utc(trigger_payload.get(key))
                if parsed is not None:
                    return parsed, f"trigger_context.trigger_payload.{key}"

    if isinstance(fresh_market_data, dict):
        fresh_captured_at = _parse_iso8601_utc(fresh_market_data.get("captured_at"))
        if fresh_captured_at is not None:
            return fresh_captured_at, "fresh_market_data.captured_at"

    return datetime.now(timezone.utc), "generated_at_fallback"


def _build_feature_payload(
    event_id: str,
    *,
    base_url: str,
    news_path: str = "data/news_events.jsonl",
    include_db_event: bool = True,
    trigger_context: Dict[str, Any] | None = None,
    fresh_market_data: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    Collect event metadata and derived signals into a single JSON-serialisable
    payload to feed into the Ollama model.
    """
    event_raw = _fetch_event_raw(event_id, base_url=base_url)
    _cache_event_in_db(event_raw)
    event_simple = _simplify_event(event_raw)
    event_db = _fetch_event_db(event_id) if include_db_event else None
    recent_spikes_db = _fetch_recent_spikes_db(event_id, limit=5)

    volume: VolumeStats = compute_volume_stats(event_id, base_url=base_url)
    orderbooks: List[OrderbookImbalance] = compute_orderbook_imbalance_for_event(
        event_id,
        base_url=base_url,
    )
    oi_snapshots: List[OpenInterestSnapshot] = fetch_open_interest_for_event(
        event_id,
        base_url=base_url,
    )
    price_stats: List[PriceHistoryStats] = compute_price_history_stats_for_event(
        event_id,
        base_url=base_url,
    )

    now_utc = datetime.now(timezone.utc)
    fresh_data = fresh_market_data or fetch_fresh_market_data_from_api(
        event_id,
        base_url=base_url,
    )
    signal_time, signal_time_source = _resolve_signal_time(
        trigger_context,
        fresh_data,
    )
    news_timing: Optional[NewsTiming] = find_nearest_news_for_event(
        event_id,
        signal_time=signal_time,
        base_url=base_url,
        news_path=news_path,
    )

    def _asdict_list(objs):
        return [asdict(o) for o in objs]

    payload: Dict[str, Any] = {
        "event": event_simple,
        "event_db": event_db,
        "recent_whale_spikes_db": recent_spikes_db,
        "generated_at": _isoformat(now_utc),
        "signal_time_for_news": _isoformat(signal_time),
        "signal_time_source": signal_time_source,
        "news_timing_interpretation": {
            "signal_time_definition": (
                "Approximate time when the suspicious trade/spike/trigger happened."
            ),
            "positive_delta_minutes_means": (
                "News arrived after the signal time, so trading/spike happened before news."
            ),
            "negative_delta_minutes_means": (
                "News arrived before the signal time, so trading/spike happened after news."
            ),
        },
        "fresh_market_data": fresh_data,
        "volume_stats": asdict(volume),
        "orderbook_imbalance": _asdict_list(orderbooks),
        "open_interest": _asdict_list(oi_snapshots),
        "price_history_stats": _asdict_list(price_stats),
        "news_timing": asdict(news_timing) if news_timing is not None else None,
        "trigger_context": trigger_context,
    }
    return payload


def _json_serial_default(obj: Any) -> Any:
    """Convert non-JSON-serializable values for json.dumps (e.g. datetime from DB)."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _build_prompt(features: Dict[str, Any]) -> str:
    """
    Build a single JSON-heavy prompt for the Ollama model.
    """
    features_json = json.dumps(
        features, indent=2, sort_keys=True, default=_json_serial_default
    )
    return (
        "You are an expert quantitative analyst focused on prediction markets, "
        "order books, and market microstructure. You are given a Polymarket event "
        "and a recent snapshot of trading activity and news.\n\n"
        "Your task is to estimate the probability that the current trading "
        "pattern for this event is driven by materially informed or insider "
        "trading (i.e. traders with significantly better information than the "
        "typical retail crowd), as opposed to ordinary speculative or noise trading.\n\n"
        "You must treat this as a heuristic risk score, not a legal judgement. "
        "Consider price moves, volume spikes, buy/sell imbalance, order book "
        "depth and imbalance, changes in open interest, abnormal price history "
        "patterns (e.g. outlier moves vs recent volatility), and the timing of "
        "relevant news.\n\n"
        "When interpreting `news_timing`, compare the news timestamp to the "
        "provided signal/trigger time rather than the assessment runtime. "
        "DO NOT treat positive `delta_minutes` as evidence that suspicious activity, just disregard news and timing of news for now completely " #Treat positive `delta_minutes` as evidence that suspicious activity 
        "happened before the relevant news, and negative `delta_minutes` as "
        "evidence that suspicious activity happened after the news. Post-news "
        "activity is usually less indicative of insider trading than pre-news "
        "activity, all else equal.\n\n"
        "You will receive a JSON object with event metadata and derived signals:\n"
        f"{features_json}\n\n"
        "Return ONLY a single JSON object with the following fields:\n"
        '{\n'
        '  "probability_insider": <float between 0 and 1>,\n'
        '  "confidence": "low" | "medium" | "high",\n'
        '  "short_summary": "<one or two concise sentences explaining why>"\n'
        "}\n\n"
        "The probability should reflect how likely it is that INFORMED or insider "
        "traders are significantly influencing the observed market activity.\n"
        "Be conservative: reserve probabilities above 0.7 for very strong signals."
    )


def _extract_json_object(text: str) -> Dict[str, Any]:
    content = (text or "").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError(
                f"Ollama response was not valid JSON. Raw content was:\n{content}"
            )
        try:
            return json.loads(content[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Ollama response was not valid JSON. Raw content was:\n{content}"
            ) from exc


def assess_insider_probability_for_event(
    event_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    ollama_host: Optional[str] = None,
    ollama_api_key: Optional[str] = None,
    model: Optional[str] = None,
    news_path: str = "data/news_events.jsonl",
    temperature: float = 0.1,
    include_db_event: bool = True,
    trigger_context: Dict[str, Any] | None = None,
    fresh_market_data: Dict[str, Any] | None = None,
) -> InsiderAssessment:
    """
    High-level helper: call Ollama to estimate insider-trading likelihood.

    Args:
        event_id: Polymarket event ID.
        base_url: Base URL of your local Polymarket proxy (default 127.0.0.1:8000).
        ollama_host: Optional override; otherwise uses OLLAMA_HOST or `https://ollama.com`.
        ollama_api_key: Optional override; otherwise uses OLLAMA_API_KEY.
        model: Ollama cloud model name. If omitted, uses OLLAMA_MODEL or `qwen3.5:cloud`.
        news_path: Path to `news_events.jsonl` for the news-timing feature.
        temperature: Sampling temperature for the model (default 0.1).
        include_db_event: Include PostgreSQL event row in model features.
        trigger_context: Optional metadata about why assessment was triggered.
        fresh_market_data: Optional trigger-time snapshot (e.g. websocket cache).
            If omitted, a fresh API snapshot is fetched automatically.
    """
    ollama_host, ollama_model, ollama_api_key = _get_ollama_config(
        host=ollama_host,
        model=model,
        api_key=ollama_api_key,
    )
    features = _build_feature_payload(
        event_id,
        base_url=base_url,
        news_path=news_path,
        include_db_event=include_db_event,
        trigger_context=trigger_context,
        fresh_market_data=fresh_market_data,
    )
    prompt = _build_prompt(features)

    system_prompt = (
        "You are a careful, risk-averse quantitative analyst whose "
        "job is to flag patterns that *might* indicate informed or "
        "insider trading in prediction markets."
    )
    with httpx.Client(timeout=120.0) as client:
        response = client.post(
            _ollama_api_url(ollama_host, "chat"),
            headers={
                "Authorization": f"Bearer {ollama_api_key}",
            },
            json={
                "model": ollama_model,
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": temperature,
                },
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Ollama cloud request failed with status {response.status_code}: {response.text}"
            ) from exc
        content = str((response.json().get("message") or {}).get("content") or "")

    parsed = _extract_json_object(content)

    prob = float(parsed.get("probability_insider", 0.0))
    # Clamp to [0, 1] for safety.
    prob = max(0.0, min(1.0, prob))
    confidence = str(parsed.get("confidence") or "low")
    summary = str(parsed.get("short_summary") or "").strip()

    return InsiderAssessment(
        probability_insider=prob,
        confidence=confidence,
        short_summary=summary,
    )


__all__ = [
    "InsiderAssessment",
    "assess_insider_probability_for_event",
]
