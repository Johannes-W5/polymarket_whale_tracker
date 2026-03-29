# Polymarket WhaleTracker Knowledge Base

## What This Project Does

Polymarket WhaleTracker is a public-data analytics system that monitors Polymarket events for unusual price behavior ("whale spikes"), scores those spikes with deterministic market features, optionally adds an LLM-based explanation layer, and stores results for dashboard and API consumption.

This is a research and monitoring tool. It does **not** execute trades, manage wallets, or place orders.

## Core Components

- `server/main.py`  
  FastAPI proxy for Gamma/Data/CLOB endpoints, plus DB-backed event listing mode.

- `model/insider_detection.py`  
  Main detector loop that samples event prices, detects spikes, computes deterministic anomaly scores, optionally invokes LLM assessment, and persists outputs.

- `model/anomaly_scoring.py`  
  Deterministic scoring engine. Produces:
  - numeric anomaly score
  - score band
  - feature snapshot
  - trigger metadata (including LLM-gating signals)

- `model/insider_model.py`  
  LLM explanation/probability refinement layer with bounded adjustments and strict response validation.

- `model/market_signals.py`  
  Feature extraction for:
  - orderbook imbalance
  - trade burst behavior
  - open interest movement
  - price-history volatility context
  - nearest related news timing

- `model/cross_asset_predictions.py`  
  Generates consequence alerts for potentially affected tradable assets based on high-score triggers.

- `database/events.py`  
  Persistence layer for events, spikes, assessments, and cross-asset predictions.

- `gui/app.py` + `gui/data.py`  
  Streamlit dashboard for live feed and daily top signals.

## End-to-End Data Flow

1. Event metadata is cached from Polymarket into Postgres (`model/event_cache.py`).
2. Detector samples prices for active events (`model/insider_detection.py`).
3. Candidate price moves are detected (absolute/relative move thresholds).
4. Market/news features are computed around each candidate.
5. Deterministic score is computed (`model/anomaly_scoring.py`).
6. If score and gate conditions are met, LLM explanation/probability is requested (`model/insider_model.py`).
7. Spike + assessment are stored in DB.
8. Optional cross-asset consequence alerts are generated and stored.
9. Streamlit dashboard and API endpoints read persisted outputs.

## Signal Types

### 1) Deterministic Anomaly Signal

Computed only from public market and timing data. Main feature families include:

- price jump magnitude (absolute and relative)
- volatility-adjusted move
- liquidity-adjusted move
- spread/depth/orderbook imbalance context
- trade burst and aggressor imbalance
- open interest relative change
- relation of signal timing to nearby news
- repeated anomaly context within rolling window

### 2) LLM-Assessed Signal

When deterministic conditions pass a gate, an LLM produces:

- `probability_insider` (bounded/validated)
- confidence label (`low|medium|high`)
- short rationale summary

Important: the LLM layer is constrained by strict schema checks and bounded probability adjustment so it cannot arbitrarily override deterministic priors.

## Key Persistence Tables (Conceptual)

- `events`  
  Cached active event metadata for fast selection/listing.

- `whale_spikes`  
  Raw detected move windows with prices and deltas.

- `insider_assessments`  
  Deterministic score output plus optional LLM outputs and trigger payload snapshot.

- `cross_asset_predictions`  
  Consequence alerts tied to high-scoring assessments.

## Dashboard Interpretation Guide

### Live Feed

- Shows newest persisted spikes first.
- Event card combines:
  - latest spike
  - deterministic score band
  - optional LLM assessment
  - market context
  - cross-asset alert rows (if present)

### Daily Top Signals

- UTC-day ranking over persisted signals.
- Includes LLM-assessed and deterministic-only entries (if LLM output is missing).
- Ranking uses available probability signal first, with deterministic score fallback.

## Known Operational Constraints

- Upstream APIs can intermittently return `502/503/504`.
- On free-tier hosting, cold starts/timeouts may increase transient errors.
- Streamlit and dynamic app pages are not ideal as static ingestion sources for tools like NotebookLM.

## Reliability and Performance Practices Used

- Retry/backoff on transient HTTP failures in hot fetch paths.
- Reused HTTP clients on repeated call paths.
- Reduced repeated event-fetch duplication during feature calculation.
- DB read paths optimized (pure selects in hot paths).
- Index coverage for frequent query/sort patterns.
- Conflict-safe dedupe inserts (`ON CONFLICT DO NOTHING`) where identity constraints exist.
- Batch upsert for event cache synchronization.

## How to Run (Local)

1. Start API proxy:
   - `cd server && python -m uvicorn main:app --reload`
2. Run one-shot backfills:
   - `python -m model.event_cache`
   - `python -m model.cross_asset_predictions --min-score 70 --limit 500`
3. Run detector:
   - `python -m model.insider_detection --all-events --max-events 500`
4. Run dashboard:
   - `streamlit run gui/app.py`
5. Run news ingestion loop:
   - `python -m news_scraper.main --loop --interval-seconds 300`

## Environment Variables (Important)

- `POLYMARKET_API_BASE`  
  Base URL for proxy/API calls.

- `DATABASE_URL`  
  Postgres connection string.

- `OLLAMA_API_KEY`, `OLLAMA_HOST`, `OLLAMA_MODEL`  
  LLM explanation and cross-asset prediction settings.

- `NEWS_EVENTS_PATH`, `NEWS_EVENTS_METADATA_PATH`  
  JSONL news dataset paths used for timing/matching logic.

- `POLYMARKET_HTTP_RETRIES`, `POLYMARKET_HTTP_RETRY_DELAY`  
  Retry controls for transient HTTP failures in selected model fetch paths.


It is a structured anomaly-research pipeline over public market/news data.

## Suggested NotebookLM Usage

For best results, upload this file directly (or export to PDF) as a static source.
If needed, add periodic snapshots with:

- top daily signals
- representative trigger payload examples
- detector uptime/error stats
- changes in scoring/version metadata
