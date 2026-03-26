from __future__ import annotations

import httpx

from model.event_prices import get_event_prices


class _DummyResponse:
    def __init__(self, status_code: int, payload: dict, *, url: str):
        self.status_code = status_code
        self._payload = payload
        self.request = httpx.Request("GET", url)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=self.request,
                response=self,
            )


class _DummyClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, params=None):
        self.calls += 1
        if not self._responses:
            raise AssertionError("No dummy response left")
        status_code, payload = self._responses.pop(0)
        return _DummyResponse(status_code, payload, url=url)


def test_get_event_prices_retries_transient_502_then_succeeds(monkeypatch) -> None:
    event_payload = {
        "markets": [
            {"id": "m1", "closed": False, "clobTokenIds": '["y","n"]', "title": "M1"}
        ]
    }
    prices_payload = {"y": {"BUY": 0.55}, "n": {"BUY": 0.45}}
    dummy = _DummyClient(
        [
            (502, {"detail": "upstream error"}),
            (200, event_payload),
            (200, prices_payload),
        ]
    )
    monkeypatch.setattr("model.event_prices.httpx.Client", lambda timeout: dummy)

    prices = get_event_prices("123", base_url="http://127.0.0.1:8000")
    assert prices.yes_price == 0.55
    assert prices.no_price == 0.45
    assert dummy.calls == 3

