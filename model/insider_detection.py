from __future__ import annotations

"""
Deterministic anomaly detection for public Polymarket market/news data.

This module:
1) Polls public yes/no prices for an event.
2) Turns candidate moves into deterministic anomaly scores using market/news data.
3) Uses the deterministic score to decide whether an LLM assessment is worth running.
"""

import hashlib
import math
import os
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import sleep
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

import httpx

from database.events import (
    get_event,
    insert_cross_asset_prediction,
    insert_insider_assessment,
    insert_whale_spike,
)

try:
    from openai import RateLimitError as OpenAIRateLimitError
except ImportError:
    OpenAIRateLimitError = None  # type: ignore[misc, assignment]

DEFAULT_MIN_ABS_CHANGE = 0.01
DEFAULT_MIN_REL_CHANGE = 0.02
ALL_EVENTS_MIN_ABS_CHANGE = 0.015
ALL_EVENTS_MIN_REL_CHANGE = 0.03
DEFAULT_ASSESSMENT_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:cloud")
DEFAULT_ASSESSMENT_TEMPERATURE = 0.1
RECENT_ANOMALY_WINDOW_MINUTES = 60.0

try:  # pragma: no cover - import fallback
    from .anomaly_scoring import (  # type: ignore[relative-beyond-top-level]
        AnomalyScoreInputs,
        FEATURE_SNAPSHOT_CONTRACT_VERSION,
        score_anomaly,
    )
except ImportError:  # pragma: no cover
    from model.anomaly_scoring import (
        AnomalyScoreInputs,
        FEATURE_SNAPSHOT_CONTRACT_VERSION,
        score_anomaly,
    )

try:  # pragma: no cover - import fallback
    from .event_prices import EventPrices, get_event_prices  # type: ignore[relative-beyond-top-level]
except ImportError:  # pragma: no cover
    from model.event_prices import EventPrices, get_event_prices

try:  # pragma: no cover - import fallback
    from .fresh_data import fetch_fresh_market_data_from_api  # type: ignore[relative-beyond-top-level]
except ImportError:  # pragma: no cover
    from model.fresh_data import fetch_fresh_market_data_from_api

try:  # pragma: no cover - import fallback
    from .insider_model import InsiderAssessment, assess_insider_probability_for_event  # type: ignore[relative-beyond-top-level]
except ImportError:  # pragma: no cover
    from model.insider_model import InsiderAssessment, assess_insider_probability_for_event

try:  # pragma: no cover - import fallback
    from .market_signals import (  # type: ignore[relative-beyond-top-level]
        NewsTiming,
        OpenInterestSnapshot,
        OrderbookImbalance,
        PriceHistoryStats,
        TradeBurstStats,
        compute_open_interest_change,
        compute_orderbook_imbalance_for_event,
        compute_price_history_stats_for_event,
        compute_trade_burst_stats,
        fetch_open_interest_for_event,
        fetch_primary_market_metadata,
        find_nearest_news_for_event,
    )
except ImportError:  # pragma: no cover
    from model.market_signals import (
        NewsTiming,
        OpenInterestSnapshot,
        OrderbookImbalance,
        PriceHistoryStats,
        TradeBurstStats,
        compute_open_interest_change,
        compute_orderbook_imbalance_for_event,
        compute_price_history_stats_for_event,
        compute_trade_burst_stats,
        fetch_open_interest_for_event,
        fetch_primary_market_metadata,
        find_nearest_news_for_event,
    )

try:  # pragma: no cover - import fallback
    from .cross_asset_predictions import build_predictions_for_assessment  # type: ignore[relative-beyond-top-level]
except ImportError:  # pragma: no cover
    from model.cross_asset_predictions import build_predictions_for_assessment


@dataclass
class PriceSample:
    """Snapshot of yes/no prices for an event at a specific time."""

    event_id: str
    captured_at: datetime
    yes_price: float | None
    no_price: float | None
    market_id: str | None = None
    market_title: str | None = None
    market_liquidity: float | None = None
    market_volume: float | None = None
    yes_token_id: str | None = None
    no_token_id: str | None = None

    @classmethod
    def from_event_prices(cls, event_id: str, prices: EventPrices) -> "PriceSample":
        return cls(
            event_id=event_id,
            captured_at=datetime.now(timezone.utc),
            yes_price=prices.yes_price,
            no_price=prices.no_price,
            market_id=prices.market_id,
            market_title=prices.market_title,
            market_liquidity=prices.market_liquidity,
            market_volume=prices.market_volume,
            yes_token_id=prices.yes_token_id,
            no_token_id=prices.no_token_id,
        )


@dataclass
class WhaleSpike:
    """Deterministically scored anomaly between two consecutive samples."""

    event_id: str
    from_ts: datetime
    to_ts: datetime
    side: str
    from_price: float
    to_price: float
    abs_change: float
    rel_change: float
    market_id: str | None = None
    spike_id: str | None = None
    deterministic_score: float | None = None
    deterministic_score_band: str | None = None
    deterministic_feature_snapshot: dict[str, Any] | None = None
    scorer_version: str | None = None
    trigger_type: str = "deterministic_anomaly"
    signal_time: datetime | None = None
    news_time: datetime | None = None
    news_delta_minutes: float | None = None
    llm_should_invoke: bool = False
    llm_gate_reason: str | None = None
    market_liquidity: float | None = None
    market_volume: float | None = None

    def to_payload(self, *, assessment: InsiderAssessment | None = None) -> dict[str, Any]:
        return _spike_trigger_payload(self, assessment=assessment)


@dataclass
class InformedFlowSignal:
    """Anomaly that appears to lead nearby public news."""

    event_id: str
    spike: WhaleSpike
    lead_minutes: float
    news_title: str
    news_source: str
    news_time: datetime


@dataclass
class TriggeredInsiderAssessment:
    """Persistable output row for a deterministic trigger and optional LLM review."""

    event_id: str
    trigger_type: str
    spike: WhaleSpike
    informed_flow: InformedFlowSignal | None
    assessment: InsiderAssessment | None

    def to_payload(self) -> dict[str, Any]:
        if self.informed_flow is not None:
            return _informed_flow_trigger_payload(self.informed_flow, assessment=self.assessment)
        payload = self.spike.to_payload(assessment=self.assessment)
        payload["trigger_type"] = self.trigger_type
        return payload


def _isoformat(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _event_is_currently_active(
    event_id: str,
    *,
    base_url: str,
    timeout: float = 30.0,
) -> bool:
    base = base_url.rstrip("/")
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{base}/events/{event_id}")
        r.raise_for_status()
        event = r.json()
    if not isinstance(event, dict):
        return False
    return bool(event.get("active")) and not bool(event.get("closed", False))


def _make_spike_id(spike: WhaleSpike) -> str:
    raw = "|".join(
        [
            spike.event_id,
            spike.market_id or "",
            spike.side,
            _isoformat(spike.from_ts) or "",
            _isoformat(spike.to_ts) or "",
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def detect_spike_between(
    prev_sample: PriceSample,
    curr_sample: PriceSample,
    *,
    min_abs_change: float = DEFAULT_MIN_ABS_CHANGE,
    min_rel_change: float = DEFAULT_MIN_REL_CHANGE,
) -> list[WhaleSpike]:
    """
    Detect candidate moves between two samples.

    `min_abs_change` and `min_rel_change` now act as low-signal floors only.
    The main emission logic lives in deterministic anomaly scoring.
    """
    spikes: list[WhaleSpike] = []

    def _check_side(side: str, prev: Optional[float], curr: Optional[float]) -> None:
        if prev is None or curr is None or prev <= 0:
            return
        abs_change = curr - prev
        rel_change = abs(abs_change) / prev
        if abs(abs_change) < min_abs_change and rel_change < min_rel_change:
            return
        spike = WhaleSpike(
            event_id=curr_sample.event_id,
            from_ts=prev_sample.captured_at,
            to_ts=curr_sample.captured_at,
            side=side,
            from_price=prev,
            to_price=curr,
            abs_change=abs_change,
            rel_change=rel_change,
            market_id=curr_sample.market_id or prev_sample.market_id,
            signal_time=curr_sample.captured_at,
            market_liquidity=curr_sample.market_liquidity,
            market_volume=curr_sample.market_volume,
        )
        spike.spike_id = _make_spike_id(spike)
        spikes.append(spike)

    _check_side("YES", prev_sample.yes_price, curr_sample.yes_price)
    _check_side("NO", prev_sample.no_price, curr_sample.no_price)
    return spikes


def iter_price_samples(
    event_id: str,
    *,
    base_url: str,
    interval_seconds: float = 5.0,
    side: str = "BUY",
    request_timeout: float = 30.0,
) -> Iterator[PriceSample]:
    while True:
        try:
            prices = get_event_prices(
                event_id,
                base_url=base_url,
                side=side,
                timeout=request_timeout,
            )
            yield PriceSample.from_event_prices(event_id, prices)
        except Exception as exc:
            print(f"[whale-tracking] Skipping sample for event {event_id}: {exc}", flush=True)
        sleep(interval_seconds)


def _select_side_token(sample: PriceSample, side: str) -> str | None:
    return sample.yes_token_id if side == "YES" else sample.no_token_id


def _select_price_stats(price_stats: list[PriceHistoryStats], token_id: str | None) -> PriceHistoryStats | None:
    if token_id is None:
        return None
    for stat in price_stats:
        if stat.market_id == token_id:
            return stat
    return None


def _select_orderbook(orderbooks: list[OrderbookImbalance], side: str) -> OrderbookImbalance | None:
    for orderbook in orderbooks:
        if orderbook.side == side:
            return orderbook
    return None


def _prune_recent_anomalies(
    anomalies: list[WhaleSpike],
    *,
    now: datetime,
    window_minutes: float = RECENT_ANOMALY_WINDOW_MINUTES,
) -> list[WhaleSpike]:
    cutoff = now - timedelta(minutes=window_minutes)
    return [spike for spike in anomalies if spike.to_ts >= cutoff]


def _safe_fetch_news(
    event_id: str,
    *,
    signal_time: datetime,
    base_url: str,
    news_path: str,
    window_minutes: float,
) -> NewsTiming | None:
    try:
        return find_nearest_news_for_event(
            event_id,
            signal_time=signal_time,
            base_url=base_url,
            news_path=news_path,
            window_minutes=window_minutes,
        )
    except Exception as exc:
        print(f"[whale-tracking] Failed to fetch news timing for event {event_id}: {exc}", flush=True)
        return None


def _score_spike_candidate(
    spike: WhaleSpike,
    *,
    event_id: str,
    signal_sample: PriceSample,
    base_url: str,
    news_path: str,
    news_window_minutes: float,
    request_timeout: float,
    prev_open_interest_by_market: dict[str, OpenInterestSnapshot],
    recent_anomalies: list[WhaleSpike],
) -> WhaleSpike | None:
    market_meta = None
    try:
        market_meta = fetch_primary_market_metadata(event_id, base_url=base_url, timeout=request_timeout)
    except Exception as exc:
        print(f"[whale-tracking] Failed to fetch market metadata for event {event_id}: {exc}", flush=True)

    if market_meta is not None:
        spike.market_id = spike.market_id or market_meta.market_id
        spike.market_liquidity = spike.market_liquidity or market_meta.liquidity
        spike.market_volume = spike.market_volume or market_meta.volume
        spike.spike_id = _make_spike_id(spike)

    orderbooks: list[OrderbookImbalance] = []
    try:
        orderbooks = compute_orderbook_imbalance_for_event(event_id, base_url=base_url, timeout=request_timeout)
    except Exception as exc:
        print(f"[whale-tracking] Failed to fetch order book features for event {event_id}: {exc}", flush=True)

    trade_stats: TradeBurstStats | None = None
    try:
        trade_stats = compute_trade_burst_stats(
            event_id,
            base_url=base_url,
            as_of=spike.to_ts,
            timeout=request_timeout,
        )
    except Exception as exc:
        print(f"[whale-tracking] Failed to fetch trade burst features for event {event_id}: {exc}", flush=True)

    price_stats: list[PriceHistoryStats] = []
    try:
        price_stats = compute_price_history_stats_for_event(
            event_id,
            base_url=base_url,
            timeout=request_timeout,
        )
    except Exception as exc:
        print(f"[whale-tracking] Failed to fetch price history stats for event {event_id}: {exc}", flush=True)

    current_oi_by_market: dict[str, OpenInterestSnapshot] = {}
    try:
        for snapshot in fetch_open_interest_for_event(
            event_id,
            base_url=base_url,
            timeout=request_timeout,
        ):
            current_oi_by_market[snapshot.market_id] = snapshot
    except Exception as exc:
        print(f"[whale-tracking] Failed to fetch open interest for event {event_id}: {exc}", flush=True)

    oi_rel_change = None
    if spike.market_id and spike.market_id in current_oi_by_market:
        prev_oi = prev_open_interest_by_market.get(spike.market_id)
        if prev_oi is not None:
            oi_rel_change = compute_open_interest_change(
                prev_oi,
                current_oi_by_market[spike.market_id],
            ).rel_change
    prev_open_interest_by_market.update(current_oi_by_market)

    news_timing = _safe_fetch_news(
        event_id,
        signal_time=spike.to_ts,
        base_url=base_url,
        news_path=news_path,
        window_minutes=news_window_minutes,
    )
    if news_timing is not None:
        spike.news_time = news_timing.news_time
        spike.news_delta_minutes = news_timing.delta_minutes

    direction = 1.0 if spike.abs_change >= 0 else -1.0
    side_token_id = _select_side_token(signal_sample, spike.side)
    price_stat = _select_price_stats(price_stats, side_token_id)
    orderbook = _select_orderbook(orderbooks, spike.side)

    realized_volatility = price_stat.realized_volatility if price_stat is not None else None
    volatility_adjusted_jump = abs(spike.abs_change) / max(realized_volatility or 0.01, 0.01)

    liquidity_reference = spike.market_liquidity if spike.market_liquidity is not None else 25_000.0
    liquidity_adjusted_move = abs(spike.abs_change) / max(
        math.sqrt(max(liquidity_reference, 1.0) / 25_000.0),
        0.25,
    )
    spread_adjusted_move = abs(spike.abs_change) / max((orderbook.spread if orderbook else None) or 0.01, 0.01)
    directional_orderbook_imbalance = max(0.0, direction * ((orderbook.imbalance if orderbook else 0.0)))
    directional_aggressor_imbalance = max(
        0.0,
        direction * ((trade_stats.aggressor_imbalance if trade_stats else 0.0)),
    )

    recent_anomalies = _prune_recent_anomalies(recent_anomalies, now=spike.to_ts)
    recent_scores = [item.deterministic_score or 0.0 for item in recent_anomalies]
    scored = score_anomaly(
        AnomalyScoreInputs(
            price_move_abs=abs(spike.abs_change),
            price_move_rel=spike.rel_change,
            volatility_adjusted_jump=volatility_adjusted_jump,
            liquidity_adjusted_move=liquidity_adjusted_move,
            spread_adjusted_move=spread_adjusted_move,
            directional_orderbook_imbalance=directional_orderbook_imbalance,
            spread_bps=orderbook.spread_bps if orderbook else None,
            depth_near_touch=orderbook.depth_near_touch if orderbook else None,
            trade_count_burst=trade_stats.trade_count_burst if trade_stats else None,
            volume_burst=trade_stats.volume_burst if trade_stats else None,
            directional_aggressor_imbalance=directional_aggressor_imbalance,
            open_interest_rel_change=oi_rel_change,
            news_delta_minutes=spike.news_delta_minutes,
            recent_anomaly_count=len(recent_anomalies),
            recent_max_score=max(recent_scores) if recent_scores else None,
        )
    )

    snapshot = dict(scored.deterministic_feature_snapshot)
    snapshot["snapshot_storage_contract"] = {
        "version": FEATURE_SNAPSHOT_CONTRACT_VERSION,
        "public_data_only": True,
        "llm_role": "explanation_confidence_refinement",
    }
    snapshot["point_in_time"] = {
        "from_ts": _isoformat(spike.from_ts),
        "to_ts": _isoformat(spike.to_ts),
        "signal_time": _isoformat(spike.signal_time or spike.to_ts),
    }
    snapshot["market_context"] = {
        "market_id": spike.market_id,
        "market_title": signal_sample.market_title,
        "market_liquidity": spike.market_liquidity,
        "market_volume": spike.market_volume,
        "side_token_id": side_token_id,
    }
    snapshot["price_context"] = {
        "yes_price": signal_sample.yes_price,
        "no_price": signal_sample.no_price,
        "volatility_reference": realized_volatility,
        "price_history_z_score": price_stat.z_score if price_stat is not None else None,
    }
    snapshot["orderbook_context"] = {
        "imbalance": orderbook.imbalance if orderbook else None,
        "spread": orderbook.spread if orderbook else None,
        "spread_bps": orderbook.spread_bps if orderbook else None,
        "depth_near_touch": orderbook.depth_near_touch if orderbook else None,
    }
    snapshot["trade_context"] = {
        "trade_count_burst": trade_stats.trade_count_burst if trade_stats else None,
        "volume_burst": trade_stats.volume_burst if trade_stats else None,
        "aggressor_imbalance": trade_stats.aggressor_imbalance if trade_stats else None,
        "recent_trade_count": trade_stats.recent_trade_count if trade_stats else None,
    }
    snapshot["open_interest_context"] = {
        "open_interest_rel_change": oi_rel_change,
    }
    snapshot["news_context"] = {
        "news_time": _isoformat(news_timing.news_time) if news_timing else None,
        "news_delta_minutes": spike.news_delta_minutes,
        "news_source": news_timing.source if news_timing else None,
        "news_title": news_timing.title if news_timing else None,
    }

    spike.deterministic_score = scored.deterministic_score
    spike.deterministic_score_band = scored.deterministic_score_band
    spike.deterministic_feature_snapshot = snapshot
    spike.scorer_version = scored.scorer_version
    spike.trigger_type = scored.trigger_type
    spike.signal_time = spike.to_ts
    spike.llm_should_invoke = scored.should_call_llm
    spike.llm_gate_reason = scored.llm_gate_reason

    return spike if scored.should_emit else None


def monitor_event_for_spikes(
    event_id: str,
    *,
    base_url: str,
    interval_seconds: float = 5.0,
    min_abs_change: float = DEFAULT_MIN_ABS_CHANGE,
    min_rel_change: float = DEFAULT_MIN_REL_CHANGE,
    news_path: str = "news_scraper/data/news_events.jsonl",
    news_window_minutes: float = 240.0,
    sample_iter_factory: Callable[..., Iterable[PriceSample]] | None = None,
    request_timeout: float = 30.0,
) -> Iterator[WhaleSpike]:
    factory = sample_iter_factory or iter_price_samples
    prev_sample: PriceSample | None = None
    prev_open_interest_by_market: dict[str, OpenInterestSnapshot] = {}
    recent_anomalies: list[WhaleSpike] = []

    for sample in factory(
        event_id,
        base_url=base_url,
        interval_seconds=interval_seconds,
        request_timeout=request_timeout,
    ):
        if prev_sample is not None:
            candidates = detect_spike_between(
                prev_sample,
                sample,
                min_abs_change=min_abs_change,
                min_rel_change=min_rel_change,
            )
            recent_anomalies = _prune_recent_anomalies(recent_anomalies, now=sample.captured_at)
            for candidate in candidates:
                scored = _score_spike_candidate(
                    candidate,
                    event_id=event_id,
                    signal_sample=sample,
                    base_url=base_url,
                    news_path=news_path,
                    news_window_minutes=news_window_minutes,
                    request_timeout=request_timeout,
                    prev_open_interest_by_market=prev_open_interest_by_market,
                    recent_anomalies=recent_anomalies,
                )
                if scored is None:
                    continue
                recent_anomalies.append(scored)
                yield scored
        prev_sample = sample


def assess_informed_flow_for_spike(
    spike: WhaleSpike,
    *,
    base_url: str,
    news_path: str = "news_scraper/data/news_events.jsonl",
    min_news_lead_minutes: float = 5.0,
    news_window_minutes: float = 240.0,
) -> InformedFlowSignal | None:
    nearest: NewsTiming | None = None
    if spike.news_time is not None and spike.news_delta_minutes is not None:
        if spike.news_delta_minutes < min_news_lead_minutes:
            return None
        nearest = NewsTiming(
            event_id=spike.event_id,
            signal_time=spike.signal_time or spike.to_ts,
            news_time=spike.news_time,
            delta_minutes=spike.news_delta_minutes,
            source=str((spike.deterministic_feature_snapshot or {}).get("news_context", {}).get("news_source") or ""),
            title=str((spike.deterministic_feature_snapshot or {}).get("news_context", {}).get("news_title") or ""),
        )
    else:
        nearest = _safe_fetch_news(
            spike.event_id,
            signal_time=spike.to_ts,
            base_url=base_url,
            news_path=news_path,
            window_minutes=news_window_minutes,
        )
        if nearest is None or nearest.delta_minutes < min_news_lead_minutes:
            return None

    return InformedFlowSignal(
        event_id=spike.event_id,
        spike=spike,
        lead_minutes=nearest.delta_minutes,
        news_title=nearest.title,
        news_source=nearest.source,
        news_time=nearest.news_time,
    )


def monitor_event_for_informed_flow(
    event_id: str,
    *,
    base_url: str,
    interval_seconds: float = 5.0,
    min_abs_change: float = DEFAULT_MIN_ABS_CHANGE,
    min_rel_change: float = DEFAULT_MIN_REL_CHANGE,
    news_path: str = "news_scraper/data/news_events.jsonl",
    min_news_lead_minutes: float = 5.0,
    news_window_minutes: float = 240.0,
    sample_iter_factory: Callable[..., Iterable[PriceSample]] | None = None,
    request_timeout: float = 30.0,
) -> Iterator[InformedFlowSignal]:
    for spike in monitor_event_for_spikes(
        event_id,
        base_url=base_url,
        interval_seconds=interval_seconds,
        min_abs_change=min_abs_change,
        min_rel_change=min_rel_change,
        news_path=news_path,
        news_window_minutes=news_window_minutes,
        sample_iter_factory=sample_iter_factory,
        request_timeout=request_timeout,
    ):
        signal = assess_informed_flow_for_spike(
            spike,
            base_url=base_url,
            news_path=news_path,
            min_news_lead_minutes=min_news_lead_minutes,
            news_window_minutes=news_window_minutes,
        )
        if signal is not None:
            yield signal


def _spike_trigger_payload(
    spike: WhaleSpike,
    *,
    assessment: InsiderAssessment | None = None,
) -> Dict[str, Any]:
    return {
        "spike_id": spike.spike_id,
        "event_id": spike.event_id,
        "market_id": spike.market_id,
        "side": spike.side,
        "from_ts": _isoformat(spike.from_ts),
        "to_ts": _isoformat(spike.to_ts),
        "deterministic_score": spike.deterministic_score,
        "deterministic_score_band": spike.deterministic_score_band,
        "deterministic_feature_snapshot": spike.deterministic_feature_snapshot,
        "scorer_version": spike.scorer_version,
        "trigger_type": spike.trigger_type,
        "signal_time": _isoformat(spike.signal_time or spike.to_ts),
        "news_time": _isoformat(spike.news_time),
        "news_delta_minutes": spike.news_delta_minutes,
        "llm_probability": (
            assessment.probability_insider if assessment is not None else None
        ),
        "llm_confidence": assessment.confidence if assessment is not None else None,
        "llm_summary": assessment.short_summary if assessment is not None else None,
        "llm_version": assessment.llm_version if assessment is not None else None,
        "prompt_hash": assessment.prompt_hash if assessment is not None else None,
        "prompt_version": assessment.prompt_version if assessment is not None else None,
        "deterministic_prior_probability": (
            assessment.deterministic_prior_probability if assessment is not None else None
        ),
        "llm_probability_adjustment": (
            assessment.probability_adjustment if assessment is not None else None
        ),
        "llm_fallback_reason": (
            assessment.fallback_reason if assessment is not None else None
        ),
        "abs_change": spike.abs_change,
        "rel_change": spike.rel_change,
        "from_price": spike.from_price,
        "to_price": spike.to_price,
        "llm_should_invoke": spike.llm_should_invoke,
        "llm_gate_reason": spike.llm_gate_reason,
    }


def _informed_flow_trigger_payload(
    signal: InformedFlowSignal,
    *,
    assessment: InsiderAssessment | None = None,
) -> Dict[str, Any]:
    payload = _spike_trigger_payload(signal.spike, assessment=assessment)
    payload.update(
        {
            "trigger_type": "informed_flow",
            "lead_minutes": signal.lead_minutes,
            "news_title": signal.news_title,
            "news_source": signal.news_source,
            "news_time": _isoformat(signal.news_time),
        }
    )
    return payload


def monitor_event_and_assess_insider(
    event_id: str,
    *,
    base_url: str,
    interval_seconds: float = 5.0,
    min_abs_change: float = DEFAULT_MIN_ABS_CHANGE,
    min_rel_change: float = DEFAULT_MIN_REL_CHANGE,
    news_path: str = "news_scraper/data/news_events.jsonl",
    min_news_lead_minutes: float = 5.0,
    news_window_minutes: float = 240.0,
    openai_model: str = DEFAULT_ASSESSMENT_MODEL,
    openai_temperature: float = DEFAULT_ASSESSMENT_TEMPERATURE,
    sample_iter_factory: Callable[..., Iterable[PriceSample]] | None = None,
    fresh_data_provider: Callable[[str], Dict[str, Any] | None] | None = None,
    skip_active_check: bool = False,
    request_timeout: float = 30.0,
) -> Iterator[TriggeredInsiderAssessment]:
    if not skip_active_check and not _event_is_currently_active(event_id, base_url=base_url):
        print(f"[whale-tracking] Skipping inactive event {event_id}.", flush=True)
        return

    def _emit_cross_asset_predictions(
        *,
        assessment_id: int | None,
        trigger_payload: dict[str, Any],
        trigger_type: str,
        spike: WhaleSpike,
    ) -> None:
        assessment_row = {
            "id": assessment_id,
            "event_id": event_id,
            "trigger_type": trigger_type,
            "spike_id": spike.spike_id,
            "side": spike.side,
            "signal_time": spike.signal_time,
            "deterministic_score": spike.deterministic_score,
            "deterministic_score_band": spike.deterministic_score_band,
            "trigger_payload": trigger_payload,
        }
        try:
            event_row = dict(get_event(event_id) or {})
            try:
                base = base_url.rstrip("/")
                with httpx.Client(timeout=request_timeout) as client:
                    response = client.get(f"{base}/events/{event_id}")
                    response.raise_for_status()
                    payload = response.json()
                if isinstance(payload, dict):
                    merged = dict(event_row)
                    merged.update(payload)
                    event_row = merged
            except Exception:
                # DB snapshot is still good enough if API enrichment is unavailable.
                pass
            predictions = build_predictions_for_assessment(
                assessment_row,
                event_row=event_row,
            )
            for prediction in predictions:
                insert_cross_asset_prediction(**prediction)
        except Exception as exc:
            print(
                f"[whale-tracking] Failed to persist cross-asset predictions for event {event_id}: {exc}",
                flush=True,
            )

    for spike in monitor_event_for_spikes(
        event_id,
        base_url=base_url,
        interval_seconds=interval_seconds,
        min_abs_change=min_abs_change,
        min_rel_change=min_rel_change,
        news_path=news_path,
        news_window_minutes=news_window_minutes,
        sample_iter_factory=sample_iter_factory,
        request_timeout=request_timeout,
    ):
        insert_whale_spike(spike, market_id=spike.market_id)

        informed_signal = assess_informed_flow_for_spike(
            spike,
            base_url=base_url,
            news_path=news_path,
            min_news_lead_minutes=min_news_lead_minutes,
            news_window_minutes=news_window_minutes,
        )

        trigger_type = "informed_flow" if informed_signal is not None else spike.trigger_type
        assessment: InsiderAssessment | None = None
        trigger_payload = (
            _informed_flow_trigger_payload(informed_signal)
            if informed_signal is not None
            else _spike_trigger_payload(spike)
        )

        if spike.llm_should_invoke:
            if fresh_data_provider is not None:
                fresh_market_data = fresh_data_provider(event_id)
            else:
                fresh_market_data = fetch_fresh_market_data_from_api(
                    event_id,
                    base_url=base_url,
                )
            try:
                assessment = assess_insider_probability_for_event(
                    event_id=event_id,
                    base_url=base_url,
                    model=openai_model,
                    news_path=news_path,
                    temperature=openai_temperature,
                    include_db_event=True,
                    trigger_context={
                        "trigger_type": trigger_type,
                        "signal_time": _isoformat(spike.signal_time or spike.to_ts),
                        "trigger_payload": trigger_payload,
                    },
                    fresh_market_data=fresh_market_data,
                )
            except Exception as api_err:
                if OpenAIRateLimitError is not None and isinstance(api_err, OpenAIRateLimitError):
                    err_body = getattr(api_err, "body", None) or {}
                    if isinstance(err_body, dict) and err_body.get("error", {}).get("type") == "insufficient_quota":
                        print(
                            "[whale-tracking] OpenAI quota exceeded (429). Skipping assessment for this anomaly.",
                            flush=True,
                        )
                    else:
                        print(
                            "[whale-tracking] OpenAI rate limit (429). Skipping assessment for this anomaly.",
                            flush=True,
                        )
                    assessment = None
                else:
                    raise

        trigger_payload = (
            _informed_flow_trigger_payload(informed_signal, assessment=assessment)
            if informed_signal is not None
            else _spike_trigger_payload(spike, assessment=assessment)
        )

        try:
            assessment_id = insert_insider_assessment(
                event_id=event_id,
                trigger_type=trigger_type,
                spike=spike,
                assessment=assessment,
                market_id=spike.market_id,
                trigger_payload=trigger_payload,
            )
            _emit_cross_asset_predictions(
                assessment_id=assessment_id,
                trigger_payload=trigger_payload,
                trigger_type=trigger_type,
                spike=spike,
            )
        except Exception as exc:
            print(f"[whale-tracking] Failed to persist trigger for event {event_id}: {exc}", flush=True)

        yield TriggeredInsiderAssessment(
            event_id=event_id,
            trigger_type=trigger_type,
            spike=spike,
            informed_flow=informed_signal,
            assessment=assessment,
        )


def monitor_events_and_assess_insider(
    event_ids: List[str],
    *,
    base_url: str,
    interval_seconds: float = 5.0,
    min_abs_change: float = DEFAULT_MIN_ABS_CHANGE,
    min_rel_change: float = DEFAULT_MIN_REL_CHANGE,
    news_path: str = "news_scraper/data/news_events.jsonl",
    min_news_lead_minutes: float = 5.0,
    news_window_minutes: float = 240.0,
    openai_model: str = DEFAULT_ASSESSMENT_MODEL,
    openai_temperature: float = DEFAULT_ASSESSMENT_TEMPERATURE,
    sample_iter_factory: Callable[..., Iterable[PriceSample]] | None = None,
    fresh_data_provider: Callable[[str], Dict[str, Any] | None] | None = None,
    skip_active_check: bool = False,
    request_timeout: float = 30.0,
) -> Iterator[TriggeredInsiderAssessment]:
    if not event_ids:
        return

    result_queue: queue.Queue[TriggeredInsiderAssessment] = queue.Queue()

    def _worker(eid: str) -> None:
        try:
            for result in monitor_event_and_assess_insider(
                eid,
                base_url=base_url,
                interval_seconds=interval_seconds,
                min_abs_change=min_abs_change,
                min_rel_change=min_rel_change,
                news_path=news_path,
                min_news_lead_minutes=min_news_lead_minutes,
                news_window_minutes=news_window_minutes,
                openai_model=openai_model,
                openai_temperature=openai_temperature,
                sample_iter_factory=sample_iter_factory,
                fresh_data_provider=fresh_data_provider,
                skip_active_check=skip_active_check,
                request_timeout=request_timeout,
            ):
                result_queue.put(result)
        except Exception as exc:
            exc_type = type(exc).__name__
            print(f"[whale-tracking] Worker for event {eid} exited with error: {exc_type}: {exc}", flush=True)

    threads = [threading.Thread(target=_worker, args=(eid,), daemon=True) for eid in event_ids]
    for thread in threads:
        thread.start()

    while any(thread.is_alive() for thread in threads):
        try:
            yield result_queue.get(timeout=1.0)
        except queue.Empty:
            continue

    while not result_queue.empty():
        yield result_queue.get_nowait()


if __name__ == "__main__":
    # CLI usage:
    # python -m model.insider_detection 2890
    # python -m model.insider_detection 2890 3100 4200 --interval 30
    # python -m model.insider_detection --all-events --interval 30
    import argparse
    import sys

    # Ensure console output appears immediately (no buffering).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    from database.events import get_all_event_ids
    try:  # pragma: no cover - import fallback
        from .event_prices import DEFAULT_BASE_URL  # type: ignore[relative-beyond-top-level]
    except ImportError:  # pragma: no cover
        from model.event_prices import DEFAULT_BASE_URL

    parser = argparse.ArgumentParser(
        description="Monitor one or more Polymarket events for deterministic public-data anomalies."
    )
    parser.add_argument(
        "event_ids",
        nargs="*",
        help="One or more Polymarket event IDs, e.g. 2890 3100 4200. Omit when using --all-events.",
    )
    parser.add_argument(
        "--all-events",
        action="store_true",
        default=False,
        help="Load all event IDs from the database and monitor them all.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("POLYMARKET_API_BASE", DEFAULT_BASE_URL),
        help="Base URL of the local Polymarket proxy server.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Polling interval in seconds per event (default: 5.0).",
    )
    parser.add_argument(
        "--min-abs",
        type=float,
        default=None,
        help=(
            "Absolute move floor used to ignore tiny price noise "
            f"(default: {DEFAULT_MIN_ABS_CHANGE:.2f}; "
            f"{ALL_EVENTS_MIN_ABS_CHANGE:.2f} with --all-events)."
        ),
    )
    parser.add_argument( 
        "--min-rel",
        type=float,
        default=None,
        help=(
            "Relative move floor used to ignore tiny price noise "
            f"(default: {DEFAULT_MIN_REL_CHANGE:.2f}; "
            f"{ALL_EVENTS_MIN_REL_CHANGE:.2f} with --all-events)."
        ),
    )
    parser.add_argument(
        "--news-path",
        default=os.getenv("NEWS_EVENTS_PATH", "news_scraper/data/news_events.jsonl"),
        help="Path to JSONL news dataset used for pre-news informed-flow checks.",
    )
    parser.add_argument(
        "--min-news-lead",
        type=float,
        default=5.0,
        help="Flag informed flow only if spike leads news by at least N minutes.",
    )
    parser.add_argument(
        "--news-window",
        type=float,
        default=240.0,
        help="News matching window in minutes around each spike timestamp.",
    )
    parser.add_argument(
        "--openai-model",
        "--ollama-model",
        dest="openai_model",
        default=os.getenv("OLLAMA_MODEL", DEFAULT_ASSESSMENT_MODEL),
        help="Ollama Cloud model name used for insider assessment.",
    )
    parser.add_argument(
        "--openai-temperature",
        "--ollama-temperature",
        dest="openai_temperature",
        type=float,
        default=DEFAULT_ASSESSMENT_TEMPERATURE,
        help="Sampling temperature for insider assessment.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=None,
        help="HTTP timeout in seconds for price requests (default 30; 60 with --all-events).",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=50,
        help="When using --all-events, monitor at most this many events (default 50).",
    )

    args = parser.parse_args()

    if args.all_events:
        event_ids = get_all_event_ids()
        if not event_ids:
            print("[whale-tracking] No events found in the database. Run 'python -m model.event_cache' first.", flush=True)
            raise SystemExit(1)
        event_ids = event_ids[: max(1, args.max_events)]
    elif args.event_ids:
        event_ids = args.event_ids
    else:
        parser.error("Provide at least one event ID or pass --all-events.")

    if args.min_abs is None:
        args.min_abs = (
            ALL_EVENTS_MIN_ABS_CHANGE if args.all_events else DEFAULT_MIN_ABS_CHANGE
        )
    if args.min_rel is None:
        args.min_rel = (
            ALL_EVENTS_MIN_REL_CHANGE if args.all_events else DEFAULT_MIN_REL_CHANGE
        )
    if args.request_timeout is None:
        args.request_timeout = 60.0 if args.all_events else 30.0

    # With --all-events, IDs already come from the DB (active=TRUE from event_cache).
    # Skip per-event API validation to avoid 8000+ requests and ~30 min startup.
    if not args.all_events:
        event_ids = [
            event_id
            for event_id in event_ids
            if _event_is_currently_active(event_id, base_url=args.base_url)
        ]
    if not event_ids:
        print("[whale-tracking] No active events available to monitor.", flush=True)
        raise SystemExit(1)

    print(
        f"Monitoring {len(event_ids)} event(s). Deterministic anomaly scores and gated explanation-layer outputs will appear below.",
        flush=True,
    )

    for result in monitor_events_and_assess_insider(
        event_ids,
        base_url=args.base_url,
        interval_seconds=args.interval,
        min_abs_change=args.min_abs,
        min_rel_change=args.min_rel,
        news_path=args.news_path,
        min_news_lead_minutes=args.min_news_lead,
        news_window_minutes=args.news_window,
        openai_model=args.openai_model,
        openai_temperature=args.openai_temperature,
        skip_active_check=args.all_events,
        request_timeout=args.request_timeout,
    ):
        a = result.assessment
        score_value = (
            f"{result.spike.deterministic_score:.2f}"
            if result.spike.deterministic_score is not None
            else "n/a"
        )
        print(
            f"[deterministic] event_id={result.event_id} "
            f"trigger={result.trigger_type} "
            f"score={score_value} "
            f"band={result.spike.deterministic_score_band or 'n/a'}",
            flush=True,
        )
        if a is not None:
            print(
                f"[llm] event_id={result.event_id} "
                f"research_signal_probability={a.probability_insider:.3f} "
                f"confidence={a.confidence} "
                f"summary={a.short_summary}",
                flush=True,
            )
        else:
            print(
                f"[llm-skipped] event_id={result.event_id} gate_reason={result.spike.llm_gate_reason or 'n/a'}",
                flush=True,
            )
        # print(
        #     "[whale-spike]",
        #     spike.event_id,
        #     spike.side,
        #     direction,
        #     f"{spike.from_price:.3f} -> {spike.to_price:.3f}",
        #     f"(Δ={spike.abs_change:+.3f}, rel={spike.rel_change*100:.1f}%)",
        #     f"window={int((spike.to_ts - spike.from_ts).total_seconds())}s",
        # )
        # if result.informed_flow is not None:
        #     print(
        #         "[informed-flow?]",
        #         result.informed_flow.event_id,
        #         f"lead={result.informed_flow.lead_minutes:.1f}m",
        #         f"source={result.informed_flow.news_source}",
        #         f"title={result.informed_flow.news_title}",
        #     )
        # print(
        #     "[insider-assessment]",
        #     result.event_id,
        #     f"trigger={result.trigger_type}",
        #     f"prob={result.assessment.probability_insider:.3f}",
        #     f"confidence={result.assessment.confidence}",
        #     f"summary={result.assessment.short_summary}",
        # )