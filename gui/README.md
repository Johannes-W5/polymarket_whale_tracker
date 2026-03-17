# Streamlit Dashboard

Simple local GUI for the Polymarket whale tracker.

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
- Ollama assessment requires `OLLAMA_API_KEY` and uses the existing `OLLAMA_HOST` / `OLLAMA_MODEL` environment variables.
- If the database does not provide an event list, you can enter an event ID manually in the sidebar.
