from __future__ import annotations

"""
LLM explanation layer for deterministic public-data anomalies.

The deterministic scorer remains the primary source of evidence. This module
uses an LLM only to:
- explain the frozen deterministic snapshot,
- refine confidence,
- apply a tightly bounded adjustment around a deterministic prior.

Outputs are research signals only and are not legal or compliance judgments.
"""

import hashlib
import math
import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from database.events import (
    get_event as get_event_from_db,
    get_recent_whale_spikes,
    insert_event as insert_event_to_db,
)
from .anomaly_scoring import FEATURE_SNAPSHOT_CONTRACT_VERSION
from .event_prices import DEFAULT_BASE_URL
from .fresh_data import fetch_fresh_market_data_from_api
from .market_signals import (
    NewsTiming,
    OpenInterestSnapshot,
    OrderbookImbalance,
    PriceHistoryStats,
    VolumeStats,
    compute_orderbook_imbalance_for_event,
    compute_price_history_stats_for_event,
    compute_volume_stats,
    fetch_open_interest_for_event,
    find_nearest_news_for_event,
)

PROMPT_VERSION = "llm-explanation-v2"
EXPLANATION_PAYLOAD_VERSION = "llm-explanation-payload-v2"
LEGACY_LIVE_PAYLOAD_VERSION = "legacy-live-explanation-payload-v1"
MAX_PROBABILITY_ADJUSTMENT = 0.12
DEFAULT_SUMMARY_FALLBACK = (
    "The explanation layer was unavailable, so this record falls back to the "
    "deterministic public-data anomaly score only."
)


@dataclass
class InsiderAssessment:
    """Structured explanation-layer output."""

    probability_insider: float
    confidence: str
    short_summary: str
    llm_version: str | None = None
    prompt_hash: str | None = None
    prompt_version: str | None = None
    deterministic_prior_probability: float | None = None
    probability_adjustment: float | None = None
    fallback_reason: str | None = None


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


def _isoformat(dt: datetime | None) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _json_serial_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


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
    event = get_event_from_db(event_id)
    if not event:
        return None
    if isinstance(event, dict):
        return dict(event)
    try:
        return dict(event)
    except Exception as exc:
        print(f"[insider-model] Failed to normalize event from DB: {exc}", flush=True)
        return None


def _is_event_active(event: Dict[str, Any]) -> bool:
    return bool(event.get("active")) and not bool(event.get("closed", False))


def _cache_event_in_db(event: Dict[str, Any]) -> None:
    if not _is_event_active(event):
        return
    try:
        insert_event_to_db(event)
    except Exception as exc:
        print(f"[insider-model] Failed to cache event in DB: {exc}", flush=True)


def _fetch_recent_spikes_db(event_id: str, limit: int = 5) -> List[Dict[str, Any]]:
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
    for market in markets:
        if not isinstance(market, dict):
            continue
        simple_markets.append(
            {
                "id": market.get("id"),
                "slug": market.get("slug"),
                "title": market.get("title") or market.get("question"),
                "closed": market.get("closed"),
                "end_date": market.get("endDate") or market.get("end_date"),
                "volume": market.get("volume"),
                "liquidity": market.get("liquidity"),
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
        "resolution_source": event.get("resolutionSource") or event.get("resolution_source"),
        "markets": simple_markets,
    }


def _resolve_signal_time(
    trigger_context: Dict[str, Any] | None,
    fresh_market_data: Dict[str, Any] | None,
) -> tuple[datetime, str]:
    if isinstance(trigger_context, dict):
        explicit_signal_time = _parse_iso8601_utc(trigger_context.get("signal_time"))
        if explicit_signal_time is not None:
            return explicit_signal_time, "trigger_context.signal_time"

        trigger_payload = trigger_context.get("trigger_payload")
        if isinstance(trigger_payload, dict):
            for key in ("signal_time", "to_ts", "captured_at", "timestamp", "triggered_at"):
                parsed = _parse_iso8601_utc(trigger_payload.get(key))
                if parsed is not None:
                    return parsed, f"trigger_context.trigger_payload.{key}"

    if isinstance(fresh_market_data, dict):
        fresh_captured_at = _parse_iso8601_utc(fresh_market_data.get("captured_at"))
        if fresh_captured_at is not None:
            return fresh_captured_at, "fresh_market_data.captured_at"

    return datetime.now(timezone.utc), "generated_at_fallback"


def _bounded_probability_adjustment(value: Any, max_adjustment: float) -> float:
    """
    Clamp LLM-provided adjustment to a bounded interval.

    `max_adjustment` is controlled by the deterministic prior. In particular,
    legacy payloads may set it to 0.0, meaning the LLM must not move the prior.
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid probability_adjustment value: {value!r}") from exc

    if not math.isfinite(numeric):
        raise ValueError(f"Non-finite probability_adjustment value: {value!r}")

    max_adj = abs(float(max_adjustment))
    return max(-max_adj, min(max_adj, numeric))


def _clamp_probability(value: Any, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = default
    return max(0.0, min(1.0, numeric))


def _deterministic_prior_probability(score: Any, band: Any) -> float:
    try:
        score_value = max(0.0, min(100.0, float(score)))
    except (TypeError, ValueError):
        return 0.5

    band_name = str(band or "").strip().lower()
    band_ranges = {
        "low": (0.05, 0.35, 0.0, 35.0),
        "elevated": (0.30, 0.60, 35.0, 55.0),
        "high": (0.52, 0.78, 55.0, 75.0),
        "severe": (0.70, 0.92, 75.0, 100.0),
    }
    low_prob, high_prob, low_score, high_score = band_ranges.get(
        band_name,
        (0.10, 0.90, 0.0, 100.0),
    )
    span = max(high_score - low_score, 1.0)
    position = max(0.0, min(1.0, (score_value - low_score) / span))
    return round(low_prob + (high_prob - low_prob) * position, 3)


def _metadata_path_for_news(news_path: str | Path) -> Path:
    path = Path(news_path)
    return path.with_suffix(".metadata.json")


def _describe_news_dataset(news_path: str | Path) -> Dict[str, Any]:
    path = Path(news_path)
    metadata_path = _metadata_path_for_news(path)
    result: Dict[str, Any] = {
        "path": str(path),
        "metadata_path": str(metadata_path),
        "exists": path.exists(),
        "metadata_exists": metadata_path.exists(),
    }

    if path.exists():
        stat = path.stat()
        result.update(
            {
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )

    if metadata_path.exists():
        try:
            result["metadata"] = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            result["metadata_error"] = str(exc)

    return result


def _top_component_scores(snapshot: Dict[str, Any], limit: int = 4) -> List[Dict[str, Any]]:
    component_scores = snapshot.get("component_scores")
    if not isinstance(component_scores, dict):
        return []

    scored: list[tuple[str, float]] = []
    for name, value in component_scores.items():
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric <= 0.0:
            continue
        scored.append((str(name), numeric))

    scored.sort(key=lambda item: item[1], reverse=True)
    return [{"name": name, "value": round(value, 4)} for name, value in scored[:limit]]


def _build_evidence_highlights(trigger_payload: Dict[str, Any], snapshot: Dict[str, Any]) -> List[str]:
    highlights: list[str] = []
    for item in _top_component_scores(snapshot):
        highlights.append(f"{item['name']}={item['value']}")

    gating = snapshot.get("gating")
    if isinstance(gating, dict):
        gate_reason = str(gating.get("llm_gate_reason") or "").strip()
        if gate_reason:
            highlights.append(f"llm_gate_reason={gate_reason}")
        if gating.get("pre_news") is True:
            highlights.append("pre_news_signal=true")
        if gating.get("repeated_anomaly") is True:
            highlights.append("repeated_anomaly=true")

    news_delta = trigger_payload.get("news_delta_minutes")
    try:
        news_delta_value = float(news_delta)
    except (TypeError, ValueError):
        news_delta_value = None
    if news_delta_value is not None:
        if news_delta_value > 0:
            highlights.append(f"news_followed_signal_by_{round(news_delta_value, 2)}m")
        elif news_delta_value < 0:
            highlights.append(f"news_led_signal_by_{round(abs(news_delta_value), 2)}m")

    return highlights


def _build_payload_from_trigger(
    event_id: str,
    *,
    news_path: str,
    trigger_context: Dict[str, Any] | None,
    fresh_market_data: Dict[str, Any] | None,
) -> Dict[str, Any] | None:
    if not isinstance(trigger_context, dict):
        return None

    trigger_payload = trigger_context.get("trigger_payload")
    if not isinstance(trigger_payload, dict):
        return None

    snapshot = trigger_payload.get("deterministic_feature_snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}

    prior_probability = _deterministic_prior_probability(
        trigger_payload.get("deterministic_score"),
        trigger_payload.get("deterministic_score_band"),
    )
    signal_time, signal_time_source = _resolve_signal_time(trigger_context, fresh_market_data)

    return {
        "schema_version": EXPLANATION_PAYLOAD_VERSION,
        "prompt_version": PROMPT_VERSION,
        "public_data_only": True,
        "llm_role": "explanation_confidence_refinement",
        "legal_determination": False,
        "event_reference": {
            "event_id": event_id,
            "market_id": trigger_payload.get("market_id"),
            "side": trigger_payload.get("side"),
        },
        "trigger_contract": {
            "spike_id": trigger_payload.get("spike_id"),
            "event_id": trigger_payload.get("event_id") or event_id,
            "market_id": trigger_payload.get("market_id"),
            "side": trigger_payload.get("side"),
            "from_ts": trigger_payload.get("from_ts"),
            "to_ts": trigger_payload.get("to_ts"),
            "deterministic_score": trigger_payload.get("deterministic_score"),
            "deterministic_score_band": trigger_payload.get("deterministic_score_band"),
            "deterministic_feature_snapshot": snapshot,
            "scorer_version": trigger_payload.get("scorer_version"),
            "trigger_type": trigger_payload.get("trigger_type"),
            "signal_time": trigger_payload.get("signal_time") or _isoformat(signal_time),
            "news_time": trigger_payload.get("news_time"),
            "news_delta_minutes": trigger_payload.get("news_delta_minutes"),
            "llm_probability": trigger_payload.get("llm_probability"),
            "llm_confidence": trigger_payload.get("llm_confidence"),
            "llm_summary": trigger_payload.get("llm_summary"),
            "llm_version": trigger_payload.get("llm_version"),
            "prompt_hash": trigger_payload.get("prompt_hash"),
        },
        "deterministic_prior": {
            "probability": prior_probability,
            "max_adjustment": MAX_PROBABILITY_ADJUSTMENT,
            "score": trigger_payload.get("deterministic_score"),
            "band": trigger_payload.get("deterministic_score_band"),
            "scorer_version": trigger_payload.get("scorer_version"),
        },
        "deterministic_evidence": {
            "feature_snapshot_contract": (
                snapshot.get("snapshot_contract_version")
                or FEATURE_SNAPSHOT_CONTRACT_VERSION
            ),
            "component_scores": snapshot.get("component_scores") or {},
            "aggregates": snapshot.get("aggregates") or {},
            "gating": snapshot.get("gating") or {},
            "raw_features": snapshot.get("raw_features") or {},
            "market_context": snapshot.get("market_context") or {},
            "price_context": snapshot.get("price_context") or {},
            "orderbook_context": snapshot.get("orderbook_context") or {},
            "trade_context": snapshot.get("trade_context") or {},
            "open_interest_context": snapshot.get("open_interest_context") or {},
            "news_context": snapshot.get("news_context") or {},
            "evidence_highlights": _build_evidence_highlights(trigger_payload, snapshot),
        },
        "point_in_time_context": {
            "signal_time": trigger_payload.get("signal_time") or _isoformat(signal_time),
            "signal_time_source": signal_time_source,
            "fresh_market_data": _json_safe(fresh_market_data),
            "news_dataset": _describe_news_dataset(news_path),
        },
    }


def _build_legacy_live_payload(
    event_id: str,
    *,
    base_url: str,
    news_path: str = "news_scraper/data/news_events.jsonl",
    include_db_event: bool = True,
    trigger_context: Dict[str, Any] | None = None,
    fresh_market_data: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
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

    fresh_data = fresh_market_data or fetch_fresh_market_data_from_api(
        event_id,
        base_url=base_url,
    )
    signal_time, signal_time_source = _resolve_signal_time(trigger_context, fresh_data)
    news_timing: Optional[NewsTiming] = find_nearest_news_for_event(
        event_id,
        signal_time=signal_time,
        base_url=base_url,
        news_path=news_path,
    )

    def _asdict_list(objs: List[Any]) -> List[Dict[str, Any]]:
        return [asdict(obj) for obj in objs]

    return {
        "schema_version": LEGACY_LIVE_PAYLOAD_VERSION,
        "prompt_version": PROMPT_VERSION,
        "public_data_only": True,
        "llm_role": "legacy_explanation_fallback",
        "legal_determination": False,
        "event": event_simple,
        "event_db": event_db,
        "recent_whale_spikes_db": recent_spikes_db,
        "signal_time": _isoformat(signal_time),
        "signal_time_source": signal_time_source,
        "fresh_market_data": _json_safe(fresh_data),
        "news_dataset": _describe_news_dataset(news_path),
        "legacy_live_features": {
            "volume_stats": asdict(volume),
            "orderbook_imbalance": _asdict_list(orderbooks),
            "open_interest": _asdict_list(oi_snapshots),
            "price_history_stats": _asdict_list(price_stats),
            "news_timing": asdict(news_timing) if news_timing is not None else None,
        },
        "deterministic_prior": {
            "probability": 0.5,
            "max_adjustment": 0.0,
            "score": None,
            "band": None,
            "scorer_version": None,
        },
    }


def _build_explanation_payload(
    event_id: str,
    *,
    base_url: str,
    news_path: str = "news_scraper/data/news_events.jsonl",
    include_db_event: bool = True,
    trigger_context: Dict[str, Any] | None = None,
    fresh_market_data: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    frozen_payload = _build_payload_from_trigger(
        event_id,
        news_path=news_path,
        trigger_context=trigger_context,
        fresh_market_data=fresh_market_data,
    )
    if frozen_payload is not None:
        return frozen_payload

    return _build_legacy_live_payload(
        event_id,
        base_url=base_url,
        news_path=news_path,
        include_db_event=include_db_event,
        trigger_context=trigger_context,
        fresh_market_data=fresh_market_data,
    )


def _build_prompt(features: Dict[str, Any]) -> str:
    features_json = json.dumps(
        features,
        indent=2,
        sort_keys=True,
        default=_json_serial_default,
    )
    return (
        "You are a conservative research analyst for public-data prediction-market anomalies.\n\n"
        "The deterministic scorer is the primary classifier. You are NOT allowed to replace it. "
        "Your role is limited to explanation, confidence refinement, and at most a small bounded "
        "adjustment around the deterministic prior probability.\n\n"
        "Rules:\n"
        "- Use only the provided JSON payload.\n"
        "- Do not make legal accusations or definitive insider-trading claims.\n"
        "- Use phrases like suspicious activity, anomaly, research signal, or public-data signal.\n"
        "- Treat positive `news_delta_minutes` as the signal occurring before the nearby public news.\n"
        "- Treat negative `news_delta_minutes` as the signal occurring after the nearby public news.\n"
        f"- If `deterministic_prior.max_adjustment` is non-zero, `probability_adjustment` must stay within +/-{MAX_PROBABILITY_ADJUSTMENT:.2f}.\n"
        "- If the evidence is incomplete or mixed, keep the adjustment near 0 and lower confidence.\n"
        "- Focus on frozen deterministic evidence and point-in-time context, not current market conditions.\n\n"
        "Return ONLY one JSON object with this exact schema:\n"
        "{\n"
        '  "probability_adjustment": <float>,\n'
        '  "confidence": "low" | "medium" | "high",\n'
        '  "short_summary": "<one or two concise sentences>"\n'
        "}\n\n"
        "Payload:\n"
        f"{features_json}"
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


def _extract_text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _extract_response_content(payload: Dict[str, Any]) -> str:
    if all(key in payload for key in ("probability_adjustment", "confidence", "short_summary")):
        return json.dumps(payload)
    if all(key in payload for key in ("probability_insider", "confidence", "short_summary")):
        return json.dumps(payload)

    message = payload.get("message")
    if isinstance(message, dict):
        content = _extract_text_content(message.get("content"))
        if content.strip():
            return content

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first_choice = choices[0]
        if isinstance(first_choice, dict):
            message = first_choice.get("message")
            if isinstance(message, dict):
                content = _extract_text_content(message.get("content"))
                if content.strip():
                    return content

    content = _extract_text_content(payload.get("response"))
    if content.strip():
        return content

    return ""


def _request_ollama_assessment(
    *,
    client: httpx.Client,
    api_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    prompt: str,
    temperature: float,
    strict_json_retry: bool = False,
) -> Dict[str, Any]:
    user_prompt = prompt
    if strict_json_retry:
        user_prompt = (
            f"{prompt}\n\n"
            "Your previous answer was invalid. Return exactly one valid JSON object and no extra text."
        )

    response = client.post(
        api_url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": temperature},
        },
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"Ollama cloud request failed with status {response.status_code}: {response.text}"
        ) from exc

    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Ollama response had unexpected type {type(payload).__name__}: {payload}"
        )

    content = _extract_response_content(payload)
    if not content.strip():
        raise RuntimeError(f"Ollama response contained no content. Payload was:\n{payload}")

    return _extract_json_object(content)


def _assessment_from_parsed_response(
    parsed: Dict[str, Any],
    *,
    model: str,
    prompt_hash: str,
    prior_probability: float,
    max_adjustment: float,
) -> InsiderAssessment:
    if "probability_adjustment" in parsed:
        if parsed.get("probability_adjustment") is None:
            raise RuntimeError("LLM response missing probability_adjustment")
        bounded_adjustment = _bounded_probability_adjustment(
            parsed.get("probability_adjustment"),
            max_adjustment=max_adjustment,
        )
    elif "probability_insider" in parsed:
        raw_insider = parsed.get("probability_insider")
        if raw_insider is None:
            raise RuntimeError("LLM response missing probability_insider")
        try:
            insider_prob = float(raw_insider)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"LLM response has invalid probability_insider: {raw_insider!r}") from exc
        if not math.isfinite(insider_prob):
            raise RuntimeError(f"LLM response has non-finite probability_insider: {raw_insider!r}")
        insider_prob_clamped = _clamp_probability(insider_prob, default=prior_probability)
        bounded_adjustment = _bounded_probability_adjustment(
            insider_prob_clamped - prior_probability,
            max_adjustment=max_adjustment,
        )
    else:
        raise RuntimeError("LLM response missing probability_adjustment/probability_insider")

    final_probability = prior_probability + bounded_adjustment
    final_probability = _clamp_probability(final_probability, default=prior_probability)

    confidence_raw = parsed.get("confidence")
    if not isinstance(confidence_raw, str):
        raise RuntimeError("LLM response confidence must be a string")
    confidence = confidence_raw.strip().lower()
    if confidence not in {"low", "medium", "high"}:
        raise RuntimeError(f"LLM response confidence invalid: {confidence_raw!r}")

    summary_raw = parsed.get("short_summary")
    if not isinstance(summary_raw, str) or not summary_raw.strip():
        raise RuntimeError("LLM response short_summary must be a non-empty string")
    summary = summary_raw.strip()

    return InsiderAssessment(
        probability_insider=final_probability,
        confidence=confidence,
        short_summary=summary,
        llm_version=model,
        prompt_hash=prompt_hash,
        prompt_version=PROMPT_VERSION,
        deterministic_prior_probability=prior_probability,
        probability_adjustment=bounded_adjustment,
    )


def _fallback_assessment(
    *,
    model: str,
    prompt_hash: str,
    prior_probability: float,
    reason: str,
) -> InsiderAssessment:
    return InsiderAssessment(
        probability_insider=prior_probability,
        confidence="low",
        short_summary=DEFAULT_SUMMARY_FALLBACK,
        llm_version=model,
        prompt_hash=prompt_hash,
        prompt_version=PROMPT_VERSION,
        deterministic_prior_probability=prior_probability,
        probability_adjustment=0.0,
        fallback_reason=reason,
    )


def _assess_with_payload(
    explanation_payload: Dict[str, Any],
    *,
    ollama_host: str,
    ollama_model: str,
    ollama_api_key: str,
    temperature: float,
    event_id: str,
) -> InsiderAssessment:
    prompt = _build_prompt(explanation_payload)
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    prior_probability = _clamp_probability(
        ((explanation_payload.get("deterministic_prior") or {}).get("probability")),
        default=0.5,
    )
    raw_max_adjustment = ((explanation_payload.get("deterministic_prior") or {}).get("max_adjustment"))
    try:
        max_adjustment = abs(float(raw_max_adjustment))
    except (TypeError, ValueError):
        max_adjustment = MAX_PROBABILITY_ADJUSTMENT
    if not math.isfinite(max_adjustment):
        max_adjustment = MAX_PROBABILITY_ADJUSTMENT

    system_prompt = (
        "You explain public-data anomaly evidence. The deterministic scorer remains the "
        "primary classifier, and you must stay conservative and audit-friendly."
    )

    with httpx.Client(timeout=120.0) as client:
        try:
            parsed = _request_ollama_assessment(
                client=client,
                api_url=_ollama_api_url(ollama_host, "chat"),
                api_key=ollama_api_key,
                model=ollama_model,
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=temperature,
            )
        except RuntimeError as first_error:
            print(
                f"[insider-model] Retrying explanation-layer assessment for event {event_id} after malformed response: {first_error}",
                flush=True,
            )
            try:
                parsed = _request_ollama_assessment(
                    client=client,
                    api_url=_ollama_api_url(ollama_host, "chat"),
                    api_key=ollama_api_key,
                    model=ollama_model,
                    system_prompt=system_prompt,
                    prompt=prompt,
                    temperature=temperature,
                    strict_json_retry=True,
                )
            except RuntimeError as retry_error:
                print(
                    f"[insider-model] Explanation-layer fallback used for event {event_id}: {retry_error}",
                    flush=True,
                )
                return _fallback_assessment(
                    model=ollama_model,
                    prompt_hash=prompt_hash,
                    prior_probability=prior_probability,
                    reason="malformed_or_unavailable_response",
                )

    try:
        return _assessment_from_parsed_response(
            parsed,
            model=ollama_model,
            prompt_hash=prompt_hash,
            prior_probability=prior_probability,
            max_adjustment=max_adjustment,
        )
    except (RuntimeError, ValueError) as exc:
        print(
            f"[insider-model] Malformed LLM response for event {event_id}: {exc}",
            flush=True,
        )
        return _fallback_assessment(
            model=ollama_model,
            prompt_hash=prompt_hash,
            prior_probability=prior_probability,
            reason="malformed_or_invalid_llm_response",
        )


def assess_insider_probability_from_payload(
    trigger_payload: Dict[str, Any],
    *,
    event_id: str | None = None,
    ollama_host: Optional[str] = None,
    ollama_api_key: Optional[str] = None,
    model: Optional[str] = None,
    news_path: str = "news_scraper/data/news_events.jsonl",
    temperature: float = 0.1,
    fresh_market_data: Dict[str, Any] | None = None,
) -> InsiderAssessment:
    resolved_event_id = str(
        event_id
        or trigger_payload.get("event_id")
        or trigger_payload.get("market_id")
        or "unknown-event"
    )
    explanation_payload = _build_payload_from_trigger(
        resolved_event_id,
        news_path=news_path,
        trigger_context={"trigger_payload": trigger_payload},
        fresh_market_data=fresh_market_data,
    )
    if explanation_payload is None:
        raise ValueError("Trigger payload must include a deterministic feature snapshot.")

    ollama_host, ollama_model, ollama_api_key = _get_ollama_config(
        host=ollama_host,
        model=model,
        api_key=ollama_api_key,
    )
    return _assess_with_payload(
        explanation_payload,
        ollama_host=ollama_host,
        ollama_model=ollama_model,
        ollama_api_key=ollama_api_key,
        temperature=temperature,
        event_id=resolved_event_id,
    )


def assess_insider_probability_for_event(
    event_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    ollama_host: Optional[str] = None,
    ollama_api_key: Optional[str] = None,
    model: Optional[str] = None,
    news_path: str = "news_scraper/data/news_events.jsonl",
    temperature: float = 0.1,
    include_db_event: bool = True,
    trigger_context: Dict[str, Any] | None = None,
    fresh_market_data: Dict[str, Any] | None = None,
) -> InsiderAssessment:
    ollama_host, ollama_model, ollama_api_key = _get_ollama_config(
        host=ollama_host,
        model=model,
        api_key=ollama_api_key,
    )
    explanation_payload = _build_explanation_payload(
        event_id,
        base_url=base_url,
        news_path=news_path,
        include_db_event=include_db_event,
        trigger_context=trigger_context,
        fresh_market_data=fresh_market_data,
    )
    return _assess_with_payload(
        explanation_payload,
        ollama_host=ollama_host,
        ollama_model=ollama_model,
        ollama_api_key=ollama_api_key,
        temperature=temperature,
        event_id=event_id,
    )


__all__ = [
    "EXPLANATION_PAYLOAD_VERSION",
    "MAX_PROBABILITY_ADJUSTMENT",
    "PROMPT_VERSION",
    "InsiderAssessment",
    "assess_insider_probability_for_event",
    "assess_insider_probability_from_payload",
]
