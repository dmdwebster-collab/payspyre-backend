"""DB-free unit tests for http_client.request_with_retry (bounded retry/backoff).

No live HTTP and no real sleeping: we monkeypatch ``http_client.request`` with an
in-memory fake that yields a scripted sequence of responses/exceptions, and inject
a no-op ``sleep`` so backoff doesn't slow the suite.
"""
import httpx
import pytest

from app.core import http_client


def _resp(status: int) -> httpx.Response:
    return httpx.Response(status, json={"ok": status})


class _FakeRequest:
    """Records calls and returns/raises the next scripted item per invocation."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _call(monkeypatch, script, **kw):
    fake = _FakeRequest(script)
    monkeypatch.setattr(http_client, "request", fake)
    sleeps: list[float] = []
    resp = None
    exc = None
    try:
        resp = http_client.request_with_retry(
            "GET",
            "https://vendor.example/v3/session/abc/",
            provider="didit",
            op="retrieve_session",
            sleep=sleeps.append,
            **kw,
        )
    except httpx.HTTPError as e:  # surfaced after the cap
        exc = e
    return fake, resp, exc, sleeps


class TestRetriesThenSucceeds:
    def test_5xx_then_200_retries_and_returns_ok(self, monkeypatch):
        fake, resp, exc, sleeps = _call(monkeypatch, [_resp(503), _resp(200)])
        assert exc is None
        assert resp.status_code == 200
        assert fake.calls == 2          # one retry happened
        assert sleeps == [0.2]          # backed off once before the retry

    def test_timeout_then_200_retries_and_returns_ok(self, monkeypatch):
        fake, resp, exc, sleeps = _call(
            monkeypatch, [httpx.ConnectTimeout("boom"), _resp(200)]
        )
        assert exc is None
        assert resp.status_code == 200
        assert fake.calls == 2


class TestGivesUpAfterCap:
    def test_all_5xx_returns_last_5xx_after_max_attempts(self, monkeypatch):
        fake, resp, exc, sleeps = _call(
            monkeypatch, [_resp(500), _resp(500), _resp(500)]
        )
        assert exc is None
        assert resp.status_code == 500
        assert fake.calls == 3          # capped at the default 3 attempts
        assert sleeps == [0.2, 0.4]     # two backoffs, none after the final try

    def test_all_timeouts_raises_after_max_attempts(self, monkeypatch):
        fake, resp, exc, sleeps = _call(
            monkeypatch,
            [httpx.ConnectTimeout("a"), httpx.ConnectTimeout("b"),
             httpx.ConnectTimeout("c")],
        )
        assert isinstance(exc, httpx.ConnectTimeout)
        assert fake.calls == 3

    def test_respects_custom_max_attempts(self, monkeypatch):
        fake, resp, exc, sleeps = _call(
            monkeypatch, [_resp(500), _resp(500)], max_attempts=2
        )
        assert resp.status_code == 500
        assert fake.calls == 2


class TestNoRetryOn4xx:
    def test_4xx_returned_immediately_without_retry(self, monkeypatch):
        fake, resp, exc, sleeps = _call(monkeypatch, [_resp(404), _resp(200)])
        assert resp.status_code == 404  # 4xx won't get better — no retry
        assert fake.calls == 1
        assert sleeps == []

    def test_2xx_returned_immediately(self, monkeypatch):
        fake, resp, exc, sleeps = _call(monkeypatch, [_resp(200)])
        assert resp.status_code == 200
        assert fake.calls == 1
        assert sleeps == []
