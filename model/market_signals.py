from __future__ import annotations

"""
Derived market-activity signals for Polymarket events.

This module builds on the local proxy in `server/main.py` and the
helpers in `model.event_prices` to compute:

- Trade volume statistics (from the Data API `/trades` endpoint)
- Order book imbalance (from the CLOB `/book` endpoint)
- Open interest snapshots (from the Data API `/oi` endpoint)
- Price history statistics such as a simple z-score for the latest move
  (from the CLOB `/prices-history` endpoint)
- News timing helpers over the JSONL stream produced by `news_scraper`
  (RSS + X/Twitter).

All functions are synchronous and intended to be used from CLI tools,
background jobs, or FastAPI routes.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Iterable, List, Optional, Sequence, Tuple

import httpx

from .event_prices import DEFAULT_BASE_URL, _parse_clob_token_ids
_HTTP_CLIENTS: dict[tuple[str, float], httpx.Client] = {}


# ---------- Shared helpers ----------


def _base_url(base_url: str | None) -> str:
    return (base_url or DEFAULT_BASE_URL).rstrip("/")


def _parse_iso8601_utc(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        # Accept both "...Z" and "+00:00" style suffixes.
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _fetch_event(
    event_id: str,
    *,
    base_url: str | None = None,
    timeout: float = 30.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    base = _base_url(base_url)
    if client is None:
        key = (base, float(timeout))
        client = _HTTP_CLIENTS.get(key)
        if client is None:
            client = httpx.Client(timeout=timeout)
            _HTTP_CLIENTS[key] = client
    r = client.get(f"{base}/events/{event_id}")
    r.raise_for_status()
    return r.json()


def _extract_markets_from_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    markets = event.get("markets") or []
    return markets if isinstance(markets, list) else []


def _select_condition_id(market: dict[str, Any]) -> Optional[str]:
    """
    Best-effort extraction of a condition/market identifier usable with
    Data API `/oi` and `/trades` and CLOB `/prices-history`.
    """
    cid = (
        market.get("conditionId")
        or market.get("condition_id")
        or market.get("id")
        or market.get("market")
    )
    if not cid:
        return None
    return str(cid)


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class MarketMetadata:
    event_id: str
    market_id: str
    # Identifier used by the Data API `/oi` endpoint (often `conditionId`),
    # which can differ from `market_id`.
    condition_market_id: str | None
    title: str | None
    liquidity: float | None
    volume: float | None
    yes_token_id: str | None
    no_token_id: str | None


def fetch_primary_market_metadata(
    event_id: str,
    *,
    base_url: str | None = None,
    market_index: int = 0,
    timeout: float = 30.0,
    event: dict[str, Any] | None = None,
) -> MarketMetadata | None:
    event = event or _fetch_event(event_id, base_url=base_url, timeout=timeout)
    markets = _extract_markets_from_event(event)
    if not markets:
        return None
    open_markets = [m for m in markets if isinstance(m, dict) and not m.get("closed", False)]
    market = open_markets[0] if open_markets else (
        markets[market_index] if market_index < len(markets) else markets[0]
    )
    if not isinstance(market, dict):
        return None
    yes_token_id, no_token_id = _parse_clob_token_ids(market)
    condition_market_id = _select_condition_id(market)
    market_id = market.get("id") or condition_market_id
    if market_id is None:
        return None
    return MarketMetadata(
        event_id=event_id,
        market_id=str(market_id),
        condition_market_id=str(condition_market_id) if condition_market_id is not None else None,
        title=str(market.get("title") or market.get("question") or "").strip() or None,
        liquidity=_coerce_float(market.get("liquidity")),
        volume=_coerce_float(market.get("volume")),
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
    )


# ---------- Volume statistics (Data API /trades) ----------


@dataclass
class VolumeStats:
    event_id: str
    total_volume: float
    buy_volume: float
    sell_volume: float
    trade_count: int


@dataclass
class TradeBurstStats:
    event_id: str
    as_of: datetime
    recent_window_minutes: float
    baseline_window_minutes: float
    recent_trade_count: int
    baseline_trade_count: float
    trade_count_burst: float
    recent_total_volume: float
    baseline_total_volume: float
    volume_burst: float
    recent_buy_volume: float
    recent_sell_volume: float
    aggressor_imbalance: float


def _fetch_event_trades(
    event_id: str,
    *,
    base_url: str | None = None,
    limit: int = 1000,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """
    Fetch recent trades for an event via the Data API `/trades` endpoint.

    Uses `eventId` for filtering and `takerOnly=true` to focus on filled
    trades. The Data API returns either a bare list or an object with a
    `trades` field; both are handled.
    """
    base = _base_url(base_url)
    params = {
        "eventId": event_id,
        "limit": max(1, min(limit, 10_000)),
        "takerOnly": "true",
    }
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{base}/trades", params=params)
        r.raise_for_status()
        payload = r.json()

    if isinstance(payload, list):
        trades = payload
    elif isinstance(payload, dict):
        trades = payload.get("trades") or []
    else:
        trades = []

    return [t for t in trades if isinstance(t, dict)]


def compute_volume_stats(
    event_id: str,
    *,
    base_url: str | None = None,
    limit: int = 1000,
    timeout: float = 30.0,
) -> VolumeStats:
    """
    Aggregate recent trade volume for an event.

    - `total_volume`: sum of absolute sizes over the last `limit` trades.
    - `buy_volume`: sum of BUY sizes.
    - `sell_volume`: sum of SELL sizes.
    """
    trades = _fetch_event_trades(
        event_id,
        base_url=base_url,
        limit=limit,
        timeout=timeout,
    )

    total = 0.0
    buy = 0.0
    sell = 0.0
    count = 0

    for t in trades:
        raw_size = t.get("size") or t.get("amount")
        try:
            size = abs(float(raw_size))
        except (TypeError, ValueError):
            continue

        side = (t.get("side") or t.get("takerSide") or "").upper()
        total += size
        if side == "BUY":
            buy += size
        elif side == "SELL":
            sell += size
        count += 1

    return VolumeStats(
        event_id=event_id,
        total_volume=total,
        buy_volume=buy,
        sell_volume=sell,
        trade_count=count,
    )


def _parse_trade_time(raw_trade: dict[str, Any]) -> datetime | None:
    for key in (
        "timestamp",
        "createdAt",
        "created_at",
        "matchTime",
        "matchedAt",
        "time",
    ):
        value = raw_trade.get(key)
        if isinstance(value, (int, float)):
            try:
                ts_value = float(value)
                if ts_value > 1_000_000_000_000:
                    ts_value /= 1000.0
                return datetime.fromtimestamp(ts_value, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
        parsed = _parse_iso8601_utc(value if isinstance(value, str) else None)
        if parsed is not None:
            return parsed
    return None


def compute_trade_burst_stats(
    event_id: str,
    *,
    base_url: str | None = None,
    as_of: datetime | None = None,
    recent_window_minutes: float = 5.0,
    baseline_window_minutes: float = 60.0,
    limit: int = 1000,
    timeout: float = 30.0,
) -> TradeBurstStats:
    trades = _fetch_event_trades(
        event_id,
        base_url=base_url,
        limit=limit,
        timeout=timeout,
    )
    as_of_utc = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    recent_start = as_of_utc - timedelta(minutes=max(recent_window_minutes, 1.0))
    baseline_start = as_of_utc - timedelta(
        minutes=max(recent_window_minutes + baseline_window_minutes, recent_window_minutes + 1.0)
    )

    recent_trade_count = 0
    recent_total_volume = 0.0
    recent_buy_volume = 0.0
    recent_sell_volume = 0.0
    older_trade_count = 0
    older_total_volume = 0.0

    for trade in trades:
        trade_time = _parse_trade_time(trade)
        if trade_time is None:
            continue
        if trade_time > as_of_utc or trade_time < baseline_start:
            continue
        size = _coerce_float(trade.get("size") or trade.get("amount"))
        if size is None:
            continue
        size = abs(size)
        side = str(trade.get("side") or trade.get("takerSide") or "").upper()
        if trade_time >= recent_start:
            recent_trade_count += 1
            recent_total_volume += size
            if side == "BUY":
                recent_buy_volume += size
            elif side == "SELL":
                recent_sell_volume += size
        else:
            older_trade_count += 1
            older_total_volume += size

    baseline_windows = max(baseline_window_minutes / max(recent_window_minutes, 1.0), 1.0)
    baseline_trade_count = older_trade_count / baseline_windows
    baseline_total_volume = older_total_volume / baseline_windows

    trade_count_burst = recent_trade_count / max(baseline_trade_count, 1.0)
    volume_burst = recent_total_volume / max(baseline_total_volume, 1.0)
    aggressor_total = recent_buy_volume + recent_sell_volume
    aggressor_imbalance = (
        (recent_buy_volume - recent_sell_volume) / aggressor_total
        if aggressor_total > 0
        else 0.0
    )

    return TradeBurstStats(
        event_id=event_id,
        as_of=as_of_utc,
        recent_window_minutes=recent_window_minutes,
        baseline_window_minutes=baseline_window_minutes,
        recent_trade_count=recent_trade_count,
        baseline_trade_count=baseline_trade_count,
        trade_count_burst=trade_count_burst,
        recent_total_volume=recent_total_volume,
        baseline_total_volume=baseline_total_volume,
        volume_burst=volume_burst,
        recent_buy_volume=recent_buy_volume,
        recent_sell_volume=recent_sell_volume,
        aggressor_imbalance=aggressor_imbalance,
    )


# ---------- Order book imbalance (CLOB /book) ----------


@dataclass
class OrderbookImbalance:
    event_id: str
    token_id: str
    side: str  # "YES" or "NO"
    bid_depth: float
    ask_depth: float
    imbalance: float  # (bid_depth - ask_depth) / (bid_depth + ask_depth)
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    spread_bps: float | None = None
    depth_near_touch: float = 0.0


def _fetch_order_book(
    token_id: str,
    *,
    base_url: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    base = _base_url(base_url)
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{base}/book", params={"token_id": token_id})
        if r.status_code in (400, 404):
            # Some tokens legitimately have no active CLOB book. Treat as a
            # normal missing-feature case and continue without noisy logging.
            return {}
        r.raise_for_status()
        data = r.json()
    return data if isinstance(data, dict) else {}


def compute_orderbook_imbalance_for_event(
    event_id: str,
    *,
    base_url: str | None = None,
    market_index: int = 0,
    max_levels: int = 5,
    timeout: float = 30.0,
    event: dict[str, Any] | None = None,
) -> list[OrderbookImbalance]:
    """
    Compute order book imbalance for the first market of an event.

    For each side (YES/NO), we:
    - Sum bid size over the top `max_levels` levels.
    - Sum ask size over the top `max_levels` levels.
    - Compute imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth).
    """
    event = event or _fetch_event(event_id, base_url=base_url, timeout=timeout)
    markets = _extract_markets_from_event(event)
    if not markets:
        return []
    market = markets[market_index] if market_index < len(markets) else markets[0]

    yes_token_id, no_token_id = _parse_clob_token_ids(market)
    results: list[OrderbookImbalance] = []

    def _compute_for_token(token_id: Optional[str], side_label: str) -> None:
        if not token_id:
            return
        book = _fetch_order_book(token_id, base_url=base_url, timeout=timeout)
        bids = book.get("bids") or []
        asks = book.get("asks") or []

        def _extract_price_size(level: Any) -> tuple[float | None, float | None]:
            try:
                if isinstance(level, dict):
                    raw_price = (
                        level.get("price")
                        or level.get("rate")
                        or level.get("value")
                    )
                    raw_size = (
                        level.get("size")
                        or level.get("quantity")
                        or level.get("amount")
                    )
                    return _coerce_float(raw_price), _coerce_float(raw_size)
                if len(level) >= 2:
                    return _coerce_float(level[0]), _coerce_float(level[1])
            except (TypeError, KeyError):
                return None, None
            return None, None

        def _depth(levels: Sequence[Any]) -> float:
            depth = 0.0
            for lvl in list(levels)[:max_levels]:
                _, size = _extract_price_size(lvl)
                if size is None:
                    continue
                depth += max(size, 0.0)
            return depth

        def _best_price(levels: Sequence[Any], *, highest: bool) -> float | None:
            prices = []
            for lvl in levels:
                price, _ = _extract_price_size(lvl)
                if price is None or price <= 0:
                    continue
                prices.append(price)
            if not prices:
                return None
            return max(prices) if highest else min(prices)

        def _top_level_depth(levels: Sequence[Any]) -> float:
            top_levels = list(levels)[:2]
            depth = 0.0
            for lvl in top_levels:
                _, size = _extract_price_size(lvl)
                if size is None:
                    continue
                depth += max(size, 0.0)
            return depth

        bid_depth = _depth(bids)
        ask_depth = _depth(asks)
        denom = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / denom if denom > 0 else 0.0
        best_bid = _best_price(bids, highest=True)
        best_ask = _best_price(asks, highest=False)
        spread = None
        spread_bps = None
        if best_bid is not None and best_ask is not None and best_ask >= best_bid:
            spread = best_ask - best_bid
            mid = (best_ask + best_bid) / 2.0
            if mid > 0:
                spread_bps = (spread / mid) * 10_000.0
        depth_near_touch = _top_level_depth(bids) + _top_level_depth(asks)

        results.append(
            OrderbookImbalance(
                event_id=event_id,
                token_id=token_id,
                side=side_label,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                imbalance=imbalance,
                best_bid=best_bid,
                best_ask=best_ask,
                spread=spread,
                spread_bps=spread_bps,
                depth_near_touch=depth_near_touch,
            )
        )

    _compute_for_token(yes_token_id, "YES")
    _compute_for_token(no_token_id, "NO")
    return results


# ---------- Open interest (Data API /oi) ----------


@dataclass
class OpenInterestSnapshot:
    event_id: str
    market_id: str
    value: float


@dataclass
class OpenInterestChange:
    event_id: str
    market_id: str
    from_value: float
    to_value: float
    abs_change: float
    rel_change: float


def fetch_open_interest_for_event(
    event_id: str,
    *,
    base_url: str | None = None,
    timeout: float = 30.0,
    event: dict[str, Any] | None = None,
) -> list[OpenInterestSnapshot]:
    """
    Fetch open interest values for all markets of an event via Data API `/oi`.
    """
    base = _base_url(base_url)
    event = event or _fetch_event(event_id, base_url=base_url, timeout=timeout)
    markets = _extract_markets_from_event(event)
    if not markets:
        return []

    # Collect condition IDs for all markets belonging to the event.
    market_ids: list[str] = []
    for m in markets:
        cid = _select_condition_id(m)
        if cid:
            market_ids.append(cid)
    if not market_ids:
        return []

    params = [("market", mid) for mid in market_ids]
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{base}/oi", params=params)
        r.raise_for_status()
        payload = r.json()

    snapshots: list[OpenInterestSnapshot] = []
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            mid = item.get("market")
            val = item.get("value")
            try:
                value_f = float(val)
            except (TypeError, ValueError):
                continue
            if not mid:
                continue
            snapshots.append(
                OpenInterestSnapshot(
                    event_id=event_id,
                    market_id=str(mid),
                    value=value_f,
                )
            )
    return snapshots


def compute_open_interest_change(
    prev: OpenInterestSnapshot,
    curr: OpenInterestSnapshot,
) -> OpenInterestChange:
    """
    Pure helper for computing open interest deltas between two snapshots.
    """
    delta = curr.value - prev.value
    if prev.value == 0.0:
        # Represent "activation from zero" with a well-defined relative change,
        # preserving sign if OI ever becomes negative (unlikely).
        denom = max(abs(curr.value), 1e-12)
        rel = (delta / denom) if denom > 0 else 0.0
    else:
        rel = delta / prev.value
    return OpenInterestChange(
        event_id=curr.event_id,
        market_id=curr.market_id,
        from_value=prev.value,
        to_value=curr.value,
        abs_change=delta,
        rel_change=rel,
    )


# ---------- Price history refinement (CLOB /prices-history) ----------


@dataclass
class PriceHistoryStats:
    event_id: str
    market_id: str
    last_price: Optional[float]
    last_return: Optional[float]
    z_score: Optional[float]
    window: int
    mean_return: Optional[float] = None
    realized_volatility: Optional[float] = None


def _fetch_price_history(
    market_id: str,
    *,
    base_url: str | None = None,
    interval: str = "1m",
    fidelity: Optional[int] = None,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    base = _base_url(base_url)
    params: dict[str, Any] = {"market": market_id, "interval": interval}
    min_fidelity_by_interval = {
        "1m": 10,
        "1w": 5,
    }
    resolved_fidelity = fidelity
    required_fidelity = min_fidelity_by_interval.get(interval)
    if required_fidelity is not None:
        resolved_fidelity = max(required_fidelity, resolved_fidelity or required_fidelity)
    if resolved_fidelity is not None:
        params["fidelity"] = int(resolved_fidelity)
    if start_ts is not None:
        params["startTs"] = int(start_ts)
    if end_ts is not None:
        params["endTs"] = int(end_ts)
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{base}/prices-history", params=params)
        if r.status_code in (400, 404):
            print(
                f"[market-signals] No usable price history for market {market_id}: {r.status_code}",
                flush=True,
            )
            return []
        r.raise_for_status()
        payload = r.json()

    history = payload.get("history") if isinstance(payload, dict) else None
    if not isinstance(history, list):
        return []
    return [h for h in history if isinstance(h, dict)]


def compute_price_history_stats_for_event(
    event_id: str,
    *,
    base_url: str | None = None,
    interval: str = "1m",
    max_points: int = 200,
    window: int = 50,
    timeout: float = 30.0,
    event: dict[str, Any] | None = None,
) -> list[PriceHistoryStats]:
    """
    Compute simple price-history-based stats for each market of an event.

    For each market we:
    - Fetch recent prices via `/prices-history`.
    - Compute last absolute return `p_t - p_{t-1}`.
    - Compute a z-score of the last return relative to the previous `window`
      returns (or all, if there are fewer).
    """
    event = event or _fetch_event(event_id, base_url=base_url, timeout=timeout)
    markets = _extract_markets_from_event(event)
    if not markets:
        return []

    results: list[PriceHistoryStats] = []
    for m in markets:
        yes_token_id, no_token_id = _parse_clob_token_ids(m)
        for token_id in (yes_token_id, no_token_id):
            if not token_id:
                continue

            history = _fetch_price_history(
                token_id,
                base_url=base_url,
                interval=interval,
                timeout=timeout,
            )
            if len(history) < 2:
                results.append(
                    PriceHistoryStats(
                        event_id=event_id,
                        market_id=token_id,
                        last_price=None,
                        last_return=None,
                        z_score=None,
                        window=0,
                        mean_return=None,
                        realized_volatility=None,
                    )
                )
                continue

            # Sort by timestamp in case the API doesn't guarantee ordering.
            history_sorted = sorted(history, key=lambda h: int(h.get("t", 0)))
            if max_points and len(history_sorted) > max_points:
                history_sorted = history_sorted[-max_points:]

            prices: list[float] = []
            for h in history_sorted:
                try:
                    prices.append(float(h.get("p")))
                except (TypeError, ValueError):
                    continue

            if len(prices) < 2:
                results.append(
                    PriceHistoryStats(
                        event_id=event_id,
                        market_id=token_id,
                        last_price=prices[-1] if prices else None,
                        last_return=None,
                        z_score=None,
                        window=0,
                        mean_return=None,
                        realized_volatility=None,
                    )
                )
                continue

            rets: list[float] = [
                prices[i] - prices[i - 1] for i in range(1, len(prices))
            ]
            last_ret = rets[-1]
            w = min(window, len(rets) - 1) if len(rets) > 1 else 0

            if w > 1:
                window_slice = rets[-w - 1 : -1]
                mu = mean(window_slice)
                sigma = pstdev(window_slice)
                z = (last_ret - mu) / sigma if sigma > 0 else 0.0
            else:
                mu = None
                sigma = None
                z = None

            results.append(
                PriceHistoryStats(
                    event_id=event_id,
                    market_id=token_id,
                    last_price=prices[-1],
                    last_return=last_ret,
                    z_score=z,
                    window=w,
                    mean_return=mu,
                    realized_volatility=sigma,
                )
            )

    return results


# ---------- News timing over `news_events.jsonl` ----------


@dataclass
class NewsRecord:
    news_time: datetime
    ingested_at: datetime
    source: str
    title: str
    text: str


@dataclass
class NewsTiming:
    event_id: str
    signal_time: datetime
    news_time: datetime
    delta_minutes: float  # news_time - signal_time (negative => news before signal)
    source: str
    title: str


_NEWS_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "vs",
    "will",
    "with",
}


# In-memory cache to avoid re-parsing the JSONL dataset for every spike.
# Cache key includes file mtime + size to invalidate when the dataset changes.
_NEWS_RECORDS_CACHE: dict[tuple[str, float, int], list["NewsRecord"]] = {}


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _tokenize_keywords(value: str) -> list[str]:
    return [
        token
        for token in _normalize_text(value).split()
        if len(token) >= 3 and token not in _NEWS_STOPWORDS
    ]


def _build_event_news_terms(event: dict[str, Any]) -> tuple[str, set[str]]:
    raw_title = str(event.get("title") or event.get("name") or "").strip()
    raw_slug = str(event.get("slug") or "").replace("-", " ").strip()
    title_normalized = _normalize_text(raw_title)

    tokens = set(_tokenize_keywords(raw_title))
    tokens.update(_tokenize_keywords(raw_slug))
    return title_normalized, tokens


def _record_matches_event(
    record: NewsRecord,
    *,
    event_title: str,
    event_terms: set[str],
) -> bool:
    haystack = _normalize_text(f"{record.title} {record.text}")
    if not haystack:
        return False

    if event_title and len(event_title) >= 12 and event_title in haystack:
        return True

    if not event_terms:
        return False

    haystack_terms = set(haystack.split())
    overlap = event_terms & haystack_terms
    if len(overlap) >= 2:
        return True

    # For short event names like "Trump" or "Bitcoin", a single distinctive term
    # can still be informative enough to count as a match.
    return len(overlap) == 1 and any(len(term) >= 6 for term in overlap)


def _load_news_records(path: str | Path) -> list[NewsRecord]:
    file_path = Path(path)
    if not file_path.exists():
        return []

    # Cache by (absolute path, mtime, size) so it invalidates when the JSONL changes.
    try:
        stat = file_path.stat()
        cache_key = (str(file_path.resolve()), float(stat.st_mtime), int(stat.st_size))
        cached = _NEWS_RECORDS_CACHE.get(cache_key)
        if cached is not None:
            return cached
    except OSError:
        cache_key = None

    records: list[NewsRecord] = []
    with file_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue

            ing_raw = raw.get("ingested_at")
            ingested_at = _parse_iso8601_utc(ing_raw)
            if ingested_at is None:
                continue

            if "rss" in raw:
                rss = raw.get("rss") or {}
                title = str(rss.get("title") or "")
                text = str(rss.get("summary") or "")
                source = str(rss.get("source") or rss.get("link") or "rss")
                news_time = _parse_iso8601_utc(rss.get("published")) or ingested_at
            elif "x" in raw:
                x = raw.get("x") or {}
                title = str(x.get("text") or "")
                text = title
                source = f"x:{x.get('query') or x.get('author_id') or ''}"
                news_time = _parse_iso8601_utc(x.get("created_at")) or ingested_at
            else:
                continue

            records.append(
                NewsRecord(
                    news_time=news_time,
                    ingested_at=ingested_at,
                    source=source,
                    title=title,
                    text=text,
                )
            )

    if cache_key is not None:
        _NEWS_RECORDS_CACHE[cache_key] = records
        # Keep cache small; typical runtime sees only a handful of news files.
        if len(_NEWS_RECORDS_CACHE) > 6:
            _NEWS_RECORDS_CACHE.pop(next(iter(_NEWS_RECORDS_CACHE)))

    return records


def find_nearest_news_for_event(
    event_id: str,
    signal_time: datetime,
    *,
    base_url: str | None = None,
    news_path: str | Path = "news_scraper/data/news_events.jsonl",
    window_minutes: float = 120.0,
    event: dict[str, Any] | None = None,
) -> Optional[NewsTiming]:
    """
    Find the nearest news record related to an event around a given signal time.

    Heuristic:
    - Fetch the event name from Gamma `/events/{id}` and use it as a search term.
    - Scan the JSONL news file for records whose title/text contains the event
      name (case-insensitive).
    - Among those, return the one with the smallest absolute time difference
      within `±window_minutes` of `signal_time`.
    """
    event = event or _fetch_event(event_id, base_url=base_url)
    event_title, event_terms = _build_event_news_terms(event)
    if not event_title and not event_terms:
        return None

    news_records = _load_news_records(news_path)
    if not news_records:
        return None

    best: Optional[Tuple[NewsRecord, float]] = None
    for rec in news_records:
        delta_sec = (rec.news_time - signal_time).total_seconds()
        delta_min = delta_sec / 60.0
        if abs(delta_min) > window_minutes:
            continue
        if not _record_matches_event(
            rec,
            event_title=event_title,
            event_terms=event_terms,
        ):
            continue

        if best is None:
            best = (rec, delta_min)
            continue

        best_delta = best[1]
        best_abs = abs(best_delta)
        cur_abs = abs(delta_min)

        # Tie-break: prefer post-news evidence (negative delta) when abs(delta)
        # ties. I.e., for equal distances choose the smaller delta_min.
        if cur_abs < best_abs - 1e-9:
            best = (rec, delta_min)
        elif abs(cur_abs - best_abs) <= 1e-9 and delta_min < best_delta:
            best = (rec, delta_min)

    if best is None:
        return None

    rec, delta_min = best
    return NewsTiming(
        event_id=event_id,
        signal_time=signal_time.astimezone(timezone.utc),
        news_time=rec.news_time,
        delta_minutes=delta_min,
        source=rec.source,
        title=rec.title,
    )


__all__ = [
    "MarketMetadata",
    "VolumeStats",
    "TradeBurstStats",
    "OrderbookImbalance",
    "OpenInterestSnapshot",
    "OpenInterestChange",
    "PriceHistoryStats",
    "NewsRecord",
    "NewsTiming",
    "fetch_primary_market_metadata",
    "compute_volume_stats",
    "compute_trade_burst_stats",
    "compute_orderbook_imbalance_for_event",
    "fetch_open_interest_for_event",
    "compute_open_interest_change",
    "compute_price_history_stats_for_event",
    "find_nearest_news_for_event",
]

