"""
Model package: fetch event/market data and prices from the Polymarket server API,
and provide higher-level utilities such as whale-tracking on price series.
"""

from __future__ import annotations

import importlib
from typing import Any

from model.anomaly_scoring import (
    AnomalyScoreInputs,
    DeterministicAnomalyScore,
    FEATURE_SNAPSHOT_CONTRACT_VERSION,
    score_anomaly,
)
from model.event_prices import (
    EventPrices,
    get_event_no_price,
    get_event_prices,
    get_event_yes_price,
)
from model.fresh_data import (
    InMemoryFreshDataStore,
    fetch_fresh_market_data_from_api,
)
from model.insider_model import (
    EXPLANATION_PAYLOAD_VERSION,
    MAX_PROBABILITY_ADJUSTMENT,
    PROMPT_VERSION,
    assess_insider_probability_from_payload,
)

# Avoid eager imports of event_cache / insider_detection so
# `python -m model.event_cache` and `python -m model.insider_detection`
# do not trigger runpy RuntimeWarning (module already in sys.modules).

_INSIDER_DETECTION_EXPORTS = frozenset(
    {
        "PriceSample",
        "WhaleSpike",
        "InformedFlowSignal",
        "TriggeredInsiderAssessment",
        "detect_spike_between",
        "iter_price_samples",
        "monitor_event_for_spikes",
        "assess_informed_flow_for_spike",
        "monitor_event_for_informed_flow",
        "monitor_event_and_assess_insider",
        "monitor_events_and_assess_insider",
    }
)


def __getattr__(name: str) -> Any:
    if name == "sync_events_to_db":
        mod = importlib.import_module("model.event_cache")
        return mod.sync_events_to_db
    if name in _INSIDER_DETECTION_EXPORTS:
        mod = importlib.import_module("model.insider_detection")
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Prices
    "EventPrices",
    "get_event_prices",
    "get_event_yes_price",
    "get_event_no_price",
    "fetch_fresh_market_data_from_api",
    "InMemoryFreshDataStore",
    "sync_events_to_db",
    "AnomalyScoreInputs",
    "DeterministicAnomalyScore",
    "FEATURE_SNAPSHOT_CONTRACT_VERSION",
    "score_anomaly",
    "EXPLANATION_PAYLOAD_VERSION",
    "MAX_PROBABILITY_ADJUSTMENT",
    "PROMPT_VERSION",
    "assess_insider_probability_from_payload",
    # Whale tracking
    "PriceSample",
    "WhaleSpike",
    "InformedFlowSignal",
    "TriggeredInsiderAssessment",
    "detect_spike_between",
    "iter_price_samples",
    "monitor_event_for_spikes",
    "assess_informed_flow_for_spike",
    "monitor_event_for_informed_flow",
    "monitor_event_and_assess_insider",
    "monitor_events_and_assess_insider",
]
