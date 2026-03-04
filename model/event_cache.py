from __future__ import annotations

"""
Cache slow-changing Polymarket event metadata in PostgreSQL.
"""

from contextlib import closing
from time import sleep
from typing import Any, Dict, List

import httpx

from database.events import insert_event
from database.connection import get_connection
from .event_prices import DEFAULT_BASE_URL


def _reset_all_events_inactive() -> None:
    """Mark every event in the DB as inactive before a fresh sync."""
    with closing(get_connection()) as conn, conn.cursor() as cur:
        cur.execute("UPDATE events SET active = FALSE;")
        conn.commit()


def fetch_events_page(
    *,
    base_url: str = DEFAULT_BASE_URL,
    limit: int = 500,
    offset: int = 0,
    timeout: float = 30.0,
    retries: int = 3,
    retry_delay: float = 5.0,
) -> List[Dict[str, Any]]:
    """
    Fetch a page of events from the local proxy.

    Retries up to `retries` times on 5xx errors before giving up.
    """
    base = base_url.rstrip("/")
    params = {"limit": max(1, min(int(limit), 1000)), "offset": max(0, int(offset))}
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.get(f"{base}/events", params=params)
                r.raise_for_status()
                payload = r.json()
            if isinstance(payload, list):
                return [e for e in payload if isinstance(e, dict)]
            return []
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            last_exc = exc
            print(
                f"[event-cache] fetch_events_page attempt {attempt + 1}/{retries} "
                f"failed with {exc.response.status_code}, retrying in {retry_delay}s..."
            )
            sleep(retry_delay)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_exc = exc
            print(
                f"[event-cache] fetch_events_page attempt {attempt + 1}/{retries} "
                f"failed ({exc}), retrying in {retry_delay}s..."
            )
            sleep(retry_delay)
    raise RuntimeError(
        f"fetch_events_page offset={offset} failed after {retries} attempts"
    ) from last_exc


def sync_events_to_db(
    *,
    base_url: str = DEFAULT_BASE_URL,
    page_size: int = 500,
    max_pages: int = 20,
    timeout: float = 30.0,
) -> int:
    """
    Sync event metadata from API into PostgreSQL.

    Uses a mark-and-sweep strategy:
    1. All existing events are reset to active=FALSE.
    2. Pages are fetched from the API and upserted; only non-closed events
       come back as active=TRUE (via _normalize_event_for_db).
    3. Any event not returned by the API (deleted upstream) stays inactive.

    Returns number of upserted rows.
    """
    _reset_all_events_inactive()
    total = 0
    for page in range(max(1, int(max_pages))):
        offset = page * page_size
        try:
            events = fetch_events_page(
                base_url=base_url,
                limit=page_size,
                offset=offset,
                timeout=timeout,
            )
        except Exception as exc:
            print(f"[event-cache] Skipping page at offset={offset}: {exc}")
            continue
        if not events:
            break
        for event in events:
            insert_event(event)
            total += 1
        if len(events) < page_size:
            break
    return total


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser(description="Backfill Polymarket events into PostgreSQL.")
    parser.add_argument(
        "--base-url",
        default=os.getenv("POLYMARKET_API_BASE", DEFAULT_BASE_URL),
        help="Base URL of the local Polymarket proxy server.",
    )
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=20)
    args = parser.parse_args()

    count = sync_events_to_db(
        base_url=args.base_url,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )
    print(f"[event-cache] upserted={count}")

