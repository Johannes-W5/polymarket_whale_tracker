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

Query parameters are forwarded to Polymarket. Examples:

- **List 5 events:** `GET /events?limit=5`
- **Single event:** `GET /events/{id}`
- **Search:** `GET /public-search?query=trump`
- **Price for token:** `GET /price?token_id=...`
- **User positions:** `GET /positions?user=0x...`

See [Polymarket API docs](https://docs.polymarket.com/developers/gamma-markets-api/overview) for full parameter and response details.
