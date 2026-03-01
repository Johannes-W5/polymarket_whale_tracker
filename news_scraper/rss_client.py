from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional

import feedparser


@dataclass
class RSSItem:
    source: str
    title: str
    link: str
    published: Optional[datetime]
    summary: str


def _parse_datetime(entry) -> Optional[datetime]:
    published_parsed = getattr(entry, "published_parsed", None)
    if not published_parsed:
        return None
    try:
        return datetime(*published_parsed[:6], tzinfo=timezone.utc)
    except Exception:
        return None


def fetch_feed(url: str, user_agent: str) -> List[RSSItem]:
    parsed = feedparser.parse(url, agent=user_agent)
    items: List[RSSItem] = []

    for entry in parsed.entries:
        items.append(
            RSSItem(
                source=url,
                title=getattr(entry, "title", ""),
                link=getattr(entry, "link", ""),
                published=_parse_datetime(entry),
                summary=getattr(entry, "summary", ""),
            )
        )

    return items


def fetch_all_feeds(
    feeds: Iterable[str],
    user_agent: str,
) -> List[RSSItem]:
    items: List[RSSItem] = []
    for url in feeds:
        try:
            items.extend(fetch_feed(url, user_agent=user_agent))
        except Exception as exc:
            # Intentionally swallow individual feed errors so the pipeline keeps running.
            print(f"[RSS] Failed to fetch {url}: {exc}")
    return items

