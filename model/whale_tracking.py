from __future__ import annotations

"""
Whale-tracking utilities for Polymarket events.

Core idea:
- Poll yes/no prices for a given event over time.
- Detect large, unexpected jumps ("spikes") that may indicate informed flow.

This module does NOT make any trading decisions. It only:
1) Normalises price samples for an event.
2) Detects jumps between subsequent samples based on configurable thresholds.
3) Provides a small CLI-style helper for quick experimentation.


Start whale tracker: cd /home/johannes/polymarket_sentiment
source .venv/bin/activate   # oder: . .venv/bin/activate
python -m model.whale_tracking 2890 (or other event id)
Try to fetch not only one event but all events in the database. Create database query to get all events.
"""

from database.events import get_events
from database.whale_spikes import insert_whale_spike

from dataclasses import dataclass
from datetime import datetime, timezone
from time import sleep
from typing import Callable, Iterable, Iterator, Optional

# Support both "python -m model.whale_tracking" (package context)
# and direct execution "python model/whale_tracking.py".
try:  # pragma: no cover - import fallback
    from .event_prices import EventPrices, get_event_prices  # type: ignore[relative-beyond-top-level]
except ImportError:  # pragma: no cover
    from model.event_prices import EventPrices, get_event_prices


@dataclass
class PriceSample:
    """Snapshot of yes/no prices for an event at a specific time."""

    event_id: str
    captured_at: datetime
    yes_price: float | None
    no_price: float | None

    @classmethod
    def from_event_prices(cls, event_id: str, prices: EventPrices) -> "PriceSample":
        return cls(
            event_id=event_id,
            captured_at=datetime.now(timezone.utc),
            yes_price=prices.yes_price,
            no_price=prices.no_price,
        )


@dataclass
class WhaleSpike:
    """Detected price spike between two consecutive samples."""

    event_id: str
    from_ts: datetime
    to_ts: datetime
    side: str  # "YES" or "NO"
    from_price: float
    to_price: float
    abs_change: float
    rel_change: float  # relative to from_price, e.g. 0.25 == +25%


def detect_spike_between(
    prev_sample: PriceSample,
    curr_sample: PriceSample,
    *,
    min_abs_change: float = 0.1,
    min_rel_change: float = 0.3,
) -> list[WhaleSpike]:
    """
    Detect price spikes between two samples for an event.

    A spike is flagged when BOTH conditions hold for a side (yes/no):
    - Absolute change >= min_abs_change
    - Relative change >= min_rel_change (e.g. 0.3 == 30%)
    """
    spikes: list[WhaleSpike] = []

    def _check_side(side: str, prev: Optional[float], curr: Optional[float]) -> None:
        if prev is None or curr is None:
            return
        if prev <= 0:
            return
        abs_change = curr - prev
        rel_change = abs(abs_change) / prev
        if abs(abs_change) >= min_abs_change and rel_change >= min_rel_change:
            spikes.append(
                WhaleSpike(
                    event_id=curr_sample.event_id,
                    from_ts=prev_sample.captured_at,
                    to_ts=curr_sample.captured_at,
                    side=side,
                    from_price=prev,
                    to_price=curr,
                    abs_change=abs_change,
                    rel_change=rel_change,
                )
            )

    _check_side("YES", prev_sample.yes_price, curr_sample.yes_price)
    _check_side("NO", prev_sample.no_price, curr_sample.no_price)
    return spikes


def iter_price_samples(
    event_id: str,
    *,
    base_url: str,
    interval_seconds: float = 60.0,
    side: str = "BUY",
) -> Iterator[PriceSample]:
    """
    Infinite iterator of price samples for an event, polling the server API.

    Intended for real-time monitoring; wrap and break out in your own code
    when you want to stop the loop.
    """
    while True:
        prices = get_event_prices(event_id, base_url=base_url, side=side)
        yield PriceSample.from_event_prices(event_id, prices)
        sleep(interval_seconds)


def monitor_event_for_spikes(
    event_id: str,
    *,
    base_url: str,
    interval_seconds: float = 60.0,
    min_abs_change: float = 0.1,
    min_rel_change: float = 0.3,
    sample_iter_factory: Callable[..., Iterable[PriceSample]] | None = None,
) -> Iterator[WhaleSpike]:
    """
    High-level helper: continuously monitor an event and yield detected spikes.

    - Uses `iter_price_samples` by default to poll the server.
    - Exposed as a generator so you can:
        * Log spikes
        * Push them to a message queue
        * Trigger downstream trading logic

    The `sample_iter_factory` hook makes this function testable by injecting
    a finite sequence of samples.
    """
    factory = sample_iter_factory or iter_price_samples
    prev_sample: PriceSample | None = None

    for sample in factory(
        event_id,
        base_url=base_url,
        interval_seconds=interval_seconds,
    ):
        if prev_sample is not None:
            spikes = detect_spike_between(
                prev_sample,
                sample,
                min_abs_change=min_abs_change,
                min_rel_change=min_rel_change,
            )
            for spike in spikes:
                yield spike
        prev_sample = sample


if __name__ == "__main__":
    # Minimal CLI for manual experimentation:
    # python -m model.whale_tracking EVENT_ID
    import argparse
    import os

    from .event_prices import DEFAULT_BASE_URL

    parser = argparse.ArgumentParser(
        description="Monitor a Polymarket event for large price jumps (whale spikes)."
    )
    parser.add_argument("event_id", help="Polymarket event ID, e.g. 2890")
    parser.add_argument(
        "--base-url",
        default=os.getenv("POLYMARKET_API_BASE", DEFAULT_BASE_URL),
        help="Base URL of the local Polymarket proxy server.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Polling interval in seconds (default: 60).",
    )
    parser.add_argument(
        "--min-abs",
        type=float,
        default=0.1,
        help="Minimum absolute price change to flag a spike (default: 0.1).",
    )
    parser.add_argument(
        "--min-rel",
        type=float,
        default=0.3,
        help="Minimum relative price change (fraction, default: 0.3 == 30%%).",
    )

    args = parser.parse_args()

    print(
        f"[whale-tracking] Monitoring event {args.event_id} "
        f"via {args.base_url} every {args.interval:.0f}s "
        f"(min_abs={args.min_abs}, min_rel={args.min_rel})"
    )

    for spike in monitor_event_for_spikes(
        args.event_id,
        base_url=args.base_url,
        interval_seconds=args.interval,
        min_abs_change=args.min_abs,
        min_rel_change=args.min_rel,
    ):
        direction = "UP" if spike.abs_change > 0 else "DOWN"
        print(
            "[whale-spike]",
            spike.event_id,
            spike.side,
            direction,
            f"{spike.from_price:.3f} -> {spike.to_price:.3f}",
            f"(Δ={spike.abs_change:+.3f}, rel={spike.rel_change*100:.1f}%)",
            f"window={int((spike.to_ts - spike.from_ts).total_seconds())}s",
        )

