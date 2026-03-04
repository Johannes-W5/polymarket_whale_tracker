from __future__ import annotations

"""

Whale-tracking and informed-flow detection utilities for Polymarket events.


Core idea:


- Poll yes/no prices for a given event over time.
- Detect large, unexpected jumps ("spikes") that may indicate informed flow.

This module does NOT make any trading decisions. It only:
1) Normalises price samples for an event.
2) Detects jumps between subsequent samples based on configurable thresholds.
3) Provides a small CLI-style helper for quick experimentation.


Start detector: cd /home/johannes/polymarket_sentiment
source .venv/bin/activate   # oder: . .venv/bin/activate
python -m model.insider_detection 2890 (or other event id)
Try to fetch not only one event but all events in the database. Create database query to get all events.
"""

from database.events import insert_whale_spike

import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from time import sleep
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

# Support both "python -m model.insider_detection" (package context)
# and direct execution "python model/insider_detection.py".
try:  # pragma: no cover - import fallback
    from .event_prices import EventPrices, get_event_prices  # type: ignore[relative-beyond-top-level]
except ImportError:  # pragma: no cover
    from model.event_prices import EventPrices, get_event_prices

try:  # pragma: no cover - import fallback
    from .market_signals import NewsTiming, find_nearest_news_for_event  # type: ignore[relative-beyond-top-level]
except ImportError:  # pragma: no cover
    from model.market_signals import NewsTiming, find_nearest_news_for_event

try:  # pragma: no cover - import fallback
    from .insider_model import InsiderAssessment, assess_insider_probability_for_event  # type: ignore[relative-beyond-top-level]
except ImportError:  # pragma: no cover
    from model.insider_model import InsiderAssessment, assess_insider_probability_for_event

try:  # pragma: no cover - import fallback
    from .fresh_data import fetch_fresh_market_data_from_api  # type: ignore[relative-beyond-top-level]
except ImportError:  # pragma: no cover
    from model.fresh_data import fetch_fresh_market_data_from_api


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


@dataclass
class InformedFlowSignal:
    """
    Spike that likely happened before relevant news was publicly visible.

    This is a heuristic signal, not proof of insider trading.
    """

    event_id: str
    spike: WhaleSpike
    lead_minutes: float
    news_title: str
    news_source: str
    news_time: datetime


@dataclass
class TriggeredInsiderAssessment:
    """
    Result of running insider_model for a detection trigger.
    """

    event_id: str
    trigger_type: str  # "whale_spike" | "informed_flow"
    spike: WhaleSpike
    informed_flow: InformedFlowSignal | None
    assessment: InsiderAssessment


def detect_spike_between(
    prev_sample: PriceSample,
    curr_sample: PriceSample,
    *,
    min_abs_change: float = 0.01,
    min_rel_change: float = 0.01, #change to 0.3 for more sensitive detection again
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
    interval_seconds: float = 5.0,
    side: str = "BUY",
) -> Iterator[PriceSample]:
    """
    Infinite iterator of price samples for an event, polling the server API.

    Intended for real-time monitoring; wrap and break out in your own code
    when you want to stop the loop.
    """
    while True:
        try:
            prices = get_event_prices(event_id, base_url=base_url, side=side)
            yield PriceSample.from_event_prices(event_id, prices)
        except Exception as exc:
            print(f"[whale-tracking] Skipping sample for event {event_id}: {exc}")
        sleep(interval_seconds)


def monitor_event_for_spikes(
    event_id: str,
    *,
    base_url: str,
    interval_seconds: float = 5.0,
    min_abs_change: float = 0.01, 
    min_rel_change: float = 0.01, #change to 0.3 again for more sensitive detection
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


def assess_informed_flow_for_spike(
    spike: WhaleSpike,
    *,
    base_url: str,
    news_path: str = "data/news_events.jsonl",
    min_news_lead_minutes: float = 5.0,
    news_window_minutes: float = 240.0,
) -> InformedFlowSignal | None:
    """
    Classify a spike as "possible informed flow" if it clearly leads nearby news.

    Logic:
    - Find nearest related news around the spike timestamp.
    - Require that news timestamp is AFTER the spike by at least
      `min_news_lead_minutes`.
    """
    nearest: NewsTiming | None = find_nearest_news_for_event(
        spike.event_id,
        signal_time=spike.to_ts,
        base_url=base_url,
        news_path=news_path,
        window_minutes=news_window_minutes,
    )
    if nearest is None:
        return None

    # Positive delta means the news was ingested after the spike.
    if nearest.delta_minutes < min_news_lead_minutes:
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
    min_abs_change: float = 0.01,
    min_rel_change: float = 0.01, #change to 0.3 again for more sensitive detection
    news_path: str = "data/news_events.jsonl",
    min_news_lead_minutes: float = 5.0,
    news_window_minutes: float = 240.0,
    sample_iter_factory: Callable[..., Iterable[PriceSample]] | None = None,
) -> Iterator[InformedFlowSignal]:
    """
    Continuously monitor and emit spikes that appear to lead relevant news.
    """
    for spike in monitor_event_for_spikes(
        event_id,
        base_url=base_url,
        interval_seconds=interval_seconds,
        min_abs_change=min_abs_change,
        min_rel_change=min_rel_change,
        sample_iter_factory=sample_iter_factory,
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


def _isoformat(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _spike_trigger_payload(spike: WhaleSpike) -> Dict[str, Any]:
    return {
        "event_id": spike.event_id,
        "from_ts": _isoformat(spike.from_ts),
        "to_ts": _isoformat(spike.to_ts),
        "side": spike.side,
        "from_price": spike.from_price,
        "to_price": spike.to_price,
        "abs_change": spike.abs_change,
        "rel_change": spike.rel_change,
    }


def _informed_flow_trigger_payload(signal: InformedFlowSignal) -> Dict[str, Any]:
    payload = _spike_trigger_payload(signal.spike)
    payload.update(
        {
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
    min_abs_change: float = 0.01,
    min_rel_change: float = 0.01, #change to 0.3 again for more sensitive detection
    news_path: str = "data/news_events.jsonl",
    min_news_lead_minutes: float = 5.0,
    news_window_minutes: float = 240.0,
    openai_model: str = "gpt-4.1-mini",
    openai_temperature: float = 0.1,
    sample_iter_factory: Callable[..., Iterable[PriceSample]] | None = None,
    fresh_data_provider: Callable[[str], Dict[str, Any] | None] | None = None,
) -> Iterator[TriggeredInsiderAssessment]:
    """
    Run insider_model only when a spike or informed-flow signal is detected.
    """
    for spike in monitor_event_for_spikes(
        event_id,
        base_url=base_url,
        interval_seconds=interval_seconds,
        min_abs_change=min_abs_change,
        min_rel_change=min_rel_change,
        sample_iter_factory=sample_iter_factory,
    ):
        # Persist each detected spike for downstream querying/auditing.
        insert_whale_spike(spike)

        informed_signal = assess_informed_flow_for_spike(
            spike,
            base_url=base_url,
            news_path=news_path,
            min_news_lead_minutes=min_news_lead_minutes,
            news_window_minutes=news_window_minutes,
        )
        if informed_signal is not None:
            trigger_type = "informed_flow"
            trigger_payload = _informed_flow_trigger_payload(informed_signal)
        else:
            trigger_type = "whale_spike"
            trigger_payload = _spike_trigger_payload(spike)

        if fresh_data_provider is not None:
            fresh_market_data = fresh_data_provider(event_id)
        else:
            fresh_market_data = fetch_fresh_market_data_from_api(
                event_id,
                base_url=base_url,
            )

        assessment = assess_insider_probability_for_event(
            event_id=event_id,
            base_url=base_url,
            model=openai_model,
            news_path=news_path,
            temperature=openai_temperature,
            include_db_event=True,
            trigger_context={
                "trigger_type": trigger_type,
                "trigger_payload": trigger_payload,
            },
            fresh_market_data=fresh_market_data,
        )
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
    min_abs_change: float = 0.01,
    min_rel_change: float = 0.01, #change to 0.3 again for more sensitive detection
    news_path: str = "data/news_events.jsonl",
    min_news_lead_minutes: float = 5.0,
    news_window_minutes: float = 240.0,
    openai_model: str = "gpt-4.1-mini",
    openai_temperature: float = 0.1,
    sample_iter_factory: Callable[..., Iterable[PriceSample]] | None = None,
    fresh_data_provider: Callable[[str], Dict[str, Any] | None] | None = None,
) -> Iterator[TriggeredInsiderAssessment]:
    """
    Concurrently monitor multiple events and yield insider assessments.

    Spawns one daemon thread per event_id, each running
    `monitor_event_and_assess_insider`. Results from all threads are
    collected in a shared queue and yielded in arrival order.

    The iterator runs until all threads exit (which only happens if the
    underlying generators are finite, e.g. in tests). In production the
    threads are infinite loops, so this iterator also runs indefinitely
    until the process is killed.
    """
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
            ):
                result_queue.put(result)
        except Exception as exc:
            print(f"[whale-tracking] Worker for event {eid} exited with error: {exc}")

    threads = [
        threading.Thread(target=_worker, args=(eid,), daemon=True)
        for eid in event_ids
    ]
    for t in threads:
        t.start()

    while any(t.is_alive() for t in threads):
        try:
            yield result_queue.get(timeout=1.0)
        except queue.Empty:
            continue

    # Drain any results that arrived after the last is_alive() check.
    while not result_queue.empty():
        yield result_queue.get_nowait()


if __name__ == "__main__":
    # CLI usage:
    # python -m model.insider_detection 2890
    # python -m model.insider_detection 2890 3100 4200 --interval 30
    # python -m model.insider_detection --all-events --interval 30
    import argparse
    import os

    from database.events import get_all_event_ids
    from .event_prices import DEFAULT_BASE_URL

    parser = argparse.ArgumentParser(
        description="Monitor one or more Polymarket events for large price jumps (whale spikes)."
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
        default=0.01,
        help="Minimum absolute price change to flag a spike (default: 0.1).",
    )
    parser.add_argument( 
        "--min-rel",
        type=float,
        default=0.01, #change to 0.3 again for more sensitive detection
        help="Minimum relative price change (fraction, default: 0.3 == 30%%).",
    )
    parser.add_argument(
        "--news-path",
        default="data/news_events.jsonl",
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
        default="gpt-4.1-mini",
        help="OpenAI model name used for insider assessment.",
    )
    parser.add_argument(
        "--openai-temperature",
        type=float,
        default=0.1,
        help="OpenAI sampling temperature for insider assessment.",
    )

    args = parser.parse_args()

    if args.all_events:
        event_ids = get_all_event_ids()
        if not event_ids:
            print("[whale-tracking] No events found in the database. Run 'python -m model.event_cache' first.")
            raise SystemExit(1)
    elif args.event_ids:
        event_ids = args.event_ids
    else:
        parser.error("Provide at least one event ID or pass --all-events.")

    print(
        f"[whale-tracking] Monitoring {len(event_ids)} event(s): "
        f"{', '.join(event_ids[:5])}{'...' if len(event_ids) > 5 else ''} "
        f"via {args.base_url} every {args.interval:.0f}s "
        f"(min_abs={args.min_abs}, min_rel={args.min_rel})"
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
    ):
        spike = result.spike
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
        if result.informed_flow is not None:
            print(
                "[informed-flow?]",
                result.informed_flow.event_id,
                f"lead={result.informed_flow.lead_minutes:.1f}m",
                f"source={result.informed_flow.news_source}",
                f"title={result.informed_flow.news_title}",
            )
        print(
            "[insider-assessment]",
            result.event_id,
            f"trigger={result.trigger_type}",
            f"prob={result.assessment.probability_insider:.3f}",
            f"confidence={result.assessment.confidence}",
            f"summary={result.assessment.short_summary}",
        )

