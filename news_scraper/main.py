from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

if __package__:
    from .config import load_config
    from .rss_client import RSSItem, fetch_all_feeds
    from .x_client import XPost, fetch_all_queries
else:  # pragma: no cover
    from config import load_config
    from rss_client import RSSItem, fetch_all_feeds
    from x_client import XPost, fetch_all_queries


def ensure_output_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _iso_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _sha256_file(path: str) -> str | None:
    file_path = Path(path)
    if not file_path.exists():
        return None

    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _jsonl_record_count(path: str) -> int:
    file_path = Path(path)
    if not file_path.exists():
        return 0
    with file_path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def _source_config_hash(cfg) -> str:
    source_payload = {
        "rss_feeds": list(cfg.rss.feeds),
        "rss_user_agent": cfg.rss.user_agent,
        "x_enabled": bool(getattr(cfg.x, "enabled", False)),
        "x_queries": list(getattr(cfg.x, "queries", []) or []),
        "x_max_results": getattr(cfg.x, "max_results", None),
    }
    encoded = json.dumps(source_payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_dataset_metadata(
    cfg,
    *,
    run_started_at: str,
    rss_written: int,
    x_written: int,
    total_seen_records: int,
) -> None:
    ensure_output_dir(cfg.metadata_path)
    metadata = {
        "updated_at": _iso_now(),
        "run_started_at": run_started_at,
        "pipeline_version": cfg.pipeline_version,
        "dataset_contract_version": cfg.dataset_contract_version,
        "output_path": cfg.output_path,
        "record_count": _jsonl_record_count(cfg.output_path),
        "seen_key_count": total_seen_records,
        "dataset_sha256": _sha256_file(cfg.output_path),
        "source_config_hash": _source_config_hash(cfg),
        "rss": {
            "feed_count": len(cfg.rss.feeds),
            "written_records": rss_written,
        },
        "x": {
            "enabled": bool(getattr(cfg.x, "enabled", False)),
            "query_count": len(getattr(cfg.x, "queries", []) or []),
            "written_records": x_written,
        },
    }
    Path(cfg.metadata_path).write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + os.linesep,
        encoding="utf-8",
    )


def _record_key(record: Dict) -> str | None:
    rss = record.get("rss")
    if isinstance(rss, dict):
        source = str(rss.get("source") or "")
        link = str(rss.get("link") or "")
        title = str(rss.get("title") or "")
        if link or title:
            return f"rss|{source}|{link}|{title}"

    x = record.get("x")
    if isinstance(x, dict):
        post_id = str(x.get("id") or "")
        query = str(x.get("query") or "")
        text = str(x.get("text") or "")
        if post_id or text:
            return f"x|{query}|{post_id}|{text}"

    return None


def load_seen_keys(path: str) -> set[str]:
    file_path = Path(path)
    if not file_path.exists():
        return set()

    seen: set[str] = set()
    with file_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = _record_key(record)
            if key:
                seen.add(key)
    return seen


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
    seen_keys = load_seen_keys(cfg.output_path)
    run_started_at = _iso_now()

    print("[pipeline] Fetching RSS feeds...")
    rss_items: List[RSSItem] = fetch_all_feeds(
        feeds=cfg.rss.feeds,
        user_agent=cfg.rss.user_agent,
    )
    print(f"[pipeline] Got {len(rss_items)} items.")

    ensure_output_dir(cfg.output_path)

    rss_written = 0
    with open(cfg.output_path, "a", encoding="utf-8") as f:
        for item in rss_items:
            record = {
                "ingested_at": _iso_now(),
                "rss": serialize_rss_item(item),
            }
            key = _record_key(record)
            if key and key in seen_keys:
                continue
            f.write(json.dumps(record) + os.linesep)
            if key:
                seen_keys.add(key)
            rss_written += 1

    print(f"[pipeline] Wrote {rss_written} new RSS records.")

    # Fetch X (Twitter) data if configured and enabled.
    x_cfg = getattr(cfg, "x", None)
    x_written = 0
    if x_cfg and x_cfg.enabled and x_cfg.queries:
        bearer = os.getenv(x_cfg.bearer_env_var)
        if not bearer:
            print(
                f"[pipeline] X scraping enabled but env var {x_cfg.bearer_env_var} "
                "is not set; skipping X fetch."
            )
        else:
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
                        "ingested_at": _iso_now(),
                        "x": serialize_x_post(post),
                    }
                    key = _record_key(record)
                    if key and key in seen_keys:
                        continue
                    f.write(json.dumps(record) + os.linesep)
                    if key:
                        seen_keys.add(key)
                    x_written += 1

            print(f"[pipeline] Wrote {x_written} new X records.")

    write_dataset_metadata(
        cfg,
        run_started_at=run_started_at,
        rss_written=rss_written,
        x_written=x_written,
        total_seen_records=len(seen_keys),
    )
    print(
        f"[pipeline] Updated dataset metadata at {cfg.metadata_path}.",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch RSS/X news into a JSONL file.")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Keep fetching news repeatedly instead of running once.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=300.0,
        help="When using --loop, wait this many seconds between fetches (default: 300).",
    )
    args = parser.parse_args()

    if not args.loop:
        run_once()
    else:
        interval_seconds = max(5.0, float(args.interval_seconds))
        print(
            f"[pipeline] Loop mode enabled. Fetching every {interval_seconds:.0f} seconds.",
            flush=True,
        )
        while True:
            try:
                run_once()
            except Exception as exc:
                print(f"[pipeline] Iteration failed: {exc}", flush=True)
            time.sleep(interval_seconds)

