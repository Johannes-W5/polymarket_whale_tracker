from __future__ import annotations

from datetime import datetime, timezone

from model.anomaly_scoring import AnomalyScoreInputs, score_anomaly
from model.insider_detection import TriggeredInsiderAssessment, WhaleSpike, monitor_event_and_assess_insider
from model.insider_model import InsiderAssessment
from model.insider_detection import PriceSample
from model.anomaly_scoring import DeterministicAnomalyScore
from model.market_signals import MarketMetadata, OpenInterestSnapshot


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


def test_score_anomaly_moderate_signal_emits_and_passes_llm_gate() -> None:
    """Score >= 40 both emits (via flow confirmation) and passes the LLM gate."""
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
    assert result.should_call_llm is True
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


def test_score_spike_candidate_passes_min_news_lead_minutes(monkeypatch) -> None:
    """
    Deterministic pre-news gating must respect the configured `min_news_lead_minutes`,
    not a hardcoded constant.
    """
    import model.insider_detection as det

    captured: dict[str, object] = {}

    def fake_score_anomaly(inputs: AnomalyScoreInputs) -> DeterministicAnomalyScore:
        captured["min_news_lead_minutes"] = inputs.min_news_lead_minutes
        return DeterministicAnomalyScore(
            deterministic_score=50.0,
            deterministic_score_band="high",
            deterministic_feature_snapshot={"gating": {"should_emit": True, "should_call_llm": False}},
            scorer_version="deterministic-v1",
            trigger_type="deterministic_anomaly",
            should_emit=True,
            should_call_llm=False,
            llm_gate_reason="test",
        )

    # Avoid network/data dependency: keep all feature builders empty/None.
    monkeypatch.setattr(det, "score_anomaly", fake_score_anomaly)
    monkeypatch.setattr(det, "fetch_primary_market_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(det, "compute_orderbook_imbalance_for_event", lambda *args, **kwargs: [])
    monkeypatch.setattr(det, "compute_trade_burst_stats", lambda *args, **kwargs: None)
    monkeypatch.setattr(det, "compute_price_history_stats_for_event", lambda *args, **kwargs: [])
    monkeypatch.setattr(det, "fetch_open_interest_for_event", lambda *args, **kwargs: [])
    monkeypatch.setattr(det, "_safe_fetch_news", lambda *args, **kwargs: None)

    ts = datetime(2026, 3, 22, 12, 0, tzinfo=timezone.utc)
    spike = WhaleSpike(
        event_id="event-1",
        from_ts=ts,
        to_ts=ts,
        side="YES",
        from_price=0.40,
        to_price=0.45,
        abs_change=0.05,
        rel_change=0.125,
        market_id="market-1",
        spike_id="spike-1",
        news_delta_minutes=6.0,
        market_liquidity=25_000.0,
        market_volume=1_000.0,
    )
    signal_sample = PriceSample(
        event_id="event-1",
        captured_at=ts,
        yes_price=0.40,
        no_price=0.60,
        market_id="market-1",
        market_title="test",
        market_liquidity=25_000.0,
        market_volume=1_000.0,
        yes_token_id="token-yes",
        no_token_id="token-no",
    )

    det._score_spike_candidate(
        spike,
        event_id="event-1",
        signal_sample=signal_sample,
        base_url="http://localhost:8000",
        news_path="news.jsonl",
        news_window_minutes=240.0,
        min_news_lead_minutes=10.0,
        request_timeout=30.0,
        prev_open_interest_by_market={},
        recent_anomalies=[],
    )

    assert captured["min_news_lead_minutes"] == 10.0


def test_score_spike_candidate_oi_fallback_uses_condition_market_id(monkeypatch) -> None:
    """
    If `spike.market_id` and the Data API `/oi` market identifier differ,
    `_score_spike_candidate` must still compute `open_interest_rel_change`.
    """
    import model.insider_detection as det

    captured: dict[str, object] = {}

    def fake_score_anomaly(inputs: AnomalyScoreInputs) -> DeterministicAnomalyScore:
        captured["open_interest_rel_change"] = inputs.open_interest_rel_change
        return DeterministicAnomalyScore(
            deterministic_score=50.0,
            deterministic_score_band="high",
            deterministic_feature_snapshot={"gating": {"should_emit": True, "should_call_llm": False}},
            scorer_version="deterministic-v1",
            trigger_type="deterministic_anomaly",
            should_emit=True,
            should_call_llm=False,
            llm_gate_reason="test",
        )

    monkeypatch.setattr(det, "score_anomaly", fake_score_anomaly)
    monkeypatch.setattr(det, "compute_orderbook_imbalance_for_event", lambda *args, **kwargs: [])
    monkeypatch.setattr(det, "compute_trade_burst_stats", lambda *args, **kwargs: None)
    monkeypatch.setattr(det, "compute_price_history_stats_for_event", lambda *args, **kwargs: [])
    monkeypatch.setattr(det, "_safe_fetch_news", lambda *args, **kwargs: None)

    # OI snapshots keyed by condition id.
    monkeypatch.setattr(
        det,
        "fetch_open_interest_for_event",
        lambda *args, **kwargs: [
            OpenInterestSnapshot(event_id="event-1", market_id="cond-1", value=110.0)
        ],
    )

    # Market metadata returns a different `market_id` (price sampling) but the condition id used by OI.
    monkeypatch.setattr(
        det,
        "fetch_primary_market_metadata",
        lambda *args, **kwargs: MarketMetadata(
            event_id="event-1",
            market_id="id-1",
            condition_market_id="cond-1",
            title="test",
            liquidity=25_000.0,
            volume=1_000.0,
            yes_token_id="token-yes",
            no_token_id="token-no",
        ),
    )

    ts = datetime(2026, 3, 22, 12, 0, tzinfo=timezone.utc)
    spike = WhaleSpike(
        event_id="event-1",
        from_ts=ts,
        to_ts=ts,
        side="YES",
        from_price=0.40,
        to_price=0.45,
        abs_change=0.05,
        rel_change=0.125,
        market_id="id-1",
        spike_id="spike-1",
        news_delta_minutes=6.0,
        market_liquidity=25_000.0,
        market_volume=1_000.0,
    )
    signal_sample = PriceSample(
        event_id="event-1",
        captured_at=ts,
        yes_price=0.40,
        no_price=0.60,
        market_id="id-1",
        market_title="test",
        market_liquidity=25_000.0,
        market_volume=1_000.0,
        yes_token_id="token-yes",
        no_token_id="token-no",
    )

    prev_open_interest_by_market = {
        "cond-1": OpenInterestSnapshot(event_id="event-1", market_id="cond-1", value=100.0)
    }

    det._score_spike_candidate(
        spike,
        event_id="event-1",
        signal_sample=signal_sample,
        base_url="http://localhost:8000",
        news_path="news.jsonl",
        news_window_minutes=240.0,
        min_news_lead_minutes=10.0,
        request_timeout=30.0,
        prev_open_interest_by_market=prev_open_interest_by_market,
        recent_anomalies=[],
    )

    assert captured["open_interest_rel_change"] == 0.1
