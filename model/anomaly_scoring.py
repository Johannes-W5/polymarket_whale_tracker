from __future__ import annotations

"""
Deterministic anomaly scoring for public-data market triggers.

The scorer intentionally uses capped, inspectable component formulas so each
output can be explained from public market and news data without relying on an
LLM. Scores are normalized to the 0-100 range.
"""

from dataclasses import asdict, dataclass
from typing import Any


SCORER_VERSION = "deterministic-v1"
FEATURE_SNAPSHOT_CONTRACT_VERSION = "deterministic-feature-snapshot-v1"


def _clamp01(value: float | None) -> float:
    if value is None:
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _safe_float(value: float | None) -> float | None:
    if value is None:
        return None
    return float(value)


@dataclass
class AnomalyScoreInputs:
    price_move_abs: float
    price_move_rel: float
    volatility_adjusted_jump: float | None = None
    liquidity_adjusted_move: float | None = None
    spread_adjusted_move: float | None = None
    directional_orderbook_imbalance: float | None = None
    spread_bps: float | None = None
    depth_near_touch: float | None = None
    trade_count_burst: float | None = None
    volume_burst: float | None = None
    directional_aggressor_imbalance: float | None = None
    open_interest_rel_change: float | None = None
    news_delta_minutes: float | None = None
    recent_anomaly_count: int = 0
    recent_max_score: float | None = None


@dataclass
class DeterministicAnomalyScore:
    deterministic_score: float
    deterministic_score_band: str
    deterministic_feature_snapshot: dict[str, Any]
    scorer_version: str
    trigger_type: str
    should_emit: bool
    should_call_llm: bool
    llm_gate_reason: str


def score_band(score: float) -> str:
    if score >= 75.0:
        return "severe"
    if score >= 55.0:
        return "high"
    if score >= 35.0:
        return "elevated"
    return "low"


def score_anomaly(inputs: AnomalyScoreInputs) -> DeterministicAnomalyScore:
    volatility_component = _clamp01(_safe_float(inputs.volatility_adjusted_jump) / 4.0)
    liquidity_component = _clamp01(_safe_float(inputs.liquidity_adjusted_move) / 0.06)
    spread_component = _clamp01(_safe_float(inputs.spread_adjusted_move) / 4.0)
    orderbook_component = _clamp01(_safe_float(inputs.directional_orderbook_imbalance) / 0.60)
    trade_burst_component = _clamp01(((_safe_float(inputs.trade_count_burst) or 0.0) - 1.0) / 2.0)
    volume_burst_component = _clamp01(((_safe_float(inputs.volume_burst) or 0.0) - 1.0) / 2.0)
    aggressor_component = _clamp01(_safe_float(inputs.directional_aggressor_imbalance) / 0.60)
    oi_component = _clamp01(abs(_safe_float(inputs.open_interest_rel_change) or 0.0) / 0.10)
    news_component = (
        _clamp01(min(float(inputs.news_delta_minutes), 60.0) / 30.0)
        if inputs.news_delta_minutes is not None and inputs.news_delta_minutes > 0
        else 0.0
    )
    repeat_component = _clamp01(float(max(inputs.recent_anomaly_count, 0)) / 3.0)

    price_dislocation = (
        0.45 * volatility_component
        + 0.30 * liquidity_component
        + 0.25 * spread_component
    )
    flow_confirmation = (
        0.40 * trade_burst_component
        + 0.25 * volume_burst_component
        + 0.20 * aggressor_component
        + 0.15 * orderbook_component
    )
    context_confirmation = 0.70 * news_component + 0.30 * repeat_component

    score = round(
        100.0
        * (
            0.50 * price_dislocation
            + 0.25 * flow_confirmation
            + 0.15 * oi_component
            + 0.10 * context_confirmation
        ),
        2,
    )
    band = score_band(score)

    pre_news = inputs.news_delta_minutes is not None and inputs.news_delta_minutes >= 5.0
    repeated_anomaly = inputs.recent_anomaly_count >= 2 and (inputs.recent_max_score or 0.0) >= 35.0

    should_emit = (
        score >= 55.0
        or (
            score >= 40.0
            and (
                trade_burst_component >= 0.35
                or aggressor_component >= 0.45
                or orderbook_component >= 0.45
                or pre_news
            )
        )
    )
    should_call_llm = score >= 40.0

    trigger_type = "deterministic_anomaly"
    llm_gate_reason = "score_below_llm_gate"
    if pre_news and should_emit:
        trigger_type = "pre_news_anomaly"
    elif repeated_anomaly and should_emit:
        trigger_type = "repeat_anomaly"
    if should_call_llm:
        llm_gate_reason = "deterministic_score_gate_passed"
    elif pre_news:
        llm_gate_reason = "pre_news_but_score_below_llm_gate"

    snapshot = {
        "snapshot_contract_version": FEATURE_SNAPSHOT_CONTRACT_VERSION,
        "scorer_version": SCORER_VERSION,
        "public_data_only": True,
        "raw_features": asdict(inputs),
        "component_scores": {
            "volatility_component": volatility_component,
            "liquidity_component": liquidity_component,
            "spread_component": spread_component,
            "orderbook_component": orderbook_component,
            "trade_burst_component": trade_burst_component,
            "volume_burst_component": volume_burst_component,
            "aggressor_component": aggressor_component,
            "open_interest_component": oi_component,
            "news_component": news_component,
            "repeat_component": repeat_component,
        },
        "aggregates": {
            "price_dislocation": round(price_dislocation, 4),
            "flow_confirmation": round(flow_confirmation, 4),
            "context_confirmation": round(context_confirmation, 4),
        },
        "gating": {
            "should_emit": should_emit,
            "should_call_llm": should_call_llm,
            "pre_news": pre_news,
            "repeated_anomaly": repeated_anomaly,
            "llm_gate_reason": llm_gate_reason,
        },
    }

    return DeterministicAnomalyScore(
        deterministic_score=score,
        deterministic_score_band=band,
        deterministic_feature_snapshot=snapshot,
        scorer_version=SCORER_VERSION,
        trigger_type=trigger_type,
        should_emit=should_emit,
        should_call_llm=should_call_llm,
        llm_gate_reason=llm_gate_reason,
    )


__all__ = [
    "SCORER_VERSION",
    "FEATURE_SNAPSHOT_CONTRACT_VERSION",
    "AnomalyScoreInputs",
    "DeterministicAnomalyScore",
    "score_anomaly",
    "score_band",
]
