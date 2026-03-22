import os
from dataclasses import dataclass
from typing import List, Optional

NEWS_PIPELINE_VERSION = "public-news-pipeline-v1"
NEWS_DATASET_CONTRACT_VERSION = "news-dataset-v1"


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
    output_path: str = "news_scraper/data/news_events.jsonl"
    metadata_path: str = "news_scraper/data/news_events.metadata.json"
    pipeline_version: str = NEWS_PIPELINE_VERSION
    dataset_contract_version: str = NEWS_DATASET_CONTRACT_VERSION


def load_config() -> PipelineConfig:
    rss_feeds = [
        # Crypto / markets
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://www.theblock.co/rss",
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
        "https://cointelegraph.com/rss",
        # Broad business / markets
        "http://feeds.reuters.com/reuters/businessNews",
        "http://feeds.reuters.com/news/economy",
        "https://www.cnbc.com/id/10001147/device/rss/rss.html",
        "https://www.cnbc.com/id/100727362/device/rss/rss.html",
        # Politics / world
        "http://feeds.reuters.com/Reuters/PoliticsNews",
        "http://feeds.reuters.com/Reuters/worldNews",
        "http://feeds.bbci.co.uk/news/world/rss.xml",
        "http://feeds.bbci.co.uk/news/politics/rss.xml",
        "https://feeds.npr.org/1001/rss.xml",
        "https://feeds.npr.org/1014/rss.xml",
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
        output_path=os.getenv("NEWS_EVENTS_PATH", "news_scraper/data/news_events.jsonl"),
        metadata_path=os.getenv(
            "NEWS_EVENTS_METADATA_PATH",
            "news_scraper/data/news_events.metadata.json",
        ),
    )

