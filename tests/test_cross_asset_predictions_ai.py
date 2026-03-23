from __future__ import annotations

from model.cross_asset_predictions import _validate_ai_predictions


def test_validate_ai_predictions_accepts_concrete_event_linked_symbol() -> None:
    predictions = [
        {
            "symbol": "XLE",
            "asset_class": "etf",
            "direction": "up",
            "horizon_bucket": "1d",
            "confidence": 0.78,
            "rationale": "opec and crude supply wording in this event can lift energy sector pricing",
        }
    ]
    kept = _validate_ai_predictions(
        predictions,
        event_row={"name": "Will crude oil rise after OPEC meeting?"},
        trigger_payload={"deterministic_feature_snapshot": {"news_context": {"news_title": "OPEC supply talks"}}},
    )
    assert len(kept) == 1
    assert kept[0]["symbol"] == "XLE"


def test_validate_ai_predictions_rejects_generic_symbol_without_specificity() -> None:
    predictions = [
        {
            "symbol": "SPY",
            "asset_class": "equity_index",
            "direction": "up",
            "horizon_bucket": "1d",
            "confidence": 0.85,
            "rationale": "broad market should react",
        }
    ]
    kept = _validate_ai_predictions(
        predictions,
        event_row={"name": "Will Cardi B and Stefon Diggs get engaged in 2026?"},
        trigger_payload={"deterministic_feature_snapshot": {"news_context": {}}},
    )
    assert kept == []


def test_validate_ai_predictions_rejects_malformed_fields() -> None:
    predictions = [
        {
            "symbol": "$$$",
            "asset_class": "invalid",
            "direction": "moon",
            "horizon_bucket": "10d",
            "confidence": "high",
            "rationale": "invalid values",
        }
    ]
    kept = _validate_ai_predictions(
        predictions,
        event_row={"name": "Oil supply shock"},
        trigger_payload={"deterministic_feature_snapshot": {"news_context": {"news_title": "oil shock"}}},
    )
    assert kept == []
