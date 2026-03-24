# Deploy on Render

This repository supports two deployment modes:

- Paid/full stack blueprint: [`render.yaml`](render.yaml)
- Free-tier stack blueprint: [`render.free.yaml`](render.free.yaml)

This guide focuses on the free-tier setup:

- Render Free Web for API + GUI
- Neon Free Postgres
- GitHub Actions schedules instead of Render workers/cron/disks

## Free architecture

| Component | Host | Config |
|---|---|---|
| API proxy | Render Free Web | `polymarket-proxy` in [`render.free.yaml`](render.free.yaml) |
| Streamlit GUI | Render Free Web | `whaletracker-gui` in [`render.free.yaml`](render.free.yaml) |
| Database | Neon Free Postgres | `DATABASE_URL` |
| Schedulers | GitHub Actions | [`.github/workflows/event-cache.yml`](.github/workflows/event-cache.yml), [`.github/workflows/news-scrape.yml`](.github/workflows/news-scrape.yml), [`.github/workflows/cross-asset.yml`](.github/workflows/cross-asset.yml) |

## Step-by-step free deployment

1. Create Neon Postgres and copy its connection string.
2. In Render Dashboard, create a Blueprint using `render.free.yaml`.
3. Set required Render env vars:
   - `DATABASE_URL` on `whaletracker-gui` (and optionally on proxy for future use)
   - `POLYMARKET_API_BASE` on `whaletracker-gui` to the deployed proxy URL, e.g. `https://polymarket-proxy.onrender.com`
   - Optional LLM vars: `OLLAMA_API_KEY`, `OLLAMA_HOST`, `OLLAMA_MODEL`
4. Deploy proxy first; confirm `https://<proxy>/health` works.
5. Deploy GUI and verify `/_stcore/health`.
6. Configure GitHub Secrets for scheduled jobs:
   - `DATABASE_URL`
   - `POLYMARKET_API_BASE`
   - `OLLAMA_API_KEY` (for cross-asset job)
   - Optional: `OLLAMA_HOST`, `OLLAMA_MODEL`, `X_BEARER_TOKEN`
7. Manually trigger workflows once in this order:
   - Event cache
   - News scrape
   - Cross asset

## Free-tier persistence behavior

Render free web services do not provide a shared persistent disk for this pipeline.

- Scheduled jobs run as one-shot tasks and use ephemeral files where needed.
- News workflow writes JSONL under `/tmp` for that run only.
- Dashboard data should be considered DB-backed first (events/assessments/predictions), not file-backed.

## Environment variables reference

| Variable | Used by | Notes |
|---|---|---|
| `DATABASE_URL` | GUI + GitHub scheduled jobs | Neon connection string |
| `POLYMARKET_API_BASE` | GUI + scheduled jobs | URL of deployed proxy |
| `OLLAMA_API_KEY` | GUI + cross-asset job | Required for AI cross-asset run |
| `OLLAMA_HOST` | GUI + cross-asset job | Defaults to `https://ollama.com` |
| `OLLAMA_MODEL` | GUI + cross-asset job | Defaults to `qwen3.5:cloud` |
| `X_BEARER_TOKEN` | News workflow | Only if X scraping is enabled |
| `NEWS_EVENTS_PATH` | News workflow | Set to `/tmp/news_events.jsonl` in workflow |
| `NEWS_EVENTS_METADATA_PATH` | News workflow | Set to `/tmp/news_events.metadata.json` in workflow |

## Smoke checks

From your machine:

```bash
export PROXY_URL=https://<your-polymarket-proxy>.onrender.com
bash scripts/render_smoke_check.sh
```

Also check:

- Proxy docs at `https://<proxy>/docs`
- GUI loads and can fetch recent data without backend worker services

## Free-tier caveats

- Render free web instances can sleep and cold-start.
- GitHub scheduled workflows are not exact real-time and depend on Actions capacity.
- This mode is best for low-cost demos and development, not strict low-latency monitoring.
