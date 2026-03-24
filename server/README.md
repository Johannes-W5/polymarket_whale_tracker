# Polymarket REST API

Python REST API that proxies to Polymarket's public APIs. **No API key, authentication, or wallet required.**

## Setup

```bash
cd server
pip install -r requirements.txt
```

## Run

From the `server` folder:

```bash
python -m uvicorn main:app --reload
```

On Windows you can instead run `run.bat`. If `uvicorn` is on your PATH you can use `uvicorn main:app --reload`.

Server runs at **http://127.0.0.1:8000**. Interactive docs: **http://127.0.0.1:8000/docs**.

## Endpoints

| Group | Endpoints |
|-------|-----------|
| **Gamma** (events & markets) | `GET /events`, `GET /events/{id}`, `GET /markets`, `GET /markets/{id}`, `GET /public-search`, `GET /tags`, `GET /series`, `GET /sports`, `GET /teams` |
| **CLOB** (prices & orderbook) | `GET /price`, `GET /prices`, `GET /book`, `POST /books`, `GET /prices-history`, `GET /midpoint`, `GET /spread` |
| **Data** (positions & analytics) | `GET /positions`, `GET /closed-positions`, `GET /activity`, `GET /value`, `GET /oi`, `GET /holders`, `GET /trades` |

Query parameters are forwarded to Polymarket for most routes. Examples:

- **Single event:** `GET /events/{id}`
- **Search:** `GET /public-search?query=trump`
- **Price for token:** `GET /price?token_id=...`
- **User positions:** `GET /positions?user=0x...`

See [Polymarket API docs](https://docs.polymarket.com/developers/gamma-markets-api/overview) for full parameter and response details.

### `GET /events` (Gamma passthrough)

The proxy forwards list requests to Gamma’s `GET /events`. That upstream endpoint’s **default** response (no query string) is often a long list of **old, closed** events, which is misleading if you expect “what’s live on Polymarket now.”

**This server is not backed by `event_cache` or Postgres for `/events`** — it is a direct Gamma proxy. The [`model/event_cache`](../model/event_cache.py) job is a **consumer** of the same API: it calls `/events` with explicit filters and stores results in the database.

**Bare `GET /events`** on this proxy applies the same defaults as `event_cache` when you omit filters:

- `active=true`
- `closed=false`
- `limit=500`

If you pass `active`, `closed`, or `limit` yourself, those values are sent to Gamma as-is (only missing keys get defaults).

**Debug / raw Gamma behavior:** append `raw=1` (or `raw=true`) to skip default injection and forward the remaining query params only. The `raw` parameter is **not** sent to Gamma.

**Recommended** for “live” listings (explicit, matches cache semantics):  
`GET /events?active=true&closed=false&limit=500` (adjust `offset` / `limit` for pagination).

Other examples:

- **List 5 events (still active, not closed by default):** `GET /events?limit=5`
