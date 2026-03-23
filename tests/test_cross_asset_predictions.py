from __future__ import annotations

from model.cross_asset_predictions import build_predictions_for_assessment


def _mock_ai(monkeypatch, predictions):
    monkeypatch.setattr(
        "model.cross_asset_predictions._get_ollama_config",
        lambda **kwargs: ("https://ollama.example.com", "test-model", "test-key"),
    )
    monkeypatch.setattr(
        "model.cross_asset_predictions._request_ai_predictions",
        lambda **kwargs: (predictions, "prompt-hash"),
    )


def test_build_predictions_requires_high_score() -> None:
    assessment = {
        "id": 1,
        "event_id": "event-1",
        "spike_id": "spike-1",
        "side": "YES",
        "signal_time": "2026-03-23T12:00:00+00:00",
        "trigger_type": "deterministic_anomaly",
        "deterministic_score": 39.9,
        "deterministic_score_band": "elevated",
        "trigger_payload": {"deterministic_feature_snapshot": {"component_scores": {}}},
    }
    rows = build_predictions_for_assessment(assessment, event_row={"name": "Oil and inflation"})
    assert rows == []


def test_build_predictions_generates_rows_from_ai_output(monkeypatch) -> None:
    _mock_ai(
        monkeypatch,
        predictions=[
            {
                "symbol": "XLE",
                "asset_class": "etf",
                "direction": "up",
                "horizon_bucket": "1d",
                "confidence": 0.82,
                "rationale": "oil and opec terms suggest direct energy-sector sensitivity in next day pricing",
            }
        ],
    )
    assessment = {
        "id": 1,
        "event_id": "event-1",
        "spike_id": "spike-1",
        "side": "YES",
        "signal_time": "2026-03-23T12:00:00+00:00",
        "trigger_type": "pre_news_anomaly",
        "deterministic_score": 82.0,
        "deterministic_score_band": "severe",
        "trigger_payload": {
            "side": "YES",
            "deterministic_feature_snapshot": {
                "component_scores": {"trade_burst_component": 1.0, "volatility_component": 0.8},
                "gating": {"pre_news": True, "repeated_anomaly": False},
            },
        },
    }
    rows = build_predictions_for_assessment(assessment, event_row={"name": "Will crude oil rise after OPEC meeting?"})
    assert rows
    assert rows[0]["asset_symbol"] == "XLE"
    assert rows[0]["horizon_bucket"] == "1d"


def test_build_predictions_rejects_weak_ai_output(monkeypatch) -> None:
    _mock_ai(
        monkeypatch,
        predictions=[
            {
                "symbol": "SPY",
                "asset_class": "equity_index",
                "direction": "up",
                "horizon_bucket": "1d",
                "confidence": 0.95,
                "rationale": "broad market reaction",
            }
        ],
    )
    assessment = {
        "id": 1,
        "event_id": "event-1",
        "spike_id": "spike-1",
        "side": "YES",
        "signal_time": "2026-03-23T12:00:00+00:00",
        "trigger_type": "deterministic_anomaly",
        "deterministic_score": 82.0,
        "deterministic_score_band": "severe",
        "trigger_payload": {"side": "YES", "deterministic_feature_snapshot": {"component_scores": {}}},
    }
    rows = build_predictions_for_assessment(
        assessment,
        event_row={"name": "Will Cardi B and Stefon Diggs get engaged in 2026?"},
    )
    assert rows == []


def test_build_predictions_gate_far_future_resolution(monkeypatch) -> None:
    _mock_ai(
        monkeypatch,
        predictions=[
            {
                "symbol": "USO",
                "asset_class": "commodity",
                "direction": "up",
                "horizon_bucket": "1d",
                "confidence": 0.8,
                "rationale": "oil event would move crude-linked instruments near term",
            }
        ],
    )
    assessment = {
        "id": 1,
        "event_id": "event-1",
        "spike_id": "spike-1",
        "side": "YES",
        "signal_time": "2026-03-23T12:00:00+00:00",
        "trigger_type": "deterministic_anomaly",
        "deterministic_score": 82.0,
        "deterministic_score_band": "severe",
        "trigger_payload": {"side": "YES", "deterministic_feature_snapshot": {"component_scores": {}}},
    }
    rows = build_predictions_for_assessment(
        assessment,
        event_row={
            "name": "Will oil rise in 2030?",
            "endDate": "2030-12-31T23:59:59Z",
        },
    )
    assert rows == []
