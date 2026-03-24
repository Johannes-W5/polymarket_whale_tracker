#!/usr/bin/env bash

usage() {
  echo "Usage: source scripts/use_env.sh <local|remote|default>"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "This script must be sourced so env vars persist in your shell."
  usage
  exit 1
fi

profile="${1:-}"
if [[ -z "$profile" ]]; then
  usage
  return 1
fi

case "$profile" in
  local)
    env_file=".env.local"
    ;;
  remote)
    # Prefer dedicated remote file; many setups only maintain .env.local (Neon + proxy URL).
    if [[ -f ".env.remote" ]]; then
      env_file=".env.remote"
    elif [[ -f ".env.local" ]]; then
      echo "[use_env] .env.remote not found; using .env.local (copy .env.remote.example → .env.remote for a split setup)." >&2
      env_file=".env.local"
    elif [[ -f ".env" ]]; then
      echo "[use_env] .env.remote not found; using .env" >&2
      env_file=".env"
    else
      echo "Missing .env.remote (and no .env.local or .env to fall back to)."
      echo "  cp .env.remote.example .env.remote   # then edit DATABASE_URL / POLYMARKET_API_BASE"
      echo "  or use: source scripts/use_env.sh local   # if .env.local already has Neon + proxy"
      return 1
    fi
    ;;
  default)
    env_file=".env"
    ;;
  *)
    usage
    return 1
    ;;
esac

if [[ ! -f "$env_file" ]]; then
  echo "Missing $env_file."
  echo "Create it from ${env_file}.example (if available) or .env.example."
  return 1
fi

# Do not change caller shell behavior permanently.
# (Using `set -euo pipefail` in a sourced script can terminate terminals.)
# Strip CRLF from env files so Windows line endings do not break sourcing
# (avoids ": command not found" / "Befehl nicht gefunden" noise).
#
# Neon URLs often include & (e.g. ...?sslmode=require&channel_binding=require).
# Without quotes around the value, bash treats & as "run in background" and
# DATABASE_URL will not be set in this shell.
set +u
set -a
# shellcheck disable=SC1090
source <(sed 's/\r$//' "$env_file")
set +a

echo "Loaded environment from $env_file"
echo "POLYMARKET_API_BASE=${POLYMARKET_API_BASE:-<unset>}"
echo "DATABASE_URL=$( [[ -n "${DATABASE_URL:-}" ]] && echo '<set>' || echo '<unset>' )"
