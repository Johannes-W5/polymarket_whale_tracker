"""
Model package: fetch event/market data and prices from the Polymarket server API,
and provide higher-level utilities such as whale-tracking on price series.
"""

from model.event_prices import (
    EventPrices,
    get_event_prices,
    get_event_no_price,
    get_event_yes_price,
)
from model.fresh_data import (
    InMemoryFreshDataStore,
    fetch_fresh_market_data_from_api,
)
from model.anomaly_scoring import (
    AnomalyScoreInputs,
    DeterministicAnomalyScore,
    FEATURE_SNAPSHOT_CONTRACT_VERSION,
    score_anomaly,
)
from model.event_cache import sync_events_to_db
from model.insider_detection import (
    PriceSample,
    WhaleSpike,
    InformedFlowSignal,
    TriggeredInsiderAssessment,
    detect_spike_between,
    iter_price_samples,
    monitor_event_for_spikes,
    assess_informed_flow_for_spike,
    monitor_event_for_informed_flow,
    monitor_event_and_assess_insider,
    monitor_events_and_assess_insider,
)
from model.insider_model import (
    EXPLANATION_PAYLOAD_VERSION,
    MAX_PROBABILITY_ADJUSTMENT,
    PROMPT_VERSION,
    assess_insider_probability_from_payload,
)

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
