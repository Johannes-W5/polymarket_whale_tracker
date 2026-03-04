from __future__ import annotations

"""
OpenAI-based heuristic for detecting likely insider / informed trading.

This module takes:
- Polymarket event metadata (via the local proxy `/events/{id}`)
- Derived market activity signals from `model.market_signals`

and asks an OpenAI model to produce:
- A probability in [0, 1] that current activity is driven by materially
  informed / insider trading rather than ordinary speculative flow.
- A short natural-language summary explaining the reasoning.

The result is NOT a legal determination and should only be used as a
research / analytics signal.
"""

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from openai import OpenAI

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
    """Structured result from the OpenAI insider-risk classifier."""

    probability_insider: float
    confidence: str
    short_summary: str


def _get_openai_client(api_key: Optional[str] = None) -> OpenAI:
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Please export it in your environment."
        )
    return OpenAI(api_key=key)


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
    except Exception:
        return None


def _cache_event_in_db(event: Dict[str, Any]) -> None:
    """
    Best-effort cache of slow-changing event metadata in PostgreSQL.
    """
    try:
        insert_event_to_db(event)
    except Exception:
        # Do not fail scoring if DB cache is temporarily unavailable.
        pass


def _fetch_recent_spikes_db(event_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Load recently detected spikes for this event from PostgreSQL.
    """
    try:
        rows = get_recent_whale_spikes(event_id, limit=limit) or []
    except Exception:
        return []

    result: List[Dict[str, Any]] = []
    for row in rows:
        try:
            result.append(dict(row))
        except Exception:
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
    payload to feed into the OpenAI model.
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

    # For news timing we use "now" as the signal time by default; callers
    # can override later if needed.
    now_utc = datetime.now(timezone.utc)
    fresh_data = fresh_market_data or fetch_fresh_market_data_from_api(
        event_id,
        base_url=base_url,
    )
    news_timing: Optional[NewsTiming] = find_nearest_news_for_event(
        event_id,
        signal_time=now_utc,
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
        "fresh_market_data": fresh_data,
        "volume_stats": asdict(volume),
        "orderbook_imbalance": _asdict_list(orderbooks),
        "open_interest": _asdict_list(oi_snapshots),
        "price_history_stats": _asdict_list(price_stats),
        "news_timing": asdict(news_timing) if news_timing is not None else None,
        "trigger_context": trigger_context,
    }
    return payload


def _build_prompt(features: Dict[str, Any]) -> str:
    """
    Build a single JSON-heavy prompt for the OpenAI model.
    """
    features_json = json.dumps(features, indent=2, sort_keys=True)
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


def assess_insider_probability_for_event(
    event_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    openai_api_key: Optional[str] = None,
    model: str = "gpt-4.1-mini",
    news_path: str = "data/news_events.jsonl",
    temperature: float = 0.1,
    include_db_event: bool = True,
    trigger_context: Dict[str, Any] | None = None,
    fresh_market_data: Dict[str, Any] | None = None,
) -> InsiderAssessment:
    """
    High-level helper: call OpenAI to estimate insider-trading likelihood.

    Args:
        event_id: Polymarket event ID.
        base_url: Base URL of your local Polymarket proxy (default 127.0.0.1:8000).
        openai_api_key: Optional override; otherwise uses OPENAI_API_KEY env var.
        model: OpenAI chat model name.
        news_path: Path to `news_events.jsonl` for the news-timing feature.
        temperature: Sampling temperature for the model (default 0.1).
        include_db_event: Include PostgreSQL event row in model features.
        trigger_context: Optional metadata about why assessment was triggered.
        fresh_market_data: Optional trigger-time snapshot (e.g. websocket cache).
            If omitted, a fresh API snapshot is fetched automatically.
    """
    client = _get_openai_client(api_key=openai_api_key)
    features = _build_feature_payload(
        event_id,
        base_url=base_url,
        news_path=news_path,
        include_db_event=include_db_event,
        trigger_context=trigger_context,
        fresh_market_data=fresh_market_data,
    )
    prompt = _build_prompt(features)

    completion = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a careful, risk-averse quantitative analyst whose "
                    "job is to flag patterns that *might* indicate informed or "
                    "insider trading in prediction markets."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
    )

    content = completion.choices[0].message.content or ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # Best-effort fallback if the model did not strictly follow JSON.
        raise RuntimeError(
            f"OpenAI response was not valid JSON. Raw content was:\n{content}"
        )

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

