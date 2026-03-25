from __future__ import annotations

from model.insider_model import (
    MAX_PROBABILITY_ADJUSTMENT,
    PROMPT_VERSION,
    assess_insider_probability_from_payload,
    _assessment_from_parsed_response,
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


def test_assess_from_payload_negative_adjustment_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(
        "model.insider_model._request_ollama_assessment",
        lambda **kwargs: {
            "probability_adjustment": -999.0,
            "confidence": "HIGH",
            "short_summary": "A negative adjustment is bounded to the deterministic prior interval.",
        },
    )

    result = assess_insider_probability_from_payload(
        _trigger_payload(score=82.0, band="severe"),
        ollama_host="https://ollama.example.com",
        ollama_api_key="test-key",
        model="test-model",
    )

    assert result.probability_adjustment == -MAX_PROBABILITY_ADJUSTMENT
    assert result.probability_insider == max(
        0.0,
        result.deterministic_prior_probability - MAX_PROBABILITY_ADJUSTMENT,
    )
    assert result.confidence == "high"


def test_assess_from_payload_missing_probability_key_triggers_fallback(monkeypatch) -> None:
    def _partial(**kwargs):
        return {
            "confidence": "HIGH",
            "short_summary": "Missing probability_adjustment should not be accepted.",
        }

    monkeypatch.setattr("model.insider_model._request_ollama_assessment", _partial)

    result = assess_insider_probability_from_payload(
        _trigger_payload(score=60.0, band="high"),
        ollama_host="https://ollama.example.com",
        ollama_api_key="test-key",
        model="test-model",
    )

    assert result.deterministic_prior_probability is not None
    assert result.probability_insider == result.deterministic_prior_probability
    assert result.probability_adjustment == 0.0
    assert result.fallback_reason == "malformed_or_invalid_llm_response"


def test_assess_from_payload_rejects_nan_probability_adjustment(monkeypatch) -> None:
    monkeypatch.setattr(
        "model.insider_model._request_ollama_assessment",
        lambda **kwargs: {
            "probability_adjustment": "nan",
            "confidence": "HIGH",
            "short_summary": "NaN adjustments must be rejected.",
        },
    )

    result = assess_insider_probability_from_payload(
        _trigger_payload(score=82.0, band="severe"),
        ollama_host="https://ollama.example.com",
        ollama_api_key="test-key",
        model="test-model",
    )

    assert result.deterministic_prior_probability is not None
    assert result.probability_insider == result.deterministic_prior_probability
    assert result.fallback_reason == "malformed_or_invalid_llm_response"


def test_assessment_from_parsed_response_respects_max_adjustment_zero() -> None:
    parsed = {
        "probability_adjustment": 0.5,
        "confidence": "high",
        "short_summary": "Adjustment must be forced to 0 when max_adjustment is 0.",
    }
    result = _assessment_from_parsed_response(
        parsed,
        model="test-model",
        prompt_hash="prompt-hash",
        prior_probability=0.6,
        max_adjustment=0.0,
    )

    assert result.probability_adjustment == 0.0
    assert result.probability_insider == 0.6
