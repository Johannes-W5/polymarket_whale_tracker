from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd
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


def _format_compact_number(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if n == 0:
        return "0"

    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000_000:
        return f"{sign}{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{sign}{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{sign}{n / 1_000:.2f}K"
    return f"{sign}{n:.2f}"


def _polymarket_event_url(event: dict[str, Any]) -> str | None:
    slug = str(event.get("slug") or "").strip()
    if not slug:
        return None
    return f"https://polymarket.com/event/{slug}"


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


def _render_assessment_section(assessment: dict[str, Any] | None, assessment_error: str | None) -> None:
    st.markdown("**Research Signal Assessment**")
    if assessment and assessment.get("has_llm_assessment"):
        prob_col, conf_col = st.columns(2)
        prob_col.metric(
            "Research Signal Probability",
            _format_percent(assessment.get("probability_insider")),
        )
        conf_col.metric(
            "Explanation Confidence",
            str(assessment.get("confidence") or "N/A").title(),
        )
        st.write(assessment.get("short_summary") or "No summary returned.")
    elif assessment:
        score_col, band_col = st.columns(2)
        score_col.metric(
            "Deterministic Score",
            _format_price(assessment.get("deterministic_score")),
        )
        band_col.metric(
            "Score Band",
            str(assessment.get("deterministic_score_band") or "N/A").title(),
        )
        st.info(
            assessment.get("llm_skip_message")
            or "No LLM explanation was persisted for this trigger."
        )
    else:
        st.warning(assessment_error or "Assessment unavailable.")


def _render_market_summary(event: dict[str, Any], market: dict[str, Any], event_id: str) -> None:
    st.markdown("**Market Summary**")
    st.write(f"**Event:** {event.get('title') or event.get('name') or event_id}")
    st.write(f"**Market:** {market.get('title') or market.get('question') or 'N/A'}")
    st.write(f"**Category:** {event.get('category') or 'N/A'}")
    st.write(f"**Volume:** {_format_compact_number(market.get('volume'))}")
    st.write(f"**Liquidity:** {_format_compact_number(market.get('liquidity'))}")
    polymarket_url = _polymarket_event_url(event)
    if polymarket_url:
        st.markdown(f"[Open on Polymarket]({polymarket_url})")


def _render_spike_details(
    spike: dict[str, Any] | None,
    *,
    title: str,
    spikes_error: str | None,
) -> None:
    st.markdown(f"**{title}**")
    if spike:
        _render_spike_metrics(spike)
        st.caption(f"Captured at {spike.get('to_ts') or 'unknown time'}")
    else:
        if spikes_error:
            st.warning(f"Spike history unavailable: {spikes_error}")
        else:
            st.info("No recent whale spikes found for this event.")


def _render_spike_history(recent_spikes: list[dict[str, Any]]) -> None:
    if recent_spikes:
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
                for spike in recent_spikes
            ]
            st.dataframe(history_rows, use_container_width=True, hide_index=True)


def _render_cross_asset_predictions(rows: list[dict[str, Any]], error: str | None) -> None:
    st.markdown("**Cross-Asset Consequence Alerts**")
    if error:
        st.warning(f"Cross-asset predictions unavailable: {error}")
        return
    if not rows:
        st.info(
            "No cross-asset consequence alert is shown because this event likely has limited "
            "direct impact on tradable assets, or the AI validation gates filtered weak/non-specific signals."
        )
        return

    view_rows = [
        {
            "asset": row.get("asset_symbol") or "N/A",
            "class": row.get("asset_class") or "N/A",
            "horizon": row.get("horizon_bucket") or "N/A",
            "direction": str(row.get("predicted_direction") or "N/A").upper(),
            "magnitude": str(row.get("predicted_magnitude_band") or "N/A").title(),
            "confidence": _format_percent(row.get("prediction_confidence")),
            "confidence_raw": row.get("prediction_confidence"),
            "score": _format_price(row.get("source_score")),
            "signal_time": row.get("signal_time") or "N/A",
        }
        for row in rows[:15]
    ]
    df = pd.DataFrame(view_rows)

    def _confidence_style(value: Any) -> str:
        try:
            pct = float(value)
        except (TypeError, ValueError):
            return ""
        if pct < 0.50:
            return "color: #d62728; font-weight: 600;"
        if pct < 0.65:
            return "color: #ff7f0e; font-weight: 600;"
        return "color: #2ca02c; font-weight: 600;"

    styled = (
        df.drop(columns=["confidence_raw"])
        .style.apply(
            lambda _: [_confidence_style(val) for val in df["confidence_raw"]],
            subset=["confidence"],
            axis=0,
        )
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _render_event_card(
    dashboard: dict[str, Any],
    *,
    label_prefix: str,
    expanded: bool,
    spike_title: str,
    spike_override: dict[str, Any] | None = None,
) -> None:
    event = dashboard["event"]
    market = dashboard["market"]
    prices = dashboard["prices"]
    assessment = dashboard["assessment"]
    spike = spike_override or dashboard["latest_spike"]

    label = (
        f"{label_prefix}: {event.get('title') or event.get('name') or dashboard['event_id']}  "
        f"({dashboard['event_id']})"
    )
    with st.expander(label, expanded=expanded):
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
            _render_assessment_section(assessment, dashboard["assessment_error"])

        with market_col:
            _render_market_summary(event, market, dashboard["event_id"])

        _render_spike_details(
            spike,
            title=spike_title,
            spikes_error=dashboard["spikes_error"],
        )
        _render_cross_asset_predictions(
            dashboard.get("cross_asset_predictions") or [],
            dashboard.get("cross_asset_error"),
        )
        _render_spike_history(dashboard["recent_spikes"])


st.set_page_config(page_title="Polymarket Whale Tracker", page_icon="🐳", layout="wide")
st.title("Polymarket Whale Tracker")
st.caption("Live public-data anomaly feed with the newest event pinned at the top.")

default_base_url = os.getenv("POLYMARKET_API_BASE", DEFAULT_BASE_URL)
default_news_path = os.getenv("NEWS_EVENTS_PATH", "news_scraper/data/news_events.jsonl")

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
    _render_event_card(
        dashboard,
        label_prefix="Latest Spike",
        expanded=True,
        spike_title="Latest Spike Details",
    )

    st.subheader("Older Whale Spikes")
    if len(recent_feed) == 1:
        st.write("No older whale spikes available yet.")
        return

    for item in recent_feed[1:]:
        event_id = str(item.get("event_id") or "").strip()
        if not event_id:
            continue
        try:
            item_dashboard = _cached_dashboard_data(event_id, base_url, news_path)
        except Exception as exc:
            st.error(f"Failed to load spike details for {event_id}: {exc}")
            continue
        _render_event_card(
            item_dashboard,
            label_prefix="Older Spike",
            expanded=False,
            spike_title="Selected Spike Details",
            spike_override=item,
        )


_render_live_dashboard()
