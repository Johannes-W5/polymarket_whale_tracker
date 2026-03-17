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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Iterable, List, Optional, Sequence, Tuple

import httpx

from .event_prices import DEFAULT_BASE_URL, _parse_clob_token_ids


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
) -> dict[str, Any]:
    base = _base_url(base_url)
    with httpx.Client(timeout=timeout) as client:
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


# ---------- Volume statistics (Data API /trades) ----------


@dataclass
class VolumeStats:
    event_id: str
    total_volume: float
    buy_volume: float
    sell_volume: float
    trade_count: int


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


# ---------- Order book imbalance (CLOB /book) ----------


@dataclass
class OrderbookImbalance:
    event_id: str
    token_id: str
    side: str  # "YES" or "NO"
    bid_depth: float
    ask_depth: float
    imbalance: float  # (bid_depth - ask_depth) / (bid_depth + ask_depth)


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
            print(
                f"[market-signals] No usable order book for token {token_id}: {r.status_code}",
                flush=True,
            )
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
) -> list[OrderbookImbalance]:
    """
    Compute order book imbalance for the first market of an event.

    For each side (YES/NO), we:
    - Sum bid size over the top `max_levels` levels.
    - Sum ask size over the top `max_levels` levels.
    - Compute imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth).
    """
    event = _fetch_event(event_id, base_url=base_url, timeout=timeout)
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

        def _depth(levels: Sequence[Any]) -> float:
            depth = 0.0
            for lvl in list(levels)[:max_levels]:
                try:
                    if isinstance(lvl, dict):
                        raw_size = (
                            lvl.get("size")
                            or lvl.get("quantity")
                            or lvl.get("amount")
                        )
                    elif len(lvl) >= 2:
                        raw_size = lvl[1]
                    else:
                        continue
                    size = float(raw_size)
                except (TypeError, ValueError, KeyError):
                    continue
                depth += max(size, 0.0)
            return depth

        bid_depth = _depth(bids)
        ask_depth = _depth(asks)
        denom = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / denom if denom > 0 else 0.0

        results.append(
            OrderbookImbalance(
                event_id=event_id,
                token_id=token_id,
                side=side_label,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                imbalance=imbalance,
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
) -> list[OpenInterestSnapshot]:
    """
    Fetch open interest values for all markets of an event via Data API `/oi`.
    """
    base = _base_url(base_url)
    event = _fetch_event(event_id, base_url=base_url, timeout=timeout)
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
    rel = (delta / prev.value) if prev.value > 0 else 0.0
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
) -> list[PriceHistoryStats]:
    """
    Compute simple price-history-based stats for each market of an event.

    For each market we:
    - Fetch recent prices via `/prices-history`.
    - Compute last absolute return `p_t - p_{t-1}`.
    - Compute a z-score of the last return relative to the previous `window`
      returns (or all, if there are fewer).
    """
    event = _fetch_event(event_id, base_url=base_url, timeout=timeout)
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
                z = None

            results.append(
                PriceHistoryStats(
                    event_id=event_id,
                    market_id=token_id,
                    last_price=prices[-1],
                    last_return=last_ret,
                    z_score=z,
                    window=w,
                )
            )

    return results


# ---------- News timing over `news_events.jsonl` ----------


@dataclass
class NewsRecord:
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


def _load_news_records(path: str | Path) -> list[NewsRecord]:
    file_path = Path(path)
    if not file_path.exists():
        return []

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
            elif "x" in raw:
                x = raw.get("x") or {}
                title = str(x.get("text") or "")
                text = title
                source = f"x:{x.get('query') or x.get('author_id') or ''}"
            else:
                continue

            records.append(
                NewsRecord(
                    ingested_at=ingested_at,
                    source=source,
                    title=title,
                    text=text,
                )
            )

    return records


def find_nearest_news_for_event(
    event_id: str,
    signal_time: datetime,
    *,
    base_url: str | None = None,
    news_path: str | Path = "data/news_events.jsonl",
    window_minutes: float = 120.0,
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
    event = _fetch_event(event_id, base_url=base_url)
    event_name = str(event.get("title") or event.get("name") or "").strip()
    if not event_name:
        return None

    term = event_name.lower()
    news_records = _load_news_records(news_path)
    if not news_records:
        return None

    best: Optional[Tuple[NewsRecord, float]] = None
    for rec in news_records:
        haystack = f"{rec.title} {rec.text}".lower()
        if term not in haystack:
            continue
        delta_sec = (rec.ingested_at - signal_time).total_seconds()
        delta_min = delta_sec / 60.0
        if abs(delta_min) > window_minutes:
            continue
        if best is None or abs(delta_min) < abs(best[1]):
            best = (rec, delta_min)

    if best is None:
        return None

    rec, delta_min = best
    return NewsTiming(
        event_id=event_id,
        signal_time=signal_time.astimezone(timezone.utc),
        news_time=rec.ingested_at,
        delta_minutes=delta_min,
        source=rec.source,
        title=rec.title,
    )


__all__ = [
    "VolumeStats",
    "OrderbookImbalance",
    "OpenInterestSnapshot",
    "OpenInterestChange",
    "PriceHistoryStats",
    "NewsRecord",
    "NewsTiming",
    "compute_volume_stats",
    "compute_orderbook_imbalance_for_event",
    "fetch_open_interest_for_event",
    "compute_open_interest_change",
    "compute_price_history_stats_for_event",
    "find_nearest_news_for_event",
]

