from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from database.events import (
    get_event,
    get_high_score_assessments,
    insert_cross_asset_prediction,
)

MODEL_VERSION = "cross-asset-ai-v1"
MIN_TRIGGER_SCORE = 40.0
HORIZON_BUCKETS = ("intraday", "1d", "3d-5d")
ALLOWED_ASSET_CLASSES = {
    "single_stock",
    "equity_index",
    "commodity",
    "fx",
    "rates",
    "crypto",
    "etf",
    "other",
}
ALLOWED_DIRECTIONS = {"up", "down", "neutral"}
MAX_PREDICTIONS_PER_SPIKE = 5
MIN_CONFIDENCE = 0.40
MIN_RATIONALE_CHARS = 20
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,14}$")
GENERIC_SYMBOLS = {"SPY", "QQQ", "DIA", "IWM"}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _parse_iso8601_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolution_datetime(event_row: dict[str, Any] | None) -> datetime | None:
    if not isinstance(event_row, dict):
        return None
    for key in ("end_date", "endDate", "resolution_date", "resolutionDate"):
        parsed = _parse_iso8601_utc(event_row.get(key))
        if parsed is not None:
            return parsed
    markets = event_row.get("markets")
    if isinstance(markets, list):
        for market in markets:
            if not isinstance(market, dict):
                continue
            for key in ("end_date", "endDate", "resolution_date", "resolutionDate"):
                parsed = _parse_iso8601_utc(market.get(key))
                if parsed is not None:
                    return parsed
    return None


def _resolution_impact_weight(
    *,
    signal_time: datetime | None,
    event_row: dict[str, Any] | None,
) -> float:
    resolution_ts = _resolution_datetime(event_row)
    if resolution_ts is None:
        return 1.0
    reference_ts = signal_time or datetime.now(timezone.utc)
    days_to_resolution = (resolution_ts - reference_ts).total_seconds() / 86_400.0
    if days_to_resolution <= 30:
        return 1.0
    if days_to_resolution <= 90:
        return 0.7
    if days_to_resolution <= 180:
        return 0.45
    if days_to_resolution <= 365:
        return 0.25
    return 0.0


def _top_components(snapshot: dict[str, Any], limit: int = 4) -> list[dict[str, Any]]:
    components = snapshot.get("component_scores")
    if not isinstance(components, dict):
        return []
    scored: list[tuple[str, float]] = []
    for name, raw in components.items():
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value <= 0.0:
            continue
        scored.append((str(name), value))
    scored.sort(key=lambda item: item[1], reverse=True)
    return [{"name": name, "value": round(value, 4)} for name, value in scored[:limit]]


def _magnitude_from_score(score: float) -> str:
    if score >= 85.0:
        return "large"
    if score >= 75.0:
        return "medium"
    return "small"


def _horizon_adjustment(snapshot: dict[str, Any], horizon_bucket: str) -> float:
    gating = snapshot.get("gating") if isinstance(snapshot, dict) else {}
    if not isinstance(gating, dict):
        gating = {}
    pre_news = bool(gating.get("pre_news"))
    repeated = bool(gating.get("repeated_anomaly"))

    if horizon_bucket == "intraday":
        return 0.05 if pre_news else 0.02
    if horizon_bucket == "1d":
        return 0.05 if repeated else 0.03
    if horizon_bucket == "3d-5d":
        return 0.02 if repeated else -0.02
    return 0.0


def _get_ollama_config(
    *,
    host: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> tuple[str, str, str]:
    resolved_host = (host or os.getenv("OLLAMA_HOST") or "https://ollama.com").rstrip("/")
    resolved_model = model or os.getenv("CROSS_ASSET_OLLAMA_MODEL") or os.getenv("OLLAMA_MODEL") or "qwen3.5:cloud"
    resolved_api_key = api_key or os.getenv("OLLAMA_API_KEY")
    if not resolved_api_key:
        raise RuntimeError("OLLAMA_API_KEY is required for AI cross-asset predictions.")
    return resolved_host, resolved_model, resolved_api_key


def _ollama_api_url(host: str, path: str) -> str:
    base = host.rstrip("/")
    if base.endswith("/api"):
        return f"{base}/{path.lstrip('/')}"
    return f"{base}/api/{path.lstrip('/')}"


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _extract_text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    chunks.append(text)
        return "".join(chunks)
    return ""


def _extract_response_content(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("predictions"), list):
        return json.dumps(payload)
    message = payload.get("message")
    if isinstance(message, dict):
        content = _extract_text_content(message.get("content"))
        if content.strip():
            return content
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            msg = first.get("message")
            if isinstance(msg, dict):
                content = _extract_text_content(msg.get("content"))
                if content.strip():
                    return content
    content = _extract_text_content(payload.get("response"))
    return content


def _extract_json_object(text: str) -> dict[str, Any]:
    content = (text or "").strip()
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError("AI response did not contain a valid JSON object.")
    parsed = json.loads(content[start : end + 1])
    if not isinstance(parsed, dict):
        raise RuntimeError("AI response JSON must be an object.")
    return parsed


def _event_terms(event_row: dict[str, Any] | None, trigger_payload: dict[str, Any]) -> set[str]:
    raw_values: list[str] = []
    source = event_row or {}
    for key in ("title", "name", "description", "category", "subCategory", "sub_category", "slug"):
        value = source.get(key)
        if isinstance(value, str):
            raw_values.append(value.lower())
    news_ctx = (trigger_payload.get("deterministic_feature_snapshot") or {}).get("news_context")
    if isinstance(news_ctx, dict):
        for key in ("news_title", "news_source"):
            value = news_ctx.get(key)
            if isinstance(value, str):
                raw_values.append(value.lower())
    tokenized = re.findall(r"[a-z0-9]{3,}", " ".join(raw_values))
    return set(tokenized)


def _build_ai_payload(
    *,
    assessment_row: dict[str, Any],
    trigger_payload: dict[str, Any],
    event_row: dict[str, Any] | None,
    top_components: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "cross-asset-ai-payload-v1",
        "task": "predict_concrete_affected_assets",
        "constraints": {
            "max_predictions": MAX_PREDICTIONS_PER_SPIKE,
            "allowed_horizons": list(HORIZON_BUCKETS),
            "allowed_directions": sorted(ALLOWED_DIRECTIONS),
            "allowed_asset_classes": sorted(ALLOWED_ASSET_CLASSES),
            "return_empty_if_uncertain": True,
        },
        "event": _json_safe(event_row or {}),
        "trigger": _json_safe(trigger_payload),
        "assessment_context": {
            "event_id": assessment_row.get("event_id"),
            "deterministic_score": assessment_row.get("deterministic_score"),
            "deterministic_score_band": assessment_row.get("deterministic_score_band"),
            "top_components": top_components,
        },
    }


def _build_ai_prompt(payload: dict[str, Any]) -> str:
    payload_json = json.dumps(payload, indent=2, sort_keys=True)
    return (
        "You are a market-impact analyst.\n"
        "Goal: map this prediction-market spike to concrete tradable assets that could be affected.\n"
        "Rules:\n"
        "- Return only assets with clear causal linkage to the event and spike evidence.\n"
        "- Do not return random or generic broad-market symbols unless explicitly justified by event details.\n"
        "- If linkage is weak, return an empty predictions array.\n"
        "- Keep outputs concise and structured.\n\n"
        "Return ONLY one JSON object with schema:\n"
        "{\n"
        '  "predictions": [\n'
        "    {\n"
        '      "symbol": "STRING",\n'
        '      "asset_class": "single_stock|equity_index|commodity|fx|rates|crypto|etf|other",\n'
        '      "direction": "up|down|neutral",\n'
        '      "horizon_bucket": "intraday|1d|3d-5d",\n'
        '      "confidence": 0.0,\n'
        '      "rationale": "Short evidence-based rationale"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Payload:\n"
        f"{payload_json}"
    )


def _request_ai_predictions(
    *,
    payload: dict[str, Any],
    host: str,
    model: str,
    api_key: str,
) -> tuple[list[dict[str, Any]], str]:
    prompt = _build_ai_prompt(payload)
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    system_prompt = (
        "You return high-precision asset impact mappings from structured event context. "
        "Avoid weakly justified assets."
    )
    with httpx.Client(timeout=90.0) as client:
        response = client.post(
            _ollama_api_url(host, "chat"),
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1},
            },
        )
        response.raise_for_status()
        raw = response.json()
    if not isinstance(raw, dict):
        raise RuntimeError("AI response payload must be an object.")
    content = _extract_response_content(raw)
    if not content.strip():
        raise RuntimeError("AI response was empty.")
    parsed = _extract_json_object(content)
    predictions = parsed.get("predictions")
    if not isinstance(predictions, list):
        raise RuntimeError("AI response missing 'predictions' list.")
    return [item for item in predictions if isinstance(item, dict)], prompt_hash


def _validate_ai_predictions(
    predictions: list[dict[str, Any]],
    *,
    event_row: dict[str, Any] | None,
    trigger_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    terms = _event_terms(event_row, trigger_payload)
    for raw in predictions:
        if len(kept) >= MAX_PREDICTIONS_PER_SPIKE:
            break
        symbol = str(raw.get("symbol") or "").upper().strip()
        if not symbol or not SYMBOL_PATTERN.match(symbol):
            continue
        asset_class = str(raw.get("asset_class") or "").strip().lower()
        if asset_class not in ALLOWED_ASSET_CLASSES:
            continue
        direction = str(raw.get("direction") or "").strip().lower()
        if direction not in ALLOWED_DIRECTIONS:
            continue
        horizon = str(raw.get("horizon_bucket") or "").strip().lower()
        if horizon not in HORIZON_BUCKETS:
            continue
        try:
            confidence = _clamp01(float(raw.get("confidence")))
        except (TypeError, ValueError):
            continue
        if confidence < MIN_CONFIDENCE:
            continue
        rationale = str(raw.get("rationale") or "").strip()
        if len(rationale) < MIN_RATIONALE_CHARS:
            continue
        rationale_tokens = set(re.findall(r"[a-z0-9]{3,}", rationale.lower()))
        term_overlap = len(terms & rationale_tokens)
        if term_overlap == 0:
            continue
        if symbol in GENERIC_SYMBOLS and term_overlap < 2:
            continue
        dedupe_key = (symbol, horizon)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        kept.append(
            {
                "symbol": symbol,
                "asset_class": asset_class,
                "direction": direction,
                "horizon_bucket": horizon,
                "confidence": confidence,
                "rationale": rationale,
            }
        )
    return kept


def build_predictions_for_assessment(
    assessment_row: dict[str, Any],
    *,
    event_row: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    trigger_payload = assessment_row.get("trigger_payload")
    if not isinstance(trigger_payload, dict):
        return []

    try:
        score = float(assessment_row.get("deterministic_score"))
    except (TypeError, ValueError):
        return []
    if score < MIN_TRIGGER_SCORE:
        return []

    snapshot = trigger_payload.get("deterministic_feature_snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}

    signal_time = _parse_iso8601_utc(assessment_row.get("signal_time"))
    impact_weight = _resolution_impact_weight(signal_time=signal_time, event_row=event_row)
    if impact_weight <= 0.0:
        return []

    top_components = _top_components(snapshot)
    ai_payload = _build_ai_payload(
        assessment_row=assessment_row,
        trigger_payload=trigger_payload,
        event_row=event_row,
        top_components=top_components,
    )
    host, model, api_key = _get_ollama_config()
    raw_predictions, prompt_hash = _request_ai_predictions(
        payload=ai_payload,
        host=host,
        model=model,
        api_key=api_key,
    )
    valid_predictions = _validate_ai_predictions(
        raw_predictions,
        event_row=event_row,
        trigger_payload=trigger_payload,
    )
    if not valid_predictions:
        return []

    # Keep confidence scaling stable even with a lower test threshold.
    score_norm = _clamp01((score - MIN_TRIGGER_SCORE) / 60.0)
    magnitude = _magnitude_from_score(score)
    if impact_weight < 0.5:
        magnitude = "small"

    rows: list[dict[str, Any]] = []
    for pred in valid_predictions:
        horizon = pred["horizon_bucket"]
        base_confidence = _clamp01(0.45 + 0.45 * score_norm + _horizon_adjustment(snapshot, horizon))
        confidence = _clamp01(min(base_confidence, pred["confidence"]) * impact_weight)
        rows.append(
            {
                "assessment_id": assessment_row.get("id"),
                "event_id": str(assessment_row.get("event_id") or ""),
                "spike_id": assessment_row.get("spike_id"),
                "asset_symbol": pred["symbol"],
                "asset_class": pred["asset_class"],
                "horizon_bucket": horizon,
                "predicted_direction": pred["direction"],
                "predicted_magnitude_band": magnitude,
                "prediction_confidence": confidence,
                "rationale_components": top_components,
                "model_version": MODEL_VERSION,
                "source_score": score,
                "source_score_band": assessment_row.get("deterministic_score_band"),
                "signal_time": assessment_row.get("signal_time"),
                "metadata": {
                    "trigger_type": assessment_row.get("trigger_type"),
                    "resolution_impact_weight": round(impact_weight, 4),
                    "ai_rationale": pred["rationale"],
                    "prompt_hash": prompt_hash,
                    "ai_model": model,
                },
            }
        )
    return rows


def generate_predictions(
    *,
    min_score: float = MIN_TRIGGER_SCORE,
    since_id: int | None = None,
    limit: int = 500,
) -> dict[str, int]:
    assessments = get_high_score_assessments(min_score=min_score, since_id=since_id, limit=limit) or []
    inserted = 0
    processed = 0
    for assessment in assessments:
        row = dict(assessment)
        processed += 1
        event_id = str(row.get("event_id") or "").strip()
        event_row = dict(get_event(event_id) or {}) if event_id else {}
        predictions = build_predictions_for_assessment(row, event_row=event_row)
        for prediction in predictions:
            insert_cross_asset_prediction(**prediction)
            inserted += 1
    return {"processed_assessments": processed, "inserted_predictions": inserted}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate deterministic cross-asset consequence alerts from high-score assessments."
    )
    parser.add_argument("--min-score", type=float, default=MIN_TRIGGER_SCORE, help="Minimum deterministic score.")
    parser.add_argument("--since-id", type=int, default=None, help="Only process assessments with id greater than this.")
    parser.add_argument("--limit", type=int, default=500, help="Max assessments to process per run.")
    args = parser.parse_args()

    result = generate_predictions(min_score=args.min_score, since_id=args.since_id, limit=args.limit)
    now = datetime.utcnow().isoformat()
    print(
        f"[{now}] processed_assessments={result['processed_assessments']} "
        f"inserted_predictions={result['inserted_predictions']}"
    )
