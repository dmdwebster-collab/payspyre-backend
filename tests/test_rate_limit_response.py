"""A tripped rate limit must surface as a clean 429, not a 500.

The rate-limit check runs inside an HTTP middleware and raises HTTPException;
an HTTPException raised in middleware is NOT handled by FastAPI's exception
handlers, so without explicit conversion it leaks to the client as a 500. This
guards the conversion to a proper 429 response (with Retry-After).
"""
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main


def test_rate_limited_request_returns_429_not_500(client: TestClient, monkeypatch):
    def _tripped(request, endpoint_type):
        raise HTTPException(status_code=429, detail="IP rate limit exceeded: test")

    monkeypatch.setattr(main.settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(main, "check_endpoint_rate_limit", _tripped)

    r = client.get("/api/applicant/v1/products")
    assert r.status_code == 429, f"expected clean 429, got {r.status_code}: {r.text}"
    assert r.json()["detail"] == "IP rate limit exceeded: test"
    assert "Retry-After" in r.headers
