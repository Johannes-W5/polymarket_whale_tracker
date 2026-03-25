from __future__ import annotations

from typing import Any, Dict, Optional

import httpx
from fastapi.testclient import TestClient

from pathlib import Path
import sys

# `server/main.py` expects `config.py` importable as top-level module.
server_dir = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(server_dir))
import main as api  # type: ignore[import-not-found]


class _RaisingAsyncClient:
    async def get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        raise httpx.ReadError("boom")


def test_proxy_get_handles_upstream_read_error() -> None:
    dummy = _RaisingAsyncClient()
    with TestClient(api.app) as client:
        api.app.state.http = dummy
        resp = client.get("/events/123")
        assert resp.status_code == 502
        assert resp.json()["detail"] == "Upstream read error"

