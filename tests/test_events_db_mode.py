from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Dict

from fastapi.testclient import TestClient

# `server/main.py` expects `config.py` importable as top-level module.
server_dir = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(server_dir))
import main as api  # type: ignore[import-not-found]


def test_events_db_mode_returns_active_events(monkeypatch) -> None:
    ts = datetime(2026, 3, 22, tzinfo=timezone.utc)

    def _fake_get_active_events(*, limit: int = 10000, offset: int = 0):
        return [
            {
                "id": "123",
                "name": "Example Event",
                "description": None,
                "created_at": ts.isoformat(),
                "active": True,
            }
        ]

    # `server/main.py` imports `get_active_events` into a local alias
    # (`get_active_events_from_db`), so patch that alias.
    monkeypatch.setattr("main.get_active_events_from_db", _fake_get_active_events)

    with TestClient(api.app) as client:
        resp = client.get("/events?use_db=1")
        assert resp.status_code == 200
        payload = resp.json()
        assert isinstance(payload, list)
        assert payload[0]["id"] == "123"
        assert payload[0]["active"] is True

