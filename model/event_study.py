from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


def summarize_prediction_accuracy(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    total = 0
    valid = 0
    for row in rows:
        total += 1
        horizon = str(row.get("horizon_bucket") or "unknown")
        direction = str(row.get("predicted_direction") or "neutral")
        try:
            realized_return = float(row.get("realized_return"))
        except (TypeError, ValueError):
            continue
        valid += 1
        hit = (
            (direction == "up" and realized_return > 0)
            or (direction == "down" and realized_return < 0)
            or (direction == "neutral" and abs(realized_return) <= 0.001)
        )
        grouped[horizon].append(
            {
                "hit": 1 if hit else 0,
                "confidence": float(row.get("prediction_confidence") or 0.0),
            }
        )

    horizon_metrics: dict[str, Any] = {}
    for horizon, items in grouped.items():
        count = len(items)
        if count == 0:
            continue
        hit_rate = sum(item["hit"] for item in items) / count
        avg_confidence = sum(item["confidence"] for item in items) / count
        horizon_metrics[horizon] = {
            "count": count,
            "hit_rate": round(hit_rate, 4),
            "avg_confidence": round(avg_confidence, 4),
            "calibration_gap": round(avg_confidence - hit_rate, 4),
        }

    return {
        "total_rows": total,
        "valid_rows": valid,
        "horizon_metrics": horizon_metrics,
    }


__all__ = ["summarize_prediction_accuracy"]
