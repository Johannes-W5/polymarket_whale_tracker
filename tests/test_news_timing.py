from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import model.market_signals as ms


def _write_news_jsonl(path, *, records) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def test_find_nearest_news_sign_convention(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        ms,
        "_fetch_event",
        lambda event_id, **kwargs: {"title": "Open Interest Surprise Report", "slug": "open-interest-surprise-report"},
    )

    signal = datetime(2026, 3, 22, 12, 0, tzinfo=timezone.utc)
    published = signal + timedelta(minutes=7)

    news_path = tmp_path / "news.jsonl"
    _write_news_jsonl(
        news_path,
        records=[
            {
                "ingested_at": signal.isoformat(),
                "rss": {
                    "source": "test",
                    "title": "Open Interest Surprise Report - update",
                    "link": "http://example.com",
                    "published": published.isoformat(),
                    "summary": "irrelevant",
                },
            }
        ],
    )

    timing = ms.find_nearest_news_for_event(
        "event-1",
        signal_time=signal,
        base_url="http://proxy.local",
        news_path=news_path,
        window_minutes=20.0,
    )

    assert timing is not None
    assert timing.delta_minutes == 7.0


def test_find_nearest_news_tie_break_prefers_post_news(monkeypatch, tmp_path) -> None:
    """
    When two matching records have equal abs(delta), prefer post-news evidence
    (negative delta => news_time < signal_time).
    """
    monkeypatch.setattr(
        ms,
        "_fetch_event",
        lambda event_id, **kwargs: {"title": "Open Interest Surprise Report", "slug": "open-interest-surprise-report"},
    )

    signal = datetime(2026, 3, 22, 12, 0, tzinfo=timezone.utc)
    neg = signal - timedelta(minutes=5)  # delta = -5
    pos = signal + timedelta(minutes=5)  # delta = +5

    # Put the +5 record first to ensure tie-break is not dependent on scan order.
    news_path = tmp_path / "news.jsonl"
    _write_news_jsonl(
        news_path,
        records=[
            {
                "ingested_at": signal.isoformat(),
                "rss": {
                    "source": "test",
                    "title": "Open Interest Surprise Report - update",
                    "link": "http://example.com",
                    "published": pos.isoformat(),
                    "summary": "irrelevant",
                },
            },
            {
                "ingested_at": signal.isoformat(),
                "rss": {
                    "source": "test",
                    "title": "Open Interest Surprise Report - update",
                    "link": "http://example.com",
                    "published": neg.isoformat(),
                    "summary": "irrelevant",
                },
            },
        ],
    )

    timing = ms.find_nearest_news_for_event(
        "event-1",
        signal_time=signal,
        base_url="http://proxy.local",
        news_path=news_path,
        window_minutes=20.0,
    )

    assert timing is not None
    assert timing.delta_minutes == -5.0

