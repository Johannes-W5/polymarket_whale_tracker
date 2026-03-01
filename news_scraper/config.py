from dataclasses import dataclass
from typing import List, Optional


@dataclass
class RSSConfig:
    feeds: List[str]
    user_agent: str = "polymarket-news-scraper/0.1"


@dataclass
class XConfig:
    """Configuration for fetching posts from X (Twitter)."""

    enabled: bool = False
    queries: List[str] | None = None
    max_results: int = 50
    bearer_env_var: str = "X_BEARER_TOKEN"


@dataclass
class PipelineConfig:
    rss: RSSConfig
    x: Optional[XConfig] = None
    output_path: str = "data/news_events.jsonl"


def load_config() -> PipelineConfig:
    rss_feeds = [
        # Add or change feeds here
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://www.theblock.co/rss",
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    ]

    rss_cfg = RSSConfig(feeds=rss_feeds)

    # Default X configuration is disabled; enable and adjust queries as needed.
    x_cfg = XConfig(
        enabled=False,
        queries=[
            "polymarket lang:en",
            "prediction market lang:en",
            "election odds polymarket lang:en",
        ],
        max_results=50,
    )

    return PipelineConfig(
        rss=rss_cfg,
        x=x_cfg,
    )

