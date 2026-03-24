# Deploy on Render

This project runs as multiple Render services defined in [`render.yaml`](render.yaml).

## Architecture

| Render service | Role |
|----------------|------|
| `polymarket-proxy` | FastAPI proxy ([`server/main.py`](server/main.py)), public URL for Gamma/Data/CLOB |
| `whaletracker-gui` | Streamlit ([`gui/app.py`](gui/app.py)) |
| `whaletracker-pipeline` | Background worker: RSS/news loop + `insider_detection` (shared persistent disk for JSONL) |
| `whaletracker-event-cache` | Cron: `python -m model.event_cache` |
| `whaletracker-cross-asset` | Cron: cross-asset predictions batch |
| `whaletracker-db` | Render Postgres |

**Why one pipeline worker?** The news scraper writes `news_events.jsonl` and the detector reads it. Render persistent disks attach to a single service, so separate news + detection workers would not share the same file without extra infrastructure (object storage, etc.).

## Prerequisites

- GitHub repo connected to Render (or GitLab).
- Render account with **paid** instance types if you use **background workers**, **cron jobs**, or **disks** (Render does not offer these on the free web tier).

## One-time: Blueprint deploy

1. In the Render Dashboard: **New** → **Blueprint**.
2. Select this repository and confirm `render.yaml` path (repo root).
3. When prompted, set **sync: false** secrets:
   - **`POLYMARKET_API_BASE`** on `whaletracker-pipeline`, `whaletracker-event-cache`, `whaletracker-cross-asset`, and `whaletracker-gui`: use the public URL of **`polymarket-proxy`**, e.g. `https://polymarket-proxy.onrender.com` (no trailing slash).
   - **`OLLAMA_API_KEY`** on services that call the LLM (`whaletracker-gui`, `whaletracker-pipeline`, `whaletracker-cross-asset`) if you use cloud models.
   - **`X_BEARER_TOKEN`** on `whaletracker-pipeline` only if you enable X/Twitter in [`news_scraper/config.py`](news_scraper/config.py).

4. Deploy. Wait for **`polymarket-proxy`** to be live before the pipeline and crons can reach the API.

## Environment variables reference

| Variable | Where | Purpose |
|----------|--------|---------|
| `DATABASE_URL` | Auto from Render Postgres | Preferred DB connection ([`database/connection.py`](database/connection.py)) |
| `PG_DB`, `PG_USER`, `PG_PASSWORD`, `PG_HOST`, `PG_PORT` | Local / external DB | Alternative to `DATABASE_URL` |
| `POLYMARKET_API_BASE` | GUI, pipeline, crons | Base URL of your deployed proxy (HTTPS) |
| `NEWS_EVENTS_PATH`, `NEWS_EVENTS_METADATA_PATH` | Pipeline (set in Blueprint) | JSONL paths on the persistent disk |
| `NEWS_SCRAPER_INTERVAL_SECONDS` | Pipeline | RSS loop interval (default `300`) |
| `INSIDER_DETECTION_MAX_EVENTS` | Pipeline | Optional cap override for `--max-events` |
| `OLLAMA_API_KEY`, `OLLAMA_HOST`, `OLLAMA_MODEL` | GUI, pipeline, cross-asset cron | LLM / Ollama Cloud |
| `X_BEARER_TOKEN` | Pipeline | X API (if enabled) |

## Smoke checks

After deploy, from your machine:

```bash
export PROXY_URL=https://<your-polymarket-proxy>.onrender.com
bash scripts/render_smoke_check.sh
```

Or open `https://<proxy>/health` and `https://<proxy>/docs` in a browser.

**Order of operations for a cold start:**

1. `polymarket-proxy` healthy (`/health`).
2. Run or wait for **`whaletracker-event-cache`** cron (or trigger manually) so the `events` table is populated.
3. **`whaletracker-pipeline`** starts `insider_detection --all-events`; it exits immediately if no event IDs exist in the DB—ensure event-cache has run at least once.
4. Open **`whaletracker-gui`** URL.

## Health checks

- Proxy: `GET /health` ([`server/main.py`](server/main.py)).
- Streamlit: `/_stcore/health` (configured in `render.yaml`).

## Operational notes

- **Persistent disk** disables zero-downtime deploys for `whaletracker-pipeline`; plan maintenance accordingly.
- **DB backups**: enable in Render Postgres settings.
- **Alerts**: configure Render notifications for deploy failures and service crashes.
- **Secrets**: never commit API keys; use Render environment variables only.

## Local development

See [`.env.example`](.env.example). Use `PG_*` or `DATABASE_URL` plus `POLYMARKET_API_BASE=http://127.0.0.1:8000` when running the stack locally.
