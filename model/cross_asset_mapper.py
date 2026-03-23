from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AssetTarget:
    symbol: str
    asset_class: str
    bias: int
    rationale: str


KEYWORD_TARGETS: tuple[tuple[tuple[str, ...], tuple[AssetTarget, ...]], ...] = (
    (
        ("oil", "crude", "opec", "brent", "wti", "energy"),
        (
            AssetTarget(symbol="USO", asset_class="commodity", bias=1, rationale="oil-sensitive ETF"),
            AssetTarget(symbol="XLE", asset_class="equity_sector", bias=1, rationale="energy equity proxy"),
        ),
    ),
    (
        ("gold", "silver", "inflation", "cpi", "precious"),
        (
            AssetTarget(symbol="GLD", asset_class="commodity", bias=1, rationale="gold proxy"),
            AssetTarget(symbol="SLV", asset_class="commodity", bias=1, rationale="silver proxy"),
            AssetTarget(symbol="TLT", asset_class="rates", bias=-1, rationale="inflation/rates sensitivity"),
        ),
    ),
    (
        ("fed", "fomc", "rate", "yield", "treasury"),
        (
            AssetTarget(symbol="TLT", asset_class="rates", bias=-1, rationale="long duration rates proxy"),
            AssetTarget(symbol="DXY", asset_class="fx", bias=1, rationale="usd proxy"),
            AssetTarget(symbol="SPY", asset_class="equity_index", bias=-1, rationale="rates risk proxy"),
        ),
    ),
    (
        ("bitcoin", "btc", "ethereum", "crypto"),
        (
            AssetTarget(symbol="BTC-USD", asset_class="crypto", bias=1, rationale="bitcoin spot proxy"),
            AssetTarget(symbol="ETH-USD", asset_class="crypto", bias=1, rationale="ethereum spot proxy"),
            AssetTarget(symbol="COIN", asset_class="single_stock", bias=1, rationale="crypto beta equity"),
        ),
    ),
    (
        ("election", "president", "senate", "house", "geopolitical", "war", "tariff"),
        (
            AssetTarget(symbol="SPY", asset_class="equity_index", bias=-1, rationale="macro risk proxy"),
            AssetTarget(symbol="GLD", asset_class="commodity", bias=1, rationale="risk-off proxy"),
            AssetTarget(symbol="DXY", asset_class="fx", bias=1, rationale="risk-off usd proxy"),
        ),
    ),
    (
        ("ai", "nvidia", "semiconductor", "chip", "cloud"),
        (
            AssetTarget(symbol="NVDA", asset_class="single_stock", bias=1, rationale="ai bellwether"),
            AssetTarget(symbol="SOXX", asset_class="equity_sector", bias=1, rationale="semiconductor basket"),
            AssetTarget(symbol="QQQ", asset_class="equity_index", bias=1, rationale="growth index proxy"),
        ),
    ),
)


def _event_text(event_row: dict[str, Any] | None, trigger_payload: dict[str, Any] | None) -> str:
    values: list[str] = []
    for source in (event_row or {}, trigger_payload or {}):
        for key in ("name", "title", "description", "trigger_type"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value.strip().lower())
    if isinstance((trigger_payload or {}).get("deterministic_feature_snapshot"), dict):
        news_ctx = (trigger_payload or {})["deterministic_feature_snapshot"].get("news_context", {})
        if isinstance(news_ctx, dict):
            for key in ("news_title", "news_source"):
                value = news_ctx.get(key)
                if isinstance(value, str) and value.strip():
                    values.append(value.strip().lower())
    return " | ".join(values)


def map_event_to_assets(
    event_row: dict[str, Any] | None,
    trigger_payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    text = _event_text(event_row, trigger_payload)
    selected: dict[str, AssetTarget] = {}

    for keywords, targets in KEYWORD_TARGETS:
        if any(keyword in text for keyword in keywords):
            for target in targets:
                selected[target.symbol] = target

    return [
        {
            "symbol": target.symbol,
            "asset_class": target.asset_class,
            "bias": target.bias,
            "mapping_rationale": target.rationale,
        }
        for target in selected.values()
    ]


__all__ = ["map_event_to_assets"]
