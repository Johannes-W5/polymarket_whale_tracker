from __future__ import annotations

"""
Cache slow-changing Polymarket event metadata in PostgreSQL.
"""

from typing import Any, Dict, List

import httpx

from database.events import insert_event
from .event_prices import DEFAULT_BASE_URL


def fetch_events_page(
    *,
    base_url: str = DEFAULT_BASE_URL,
    limit: int = 500,
    offset: int = 0,
    timeout: float = 30.0,
) -> List[Dict[str, Any]]:
    """
    Fetch a page of events from the local proxy.
    """
    base = base_url.rstrip("/")
    params = {"limit": max(1, min(int(limit), 1000)), "offset": max(0, int(offset))}
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{base}/events", params=params)
        r.raise_for_status()
        payload = r.json()
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    return []


def sync_events_to_db(
    *,
    base_url: str = DEFAULT_BASE_URL,
    page_size: int = 500,
    max_pages: int = 20,
    timeout: float = 30.0,
) -> int:
    """
    Backfill event metadata from API into PostgreSQL.

    Returns number of upserted rows.
    """
    total = 0
    for page in range(max(1, int(max_pages))):
        offset = page * page_size
        events = fetch_events_page(
            base_url=base_url,
            limit=page_size,
            offset=offset,
            timeout=timeout,
        )
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

