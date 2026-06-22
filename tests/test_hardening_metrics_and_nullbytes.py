"""Hardening regressions: NUL-byte rejection + /metrics auth.

NUL bytes: a NUL in a JSON string field used to reach psycopg2/bcrypt and 500. The
middleware now turns the whole class into a clean 422 - for both a raw NUL byte and
the JSON unicode escape (how a fuzzer sending valid JSON delivers it).

/metrics: the Prometheus KPI surface must not be unauthenticated in production.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

# Built in code (not prose) so this source file never itself contains a NUL.
_ESCAPED_NUL_BODY = b'{"email": "a' + b"\\u0000" + b'b@example.com"}'
_RAW_NUL_BODY = b'{"email": "a' + b"\x00" + b'b@example.com"}'


# --- NUL-byte rejection ----------------------------------------------------


def test_escaped_nul_in_json_body_is_422_not_500(client):
    # Valid JSON carrying the \\u0000 escape (decodes to a NUL). Hits the middleware
    # before any handler/DB, so the endpoint doesn't matter - it must be 422, not 500.
    resp = client.post(
        "/api/clinic/v1/dev/seed-clinic",
        content=_ESCAPED_NUL_BODY,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422, resp.text
    assert "NUL" in resp.json()["detail"]


def test_raw_nul_byte_in_json_body_is_422(client):
    resp = client.post(
        "/api/clinic/v1/dev/seed-clinic",
        content=_RAW_NUL_BODY,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422, resp.text


def test_clean_json_body_passes_through(client):
    # A NUL-free request still works end-to-end (middleware replays the body downstream).
    resp = client.post("/api/clinic/v1/dev/seed-clinic", json={"email": "clean@example.com"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == "clean@example.com"


def test_non_json_body_is_not_inspected(client):
    # The middleware only guards application/json, so a non-JSON body with a NUL is
    # not rejected by IT (falls through to normal handling) - never our 422/NUL error.
    resp = client.post(
        "/api/clinic/v1/dev/seed-clinic",
        content=b"\x00rawbytes",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert not (resp.status_code == 422 and "NUL" in resp.text)


# --- /metrics auth ---------------------------------------------------------


def test_metrics_open_in_non_production_without_token(monkeypatch):
    monkeypatch.setattr("app.api.metrics.settings.ENVIRONMENT", "staging", raising=False)
    monkeypatch.setattr("app.api.metrics.settings.METRICS_AUTH_TOKEN", None, raising=False)
    assert TestClient(app).get("/metrics").status_code == 200


def test_metrics_denied_in_production_without_token(monkeypatch):
    monkeypatch.setattr("app.api.metrics.settings.ENVIRONMENT", "production", raising=False)
    monkeypatch.setattr("app.api.metrics.settings.METRICS_AUTH_TOKEN", None, raising=False)
    assert TestClient(app).get("/metrics").status_code == 404


def test_metrics_requires_bearer_token_when_configured(monkeypatch):
    monkeypatch.setattr("app.api.metrics.settings.ENVIRONMENT", "production", raising=False)
    monkeypatch.setattr("app.api.metrics.settings.METRICS_AUTH_TOKEN", "s3cret", raising=False)
    c = TestClient(app)
    assert c.get("/metrics").status_code == 401
    assert c.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = c.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
