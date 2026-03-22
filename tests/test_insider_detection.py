from __future__ import annotations

from datetime import datetime, timezone

from model.anomaly_scoring import AnomalyScoreInputs, score_anomaly
from model.insider_detection import TriggeredInsiderAssessment, WhaleSpike, monitor_event_and_assess_insider
from model.insider_model import InsiderAssessment


def _make_spike(*, llm_should_invoke: bool, score: float, band: str = "high") -> WhaleSpike:
    ts = datetime(2026, 3, 22, 12, 0, tzinfo=timezone.utc)
    return WhaleSpike(
        event_id="event-1",
        from_ts=ts,
        to_ts=ts,
        side="YES",
        from_price=0.40,
        to_price=0.50,
        abs_change=0.10,
        rel_change=0.25,
        market_id="market-1",
        spike_id="spike-1",
        deterministic_score=score,
        deterministic_score_band=band,
        deterministic_feature_snapshot={"gating": {"should_emit": True}},
        scorer_version="deterministic-v1",
        trigger_type="deterministic_anomaly",
        signal_time=ts,
        llm_should_invoke=llm_should_invoke,
        llm_gate_reason="deterministic_score_gate_passed" if llm_should_invoke else "score_below_llm_gate",
    )


def test_score_anomaly_high_signal_pre_news_calls_llm() -> None:
    result = score_anomaly(
        AnomalyScoreInputs(
            price_move_abs=0.10,
            price_move_rel=0.25,
            volatility_adjusted_jump=6.0,
            liquidity_adjusted_move=0.12,
            spread_adjusted_move=6.0,
            directional_orderbook_imbalance=0.8,
            trade_count_burst=4.0,
            volume_burst=4.0,
            directional_aggressor_imbalance=0.8,
            open_interest_rel_change=0.20,
            news_delta_minutes=12.0,
            recent_anomaly_count=2,
            recent_max_score=60.0,
        )
    )
    assert result.should_emit is True
    assert result.should_call_llm is True
    assert result.trigger_type == "pre_news_anomaly"
    assert result.deterministic_score_band in {"high", "severe"}


def test_score_anomaly_moderate_signal_can_emit_without_llm() -> None:
    result = score_anomaly(
        AnomalyScoreInputs(
            price_move_abs=0.035,
            price_move_rel=0.08,
            volatility_adjusted_jump=2.8,
            liquidity_adjusted_move=0.045,
            spread_adjusted_move=2.5,
            directional_orderbook_imbalance=0.35,
            trade_count_burst=2.2,
            volume_burst=2.0,
            directional_aggressor_imbalance=0.30,
            open_interest_rel_change=0.01,
            news_delta_minutes=None,
            recent_anomaly_count=0,
        )
    )
    assert result.should_emit is True
    assert result.should_call_llm is False
    assert result.deterministic_score_band in {"elevated", "high"}


def test_monitor_event_and_assess_insider_skips_llm_when_gate_fails(monkeypatch) -> None:
    spike = _make_spike(llm_should_invoke=False, score=45.0, band="elevated")
    insert_calls: list[dict[str, object]] = []

    def fake_monitor_event_for_spikes(*args, **kwargs):
        yield spike

    def fake_assess(*args, **kwargs):
        raise AssertionError("LLM should not run when gate fails")

    def fake_insert(**kwargs):
        insert_calls.append(kwargs)

    monkeypatch.setattr("model.insider_detection.monitor_event_for_spikes", fake_monitor_event_for_spikes)
    monkeypatch.setattr("model.insider_detection.insert_whale_spike", lambda *args, **kwargs: None)
    monkeypatch.setattr("model.insider_detection.insert_insider_assessment", fake_insert)
    monkeypatch.setattr("model.insider_detection.assess_insider_probability_for_event", fake_assess)
    monkeypatch.setattr("model.insider_detection.assess_informed_flow_for_spike", lambda *args, **kwargs: None)

    results = list(
        monitor_event_and_assess_insider(
            "event-1",
            base_url="http://localhost:8000",
            skip_active_check=True,
        )
    )

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, TriggeredInsiderAssessment)
    assert result.assessment is None
    assert insert_calls[0]["assessment"] is None


def test_monitor_event_and_assess_insider_runs_llm_when_gate_passes(monkeypatch) -> None:
    spike = _make_spike(llm_should_invoke=True, score=82.0, band="severe")
    called = {"llm": 0}

    def fake_monitor_event_for_spikes(*args, **kwargs):
        yield spike

    def fake_assess(*args, **kwargs):
        called["llm"] += 1
        return InsiderAssessment(
            probability_insider=0.72,
            confidence="high",
            short_summary="Strong public-data anomaly.",
            llm_version="test-model",
            prompt_hash="abc123",
        )

    monkeypatch.setattr("model.insider_detection.monitor_event_for_spikes", fake_monitor_event_for_spikes)
    monkeypatch.setattr("model.insider_detection.insert_whale_spike", lambda *args, **kwargs: None)
    monkeypatch.setattr("model.insider_detection.insert_insider_assessment", lambda **kwargs: None)
    monkeypatch.setattr("model.insider_detection.assess_insider_probability_for_event", fake_assess)
    monkeypatch.setattr("model.insider_detection.assess_informed_flow_for_spike", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "model.insider_detection.fetch_fresh_market_data_from_api",
        lambda *args, **kwargs: {"event_id": "event-1", "captured_at": "2026-03-22T12:00:00+00:00"},
    )

    results = list(
        monitor_event_and_assess_insider(
            "event-1",
            base_url="http://localhost:8000",
            skip_active_check=True,
        )
    )

    assert called["llm"] == 1
    assert results[0].assessment is not None
    assert results[0].assessment.llm_version == "test-model"
