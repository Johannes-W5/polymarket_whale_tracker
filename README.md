# Polymarket whale tracker

Research-oriented pipeline: Polymarket public data, optional news ingestion, anomaly scoring, and a Streamlit dashboard.

## Quick start (local)

See [`start_application`](start_application) for typical commands (API proxy, event cache, detectors, GUI).

### Local vs remote env profiles

- Copy profile templates and fill secrets:
  - `cp .env.localdev.example .env.local`
  - `cp .env.remote.example .env.remote`
- Load one profile in your shell before running commands:
  - `source scripts/use_env.sh local`
  - `source scripts/use_env.sh remote`
- For local development, keep `POLYMARKET_API_BASE=http://127.0.0.1:8000` in `.env.local`.

## Deploy on Render

Step-by-step hosting guide: **[DEPLOYMENT_RENDER.md](DEPLOYMENT_RENDER.md)**  
Infrastructure as code: **[render.yaml](render.yaml)** (paid/full) and **[render.free.yaml](render.free.yaml)** (free-tier)

## Configuration

- Example environment variables: [`.env.example`](.env.example)
- Database URL: `DATABASE_URL` (Render) or `PG_*` (local / external Postgres) in [`database/connection.py`](database/connection.py)

## CI

GitHub Actions runs `pytest` on push/PR (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)).
