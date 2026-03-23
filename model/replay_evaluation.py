from __future__ import annotations

"""
Replay and evaluation helpers for frozen trigger payloads.

This module operates on shared-contract records so historical evaluation can use
the same deterministic evidence that originally triggered the research signal.
"""

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .insider_model import assess_insider_probability_from_payload
from .event_study import summarize_prediction_accuracy

REQUIRED_FIELDS = (
    "spike_id",
    "event_id",
    "market_id",
    "side",
    "from_ts",
    "to_ts",
    "deterministic_score",
    "deterministic_score_band",
    "deterministic_feature_snapshot",
    "scorer_version",
    "trigger_type",
    "signal_time",
    "news_time",
    "news_delta_minutes",
)


def _unwrap_record(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("trigger_payload")
    if isinstance(payload, dict):
        return payload
    return record


def load_records(path: str | Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Replay input not found: {file_path}")

    records: list[dict[str, Any]] = []
    with file_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                records.append(_unwrap_record(payload))
            if limit is not None and len(records) >= limit:
                break
    return records


def validate_record(record: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for field in REQUIRED_FIELDS:
        if field not in record:
            missing.append(field)
    snapshot = record.get("deterministic_feature_snapshot")
    if not isinstance(snapshot, dict):
        missing.append("deterministic_feature_snapshot(dict)")
    return missing


def summarize_records(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    record_list = list(records)
    trigger_types = Counter(str(record.get("trigger_type") or "unknown") for record in record_list)
    score_bands = Counter(str(record.get("deterministic_score_band") or "unknown") for record in record_list)
    scorer_versions = Counter(str(record.get("scorer_version") or "unknown") for record in record_list)
    prompt_versions = Counter(
        str(record.get("prompt_version") or record.get("llm_prompt_version") or "unknown")
        for record in record_list
    )
    invalid_records = sum(1 for record in record_list if validate_record(record))

    return {
        "record_count": len(record_list),
        "invalid_record_count": invalid_records,
        "trigger_types": dict(trigger_types),
        "score_bands": dict(score_bands),
        "scorer_versions": dict(scorer_versions),
        "prompt_versions": dict(prompt_versions),
    }


def replay_llm(
    records: Iterable[dict[str, Any]],
    *,
    news_path: str,
    model: str | None = None,
    temperature: float = 0.1,
) -> list[dict[str, Any]]:
    replayed: list[dict[str, Any]] = []
    for record in records:
        missing = validate_record(record)
        if missing:
            replayed.append(
                {
                    "event_id": record.get("event_id"),
                    "spike_id": record.get("spike_id"),
                    "status": "invalid",
                    "missing_fields": missing,
                }
            )
            continue

        assessment = assess_insider_probability_from_payload(
            record,
            event_id=str(record.get("event_id") or ""),
            news_path=news_path,
            model=model,
            temperature=temperature,
        )
        replayed.append(
            {
                **record,
                "llm_probability": assessment.probability_insider,
                "llm_confidence": assessment.confidence,
                "llm_summary": assessment.short_summary,
                "llm_version": assessment.llm_version,
                "prompt_hash": assessment.prompt_hash,
                "prompt_version": assessment.prompt_version,
                "deterministic_prior_probability": assessment.deterministic_prior_probability,
                "llm_probability_adjustment": assessment.probability_adjustment,
                "llm_fallback_reason": assessment.fallback_reason,
                "status": "replayed",
            }
        )
    return replayed


def _write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Validate or replay frozen research-signal trigger payloads."
    )
    parser.add_argument(
        "--input",
        required=True,
        help="JSONL file containing shared-contract trigger payloads or rows with trigger_payload.",
    )
    parser.add_argument(
        "--output",
        help="Optional JSONL output path for replayed rows.",
    )
    parser.add_argument(
        "--news-path",
        default="news_scraper/data/news_events.jsonl",
        help="News dataset path used for metadata references during replay.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optionally limit the number of rows loaded from the input file.",
    )
    parser.add_argument(
        "--rerun-llm",
        action="store_true",
        help="Re-run the explanation layer against the frozen deterministic payloads.",
    )
    parser.add_argument(
        "--ollama-model",
        dest="ollama_model",
        default=None,
        help="Optional model override for replayed explanation runs.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.1,
        help="Sampling temperature for replayed explanation runs.",
    )
    parser.add_argument(
        "--prediction-eval-input",
        default=None,
        help=(
            "Optional JSONL file with cross-asset prediction outcomes. "
            "Expected fields include predicted_direction, horizon_bucket, "
            "prediction_confidence, and realized_return."
        ),
    )
    args = parser.parse_args()

    rows = load_records(args.input, limit=args.limit)
    summary = summarize_records(rows)
    print(json.dumps(summary, indent=2, sort_keys=True))

    if args.rerun_llm:
        replayed = replay_llm(
            rows,
            news_path=args.news_path,
            model=args.ollama_model,
            temperature=args.temperature,
        )
        if args.output:
            _write_jsonl(args.output, replayed)
            print(f"Wrote replay output to {args.output}")

    if args.prediction_eval_input:
        prediction_rows = load_records(args.prediction_eval_input, limit=args.limit)
        evaluation = summarize_prediction_accuracy(prediction_rows)
        print(json.dumps({"prediction_evaluation": evaluation}, indent=2, sort_keys=True))
