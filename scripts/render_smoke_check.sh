#!/usr/bin/env bash
# Manual smoke checks after deploy. Usage:
#   export PROXY_URL=https://polymarket-proxy.onrender.com
#   bash scripts/render_smoke_check.sh
set -euo pipefail

PROXY_URL="${PROXY_URL:-${POLYMARKET_API_BASE:-}}"
if [[ -z "${PROXY_URL}" ]]; then
  echo "Set PROXY_URL or POLYMARKET_API_BASE to your polymarket-proxy HTTPS origin." >&2
  exit 1
fi

BASE="${PROXY_URL%/}"
echo "Checking ${BASE}/health ..."
curl -fsS "${BASE}/health" | head -c 200 || true
echo
echo "Checking ${BASE}/events?limit=1 ..."
curl -fsS "${BASE}/events?limit=1" | head -c 400 || true
echo
