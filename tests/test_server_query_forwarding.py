from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Dict, Optional

from fastapi.testclient import TestClient

# `server/main.py` expects `config.py` to be importable as a top-level module,
# which is true when running with `cd server && ...`.
server_dir = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(server_dir))

import main as api  # type: ignore[import-not-found]


class _DummyResponse:
    def __init__(self, *, status_code: int = 200, payload: Optional[Dict[str, Any]] = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> Dict[str, Any]:
        return dict(self._payload)


class _DummyAsyncClient:
    def __init__(self) -> None:
        self.last: Dict[str, Any] = {}

    async def get(self, url: str, params: Optional[Dict[str, Any]] = None) -> _DummyResponse:
        self.last = {"url": url, "params": params}
        return _DummyResponse(status_code=200, payload={"detail": "ok"})


def test_query_params_preserve_repeated_keys_for_oi() -> None:
    """
    Regression: `/oi?market=a&market=b` must forward both `market` values to the upstream.
    """
    dummy = _DummyAsyncClient()
    with TestClient(api.app) as client:
        api.app.state.http = dummy
        resp = client.get("/oi?market=m1&market=m2")
        assert resp.status_code == 200
        assert dummy.last["params"] == {"market": ["m1", "m2"]}

