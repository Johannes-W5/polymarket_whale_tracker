from __future__ import annotations

from model.insider_model import (
    MAX_PROBABILITY_ADJUSTMENT,
    PROMPT_VERSION,
    assess_insider_probability_from_payload,
)


def _trigger_payload(*, score: float = 82.0, band: str = "severe") -> dict[str, object]:
    return {
        "spike_id": "spike-1",
        "event_id": "event-1",
        "market_id": "market-1",
        "side": "YES",
        "from_ts": "2026-03-22T12:00:00+00:00",
        "to_ts": "2026-03-22T12:05:00+00:00",
        "deterministic_score": score,
        "deterministic_score_band": band,
        "deterministic_feature_snapshot": {
            "snapshot_contract_version": "deterministic-feature-snapshot-v1",
            "component_scores": {
                "volatility_component": 1.0,
                "trade_burst_component": 0.9,
            },
            "gating": {
                "should_emit": True,
                "should_call_llm": True,
                "pre_news": True,
                "llm_gate_reason": "deterministic_score_gate_passed",
            },
            "news_context": {
                "news_time": "2026-03-22T12:15:00+00:00",
                "news_delta_minutes": 10.0,
            },
        },
        "scorer_version": "deterministic-v1",
        "trigger_type": "pre_news_anomaly",
        "signal_time": "2026-03-22T12:05:00+00:00",
        "news_time": "2026-03-22T12:15:00+00:00",
        "news_delta_minutes": 10.0,
    }


def test_assess_from_payload_binds_adjustment_to_deterministic_prior(monkeypatch) -> None:
    monkeypatch.setattr(
        "model.insider_model._request_ollama_assessment",
        lambda **kwargs: {
            "probability_adjustment": 0.50,
            "confidence": "HIGH",
            "short_summary": "Frozen evidence supports a strong pre-news anomaly.",
        },
    )

    result = assess_insider_probability_from_payload(
        _trigger_payload(),
        ollama_host="https://ollama.example.com",
        ollama_api_key="test-key",
        model="test-model",
    )

    assert result.deterministic_prior_probability is not None
    assert result.probability_adjustment == MAX_PROBABILITY_ADJUSTMENT
    assert result.probability_insider == min(
        1.0,
        result.deterministic_prior_probability + MAX_PROBABILITY_ADJUSTMENT,
    )
    assert result.confidence == "high"
    assert result.prompt_version == PROMPT_VERSION


def test_assess_from_payload_fallback_keeps_deterministic_prior(monkeypatch) -> None:
    def _raise(**kwargs):
        raise RuntimeError("bad json")

    monkeypatch.setattr("model.insider_model._request_ollama_assessment", _raise)

    result = assess_insider_probability_from_payload(
        _trigger_payload(score=60.0, band="high"),
        ollama_host="https://ollama.example.com",
        ollama_api_key="test-key",
        model="test-model",
    )

    assert result.deterministic_prior_probability is not None
    assert result.probability_insider == result.deterministic_prior_probability
    assert result.probability_adjustment == 0.0
    assert result.fallback_reason == "malformed_or_unavailable_response"
    assert "deterministic public-data anomaly score" in result.short_summary
