from __future__ import annotations

from typing import Any, Dict, List

import model.cross_asset_predictions as cap


class _DummyResponse:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Dict[str, Any]:
        return dict(self._payload)


class _DummyClient:
    def __init__(self, *, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.calls: List[str] = []

    def __enter__(self) -> "_DummyClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(self, url: str) -> _DummyResponse:
        self.calls.append(url)
        # Far-future resolution metadata to force impact_weight <= 0.0
        return _DummyResponse(
            {
                "endDate": "2030-12-31T23:59:59Z",
            }
        )


def test_generate_predictions_enriches_event_row_for_far_future_suppression(monkeypatch) -> None:
    dummy_client = _DummyClient(timeout=30.0)

    def _client_factory(*, timeout: float = 30.0) -> _DummyClient:
        return dummy_client

    assessment = {
        "id": 1,
        "event_id": "event-1",
        "spike_id": "spike-1",
        "side": "YES",
        "signal_time": "2026-03-23T12:00:00+00:00",
        "trigger_type": "deterministic_anomaly",
        "deterministic_score": 82.0,
        "deterministic_score_band": "severe",
        "trigger_payload": {
            "side": "YES",
            "deterministic_feature_snapshot": {
                "component_scores": {},
                "gating": {},
            },
        },
    }

    monkeypatch.setattr(cap, "get_high_score_assessments", lambda **kwargs: [assessment])
    monkeypatch.setattr(cap, "get_event", lambda event_id: {"name": "Test Event"})

    inserted_rows: List[Dict[str, Any]] = []

    def _insert_cross_asset_prediction(**kwargs: Any) -> None:
        inserted_rows.append(dict(kwargs))

    monkeypatch.setattr(cap, "insert_cross_asset_prediction", _insert_cross_asset_prediction)
    monkeypatch.setattr(cap.httpx, "Client", _client_factory)

    # Ensure we don't accidentally hit the AI path; far-future should short-circuit first.
    monkeypatch.setattr(cap, "_get_ollama_config", lambda **kwargs: ("https://ollama.example.com", "test-model", "test-key"))
    monkeypatch.setattr(
        cap,
        "_request_ai_predictions",
        lambda **kwargs: ([{"symbol": "USO", "asset_class": "commodity", "direction": "up", "horizon_bucket": "1d", "confidence": 0.8, "rationale": "oil"}], "prompt-hash"),
    )

    result = cap.generate_predictions(min_score=40.0, since_id=None, limit=10, base_url="http://proxy.local")

    assert result["processed_assessments"] == 1
    assert result["inserted_predictions"] == 0
    assert len(inserted_rows) == 0
    assert dummy_client.calls, "Expected batch mode to call the proxy /events/{id} enrichment."

