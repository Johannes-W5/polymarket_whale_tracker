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
)

__all__ = [
    # Prices
    "EventPrices",
    "get_event_prices",
    "get_event_yes_price",
    "get_event_no_price",
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
]
