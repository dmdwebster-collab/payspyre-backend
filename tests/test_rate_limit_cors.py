"""A rate-limited (429) response must still carry CORS headers.

The endpoint rate limiter runs OUTSIDE CORSMiddleware in the stack, so its
early 429 short-circuits before CORS can attach its headers. Without that, a
cross-origin browser client sees an opaque "Failed to fetch" instead of the
429. These tests are DB-free: the 429 short-circuits at the outermost
middleware, so no route handler or DB session is ever touched.
"""
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main


def _trip_rate_limit(monkeypatch):
    def _tripped(request, endpoint_type):
        raise HTTPException(status_code=429, detail="IP rate limit exceeded: test")

    monkeypatch.setattr(main.settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(main, "check_endpoint_rate_limit", _tripped)


# Note: TestClient is used WITHOUT a ``with`` block on purpose — that skips the
# app's startup/shutdown events (which probe the DB), keeping these tests fully
# DB-free. The 429 short-circuits at the outermost middleware regardless.


def test_429_carries_cors_header_for_allowed_origin(monkeypatch):
    _trip_rate_limit(monkeypatch)
    allowed_origin = main.settings.cors_origins_list[0]

    c = TestClient(main.app)
    r = c.get("/api/applicant/v1/products", headers={"Origin": allowed_origin})

    assert r.status_code == 429, f"expected 429, got {r.status_code}: {r.text}"
    # The browser only surfaces the 429 if the allow-origin header is present.
    assert r.headers.get("Access-Control-Allow-Origin") == allowed_origin
    assert r.headers.get("Access-Control-Allow-Credentials") == "true"
    assert "Origin" in r.headers.get("Vary", "")
    assert "Retry-After" in r.headers


def test_429_omits_cors_header_for_disallowed_origin(monkeypatch):
    _trip_rate_limit(monkeypatch)

    c = TestClient(main.app)
    r = c.get(
        "/api/applicant/v1/products",
        headers={"Origin": "https://evil.example.com"},
    )

    assert r.status_code == 429
    # Policy is unchanged: a non-allowlisted origin gets no allow-origin header.
    assert "Access-Control-Allow-Origin" not in r.headers


def test_429_no_origin_header_has_no_cors_header(monkeypatch):
    _trip_rate_limit(monkeypatch)

    c = TestClient(main.app)
    r = c.get("/api/applicant/v1/products")

    assert r.status_code == 429
    assert "Access-Control-Allow-Origin" not in r.headers
