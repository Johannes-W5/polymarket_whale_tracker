from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional

import httpx


API_BASE = "https://api.twitter.com/2/tweets/search/recent"


@dataclass
class XPost:
    id: str
    text: str
    author_id: Optional[str]
    created_at: Optional[datetime]
    lang: Optional[str]
    like_count: Optional[int]
    retweet_count: Optional[int]
    reply_count: Optional[int]
    quote_count: Optional[int]
    query: str


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # X/Twitter returns RFC3339 with Z suffix; normalise to UTC.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def search_recent_tweets(
    query: str,
    bearer_token: str,
    *,
    max_results: int = 50,
    timeout: float = 30.0,
) -> List[XPost]:
    max_results = max(10, min(max_results, 100))

    headers = {
        "Authorization": f"Bearer {bearer_token}",
    }
    params = {
        "query": query,
        "max_results": max_results,
        "tweet.fields": "created_at,lang,public_metrics,author_id",
    }

    with httpx.Client(timeout=timeout) as client:
        r = client.get(API_BASE, headers=headers, params=params)
        r.raise_for_status()
        payload = r.json()

    posts: List[XPost] = []
    for t in payload.get("data", []):
        metrics = t.get("public_metrics") or {}
        posts.append(
            XPost(
                id=t.get("id", ""),
                text=t.get("text", ""),
                author_id=t.get("author_id"),
                created_at=_parse_datetime(t.get("created_at")),
                lang=t.get("lang"),
                like_count=int(metrics.get("like_count", 0)),
                retweet_count=int(metrics.get("retweet_count", 0)),
                reply_count=int(metrics.get("reply_count", 0)),
                quote_count=int(metrics.get("quote_count", 0)),
                query=query,
            )
        )

    return posts


def fetch_all_queries(
    queries: Iterable[str],
    bearer_token: str,
    *,
    max_results: int = 50,
    timeout: float = 30.0,
) -> List[XPost]:
    posts: List[XPost] = []
    for q in queries:
        try:
            posts.extend(
                search_recent_tweets(
                    q,
                    bearer_token=bearer_token,
                    max_results=max_results,
                    timeout=timeout,
                )
            )
        except Exception as exc:
            print(f"[X] Failed to fetch query {q!r}: {exc}")
    return posts

