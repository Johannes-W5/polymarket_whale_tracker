#!/usr/bin/env bash
# Combined background worker for Render: RSS/news loop + insider_detection.
# JSONL lives on the attached disk so both processes share the same files.
set -euo pipefail

NEWS_EVENTS_PATH="${NEWS_EVENTS_PATH:-/var/news-data/news_events.jsonl}"
NEWS_EVENTS_METADATA_PATH="${NEWS_EVENTS_METADATA_PATH:-/var/news-data/news_events.metadata.json}"
export NEWS_EVENTS_PATH NEWS_EVENTS_METADATA_PATH

mkdir -p "$(dirname "${NEWS_EVENTS_PATH}")"
mkdir -p "$(dirname "${NEWS_EVENTS_METADATA_PATH}")"

INTERVAL="${NEWS_SCRAPER_INTERVAL_SECONDS:-300}"
MAX_EVENTS="${INSIDER_DETECTION_MAX_EVENTS:-500}"

echo "[render] Starting news scraper loop (interval=${INTERVAL}s)..." >&2
python -m news_scraper.main --loop --interval-seconds "${INTERVAL}" &
NEWS_PID=$!

cleanup() {
  echo "[render] Shutting down; stopping news scraper (pid=${NEWS_PID})..." >&2
  kill "${NEWS_PID}" 2>/dev/null || true
  wait "${NEWS_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[render] Starting insider_detection --all-events (max_events=${MAX_EVENTS})..." >&2
set +e
python -m model.insider_detection --all-events --max-events "${MAX_EVENTS}" \
  --news-path "${NEWS_EVENTS_PATH}"
DETECT_EXIT=$?
set -e
cleanup
exit "${DETECT_EXIT}"
