# Polymarket whale tracker

Research-oriented pipeline: Polymarket public data, optional news ingestion, anomaly scoring, and a Streamlit dashboard.

## Quick start (local)

See [`start_application`](start_application) for typical commands (API proxy, event cache, detectors, GUI).

## Deploy on Render

Step-by-step hosting guide: **[DEPLOYMENT_RENDER.md](DEPLOYMENT_RENDER.md)**  
Infrastructure as code: **[render.yaml](render.yaml)** (paid/full) and **[render.free.yaml](render.free.yaml)** (free-tier)

## Configuration

- Example environment variables: [`.env.example`](.env.example)
- Database URL: `DATABASE_URL` (Render) or `PG_*` (local / external Postgres) in [`database/connection.py`](database/connection.py)

## CI

GitHub Actions runs `pytest` on push/PR (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)).
