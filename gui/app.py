from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gui.data import get_default_event_id, get_recent_spike_feed, load_dashboard_data
from model.event_prices import DEFAULT_BASE_URL


def _format_price(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "N/A"


def _format_percent(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "N/A"


@st.cache_data(ttl=5, show_spinner=False)
def _cached_recent_spike_feed(limit: int) -> list[dict[str, Any]]:
    return get_recent_spike_feed(limit=limit)


@st.cache_data(ttl=5, show_spinner=False)
def _cached_dashboard_data(event_id: str, base_url: str, news_path: str) -> dict[str, Any]:
    return load_dashboard_data(event_id, base_url=base_url, news_path=news_path)


def _render_spike_metrics(spike: dict[str, Any]) -> None:
    metric_a, metric_b, metric_c, metric_d = st.columns(4)
    metric_a.metric("Side", str(spike.get("side") or "N/A"))
    metric_b.metric("From Price", _format_price(spike.get("from_price")))
    metric_c.metric("To Price", _format_price(spike.get("to_price")))
    metric_d.metric("Relative Move", _format_percent(spike.get("rel_change")))


def _render_top_card(dashboard: dict[str, Any]) -> None:
    event = dashboard["event"]
    market = dashboard["market"]
    prices = dashboard["prices"]
    assessment = dashboard["assessment"]
    latest_spike = dashboard["latest_spike"]

    label = (
        f"Latest Spike: {event.get('title') or event.get('name') or dashboard['event_id']}  "
        f"({dashboard['event_id']})"
    )
    with st.expander(label, expanded=True):
        top_left, top_middle, top_right = st.columns(3)
        top_left.metric("Event ID", dashboard["event_id"])
        top_middle.metric("YES Price", _format_price(prices.get("yes_price")))
        top_right.metric("NO Price", _format_price(prices.get("no_price")))

        if dashboard.get("event_error"):
            st.warning(f"Event metadata unavailable: {dashboard['event_error']}")

        if dashboard.get("prices_error"):
            st.warning(f"Current prices unavailable: {dashboard['prices_error']}")

        assessment_col, market_col = st.columns(2)

        with assessment_col:
            st.markdown("**Ollama Assessment**")
            if assessment:
                prob_col, conf_col = st.columns(2)
                prob_col.metric(
                    "Insider Probability",
                    _format_percent(assessment.get("probability_insider")),
                )
                conf_col.metric(
                    "Confidence",
                    str(assessment.get("confidence") or "N/A").title(),
                )
                st.write(assessment.get("short_summary") or "No summary returned.")
            else:
                st.warning(dashboard["assessment_error"] or "Assessment unavailable.")

        with market_col:
            st.markdown("**Market Summary**")
            st.write(f"**Event:** {event.get('title') or event.get('name') or dashboard['event_id']}")
            st.write(f"**Market:** {market.get('title') or market.get('question') or 'N/A'}")
            st.write(f"**Category:** {event.get('category') or 'N/A'}")
            st.write(f"**Volume:** {market.get('volume') or 'N/A'}")
            st.write(f"**Liquidity:** {market.get('liquidity') or 'N/A'}")

        st.markdown("**Latest Spike Details**")
        if latest_spike:
            _render_spike_metrics(latest_spike)
            st.caption(f"Captured at {latest_spike.get('to_ts') or 'unknown time'}")
        else:
            if dashboard["spikes_error"]:
                st.warning(f"Spike history unavailable: {dashboard['spikes_error']}")
            else:
                st.info("No recent whale spikes found for this event.")

        if dashboard["recent_spikes"]:
            with st.expander("Event Spike History", expanded=False):
                history_rows = [
                    {
                        "time": spike.get("to_ts") or "N/A",
                        "side": spike.get("side") or "N/A",
                        "from_price": _format_price(spike.get("from_price")),
                        "to_price": _format_price(spike.get("to_price")),
                        "abs_change": _format_price(spike.get("abs_change")),
                        "rel_change": _format_percent(spike.get("rel_change")),
                    }
                    for spike in dashboard["recent_spikes"]
                ]
                st.dataframe(history_rows, use_container_width=True, hide_index=True)


def _render_feed_card(item: dict[str, Any]) -> None:
    label = (
        f"{item.get('event_name') or item.get('event_id') or 'Unknown Event'}  "
        f"| {item.get('side') or 'N/A'} | {_format_percent(item.get('rel_change'))}  "
        f"| {item.get('to_ts') or 'N/A'}"
    )
    with st.expander(label, expanded=False):
        meta_left, meta_right = st.columns([2, 3])
        with meta_left:
            st.write(f"**Event ID:** {item.get('event_id') or 'N/A'}")
            st.write(f"**Side:** {item.get('side') or 'N/A'}")
            st.write(f"**Time:** {item.get('to_ts') or 'N/A'}")
        with meta_right:
            st.write(item.get("short_summary") or "No persisted assessment summary yet.")

        _render_spike_metrics(item)

        extra_a, extra_b = st.columns(2)
        extra_a.metric("Insider Probability", _format_percent(item.get("probability_insider")))
        extra_b.metric("Confidence", str(item.get("confidence") or "N/A").title())


st.set_page_config(page_title="Polymarket Whale Tracker", page_icon="PM", layout="wide")
st.title("Polymarket Whale Tracker")
st.caption("Live whale-spike feed with the newest event pinned at the top.")

default_base_url = os.getenv("POLYMARKET_API_BASE", DEFAULT_BASE_URL)
default_news_path = os.getenv("NEWS_EVENTS_PATH", "data/news_events.jsonl")

with st.sidebar:
    st.header("Controls")
    base_url = st.text_input("API base URL", value=default_base_url)
    news_path = st.text_input("News dataset path", value=default_news_path)
    feed_limit = st.slider("Recent spikes to show", min_value=5, max_value=50, value=20)
    refresh_seconds = st.slider("Auto-refresh seconds", min_value=2, max_value=30, value=5)
    refresh = st.button("Refresh now", type="primary")

    if refresh:
        st.cache_data.clear()

    st.caption("Newest spike is pinned automatically. Older spikes update below in real time.")


@st.fragment(run_every=refresh_seconds)
def _render_live_dashboard() -> None:
    with st.spinner("Loading live spike feed..."):
        recent_feed = _cached_recent_spike_feed(feed_limit)

    if not recent_feed:
        st.warning("No recent whale spikes are available yet.")
        return

    latest_event_id = str(recent_feed[0].get("event_id") or "").strip() or get_default_event_id()
    if not latest_event_id:
        st.warning("No recent whale spikes are available yet.")
        return

    try:
        dashboard = _cached_dashboard_data(latest_event_id, base_url, news_path)
    except Exception as exc:
        st.error(f"Failed to load latest spike details: {exc}")
        return

    st.subheader("Live Latest Spike")
    _render_top_card(dashboard)

    st.subheader("Older Whale Spikes")
    if len(recent_feed) == 1:
        st.write("No older whale spikes available yet.")
        return

    for item in recent_feed[1:]:
        _render_feed_card(item)


_render_live_dashboard()
