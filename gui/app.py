from __future__ import annotations

import os
from typing import Any

import streamlit as st

from gui.data import list_event_options, load_dashboard_data
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


@st.cache_data(ttl=60, show_spinner=False)
def _cached_event_options() -> list[dict[str, str]]:
    return list_event_options()


@st.cache_data(ttl=60, show_spinner=False)
def _cached_dashboard_data(event_id: str, base_url: str, news_path: str) -> dict[str, Any]:
    return load_dashboard_data(event_id, base_url=base_url, news_path=news_path)


st.set_page_config(page_title="Polymarket Whale Tracker", page_icon="PM", layout="wide")
st.title("Polymarket Whale Tracker")
st.caption("Simple dashboard for event prices, recent whale spikes, and Ollama assessments.")

default_base_url = os.getenv("POLYMARKET_API_BASE", DEFAULT_BASE_URL)
default_news_path = os.getenv("NEWS_EVENTS_PATH", "data/news_events.jsonl")

with st.sidebar:
    st.header("Controls")
    base_url = st.text_input("API base URL", value=default_base_url)
    news_path = st.text_input("News dataset path", value=default_news_path)
    refresh = st.button("Refresh dashboard", type="primary")

    if refresh:
        st.cache_data.clear()

    event_options = _cached_event_options()
    event_labels = {item["id"]: item["label"] for item in event_options}

    if event_options:
        selected_event_id = st.selectbox(
            "Event",
            options=[item["id"] for item in event_options],
            format_func=lambda event_id: event_labels.get(event_id, event_id),
        )
    else:
        st.info("No event list available from the database. Enter an event ID manually.")
        selected_event_id = st.text_input("Event ID", value="")

if not selected_event_id:
    st.warning("Select an event or enter an event ID to load the dashboard.")
    st.stop()

with st.spinner("Loading dashboard data..."):
    try:
        dashboard = _cached_dashboard_data(selected_event_id, base_url, news_path)
    except Exception as exc:
        st.error(f"Failed to load dashboard data: {exc}")
        st.stop()

event = dashboard["event"]
market = dashboard["market"]
prices = dashboard["prices"]
assessment = dashboard["assessment"]
latest_spike = dashboard["latest_spike"]

top_left, top_middle, top_right = st.columns(3)
top_left.metric("Event ID", dashboard["event_id"])
top_middle.metric("YES Price", _format_price(prices.get("yes_price")))
top_right.metric("NO Price", _format_price(prices.get("no_price")))

assessment_col, market_col = st.columns(2)

with assessment_col:
    st.subheader("Ollama Assessment")
    if assessment:
        prob_col, conf_col = st.columns(2)
        prob_col.metric("Insider Probability", _format_percent(assessment.get("probability_insider")))
        conf_col.metric("Confidence", str(assessment.get("confidence") or "N/A").title())
        st.write(assessment.get("short_summary") or "No summary returned.")
    else:
        st.warning(dashboard["assessment_error"] or "Assessment unavailable.")

with market_col:
    st.subheader("Market Summary")
    st.write(f"**Event:** {event.get('title') or event.get('name') or dashboard['event_id']}")
    st.write(f"**Market:** {market.get('title') or market.get('question') or 'N/A'}")
    st.write(f"**Category:** {event.get('category') or 'N/A'}")
    st.write(f"**Volume:** {market.get('volume') or 'N/A'}")
    st.write(f"**Liquidity:** {market.get('liquidity') or 'N/A'}")

st.subheader("Latest Spike")
if latest_spike:
    spike_a, spike_b, spike_c, spike_d = st.columns(4)
    spike_a.metric("Side", str(latest_spike.get("side") or "N/A"))
    spike_b.metric("From Price", _format_price(latest_spike.get("from_price")))
    spike_c.metric("To Price", _format_price(latest_spike.get("to_price")))
    spike_d.metric("Relative Move", _format_percent(latest_spike.get("rel_change")))
    st.caption(f"Captured at {latest_spike.get('to_ts') or 'unknown time'}")
else:
    if dashboard["spikes_error"]:
        st.warning(f"Spike history unavailable: {dashboard['spikes_error']}")
    else:
        st.info("No recent whale spikes found for this event.")

with st.expander("Details"):
    if prices.get("yes_token_id") or prices.get("no_token_id"):
        st.write("**Token IDs**")
        st.json(
            {
                "yes_token_id": prices.get("yes_token_id"),
                "no_token_id": prices.get("no_token_id"),
            }
        )

    st.write("**Recent Spikes**")
    if dashboard["recent_spikes"]:
        st.json(dashboard["recent_spikes"])
    else:
        st.write("No recent spikes available.")

    st.write("**Raw Event Metadata**")
    st.json(event)
