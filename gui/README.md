# Streamlit Dashboard

Simple local GUI for the Polymarket whale tracker.

This app is a research analytics tool built on public market and news data. It
surfaces suspicious activity, anomaly scores, and LLM-generated explanations of
deterministic evidence. It is not a trading tool and does not make legal
determinations.

## Install

From the repository root:

```bash
pip install -r server/requirements.txt
pip install -r gui/requirements.txt
```

## Run

Start the FastAPI proxy first:

```bash
cd server
python -m uvicorn main:app --reload
```

Then, from the repository root, start the dashboard:

```bash
streamlit run gui/app.py
```

## Notes

- Default API base URL is `http://127.0.0.1:8000`.
- The dashboard can still load with partial data if PostgreSQL spike history is unavailable.
- The explanation layer requires `OLLAMA_API_KEY` and uses the existing `OLLAMA_HOST` / `OLLAMA_MODEL` environment variables.
- The LLM is bounded to explanation, confidence refinement, and a small probability adjustment around deterministic evidence.
- News ingestion writes `news_events.metadata.json` alongside `news_events.jsonl` so point-in-time evaluation can reference dataset metadata.
- If the database does not provide an event list, you can enter an event ID manually in the sidebar.
