from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from config import load_config
from rss_client import RSSItem, fetch_all_feeds
from x_client import XPost, fetch_all_queries


def ensure_output_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def serialize_rss_item(item: RSSItem) -> Dict:
    return {
        "source": item.source,
        "title": item.title,
        "link": item.link,
        "published": item.published.isoformat() if item.published else None,
        "summary": item.summary,
    }


def serialize_x_post(post: XPost) -> Dict:
    return {
        "id": post.id,
        "text": post.text,
        "author_id": post.author_id,
        "created_at": post.created_at.isoformat() if post.created_at else None,
        "lang": post.lang,
        "like_count": post.like_count,
        "retweet_count": post.retweet_count,
        "reply_count": post.reply_count,
        "quote_count": post.quote_count,
        "query": post.query,
    }


def run_once() -> None:
    cfg = load_config()

    print("[pipeline] Fetching RSS feeds...")
    rss_items: List[RSSItem] = fetch_all_feeds(
        feeds=cfg.rss.feeds,
        user_agent=cfg.rss.user_agent,
    )
    print(f"[pipeline] Got {len(rss_items)} items.")

    ensure_output_dir(cfg.output_path)

    with open(cfg.output_path, "a", encoding="utf-8") as f:
        for item in rss_items:
            record = {
                "ingested_at": datetime.utcnow().isoformat() + "Z",
                "rss": serialize_rss_item(item),
            }
            f.write(json.dumps(record) + os.linesep)

    print(f"[pipeline] Wrote {len(rss_items)} RSS records.")

    # Fetch X (Twitter) data if configured and enabled.
    x_cfg = getattr(cfg, "x", None)
    if x_cfg and x_cfg.enabled and x_cfg.queries:
        bearer = os.getenv(x_cfg.bearer_env_var)
        if not bearer:
            print(
                f"[pipeline] X scraping enabled but env var {x_cfg.bearer_env_var} "
                "is not set; skipping X fetch."
            )
            return

        print("[pipeline] Fetching X posts...")
        x_posts: List[XPost] = fetch_all_queries(
            queries=x_cfg.queries,
            bearer_token=bearer,
            max_results=x_cfg.max_results,
        )
        print(f"[pipeline] Got {len(x_posts)} X posts.")

        with open(cfg.output_path, "a", encoding="utf-8") as f:
            for post in x_posts:
                record = {
                    "ingested_at": datetime.utcnow().isoformat() + "Z",
                    "x": serialize_x_post(post),
                }
                f.write(json.dumps(record) + os.linesep)

        print(f"[pipeline] Wrote {len(x_posts)} X records.")


if __name__ == "__main__":
    # For a real-time system, you can wrap `run_once` in a scheduler (cron, systemd timer,
    # Airflow, etc.) or a simple while True + sleep loop.
    run_once()

