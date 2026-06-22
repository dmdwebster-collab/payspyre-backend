"""Dev-only admin seeder — proves a seeded admin can sign into the cockpit."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.api import api_router
from app.core.config import settings
from app.db.base import get_db

_TOKEN = "test-seed-token-123"
_HDR = {"X-Dev-Seed-Token": _TOKEN}


@pytest.fixture
def client(db_session: Session, monkeypatch):
    monkeypatch.setattr(settings, "DEV_SEED_TOKEN", _TOKEN)
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    # NOTE: get_current_user is NOT overridden — auth runs for real, so this
    # exercises the seeded JWT + RBAC role link end-to-end.
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_seed_admin_then_access_cockpit(client):
    r = client.post("/api/v1/admin/dev/seed-admin", json={}, headers=_HDR)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "admin"
    assert body["email"] and body["password"] and body["jwt"]

    # the seeded admin's JWT opens a role-gated cockpit endpoint (real auth)
    auth = {"Authorization": f"Bearer {body['jwt']}"}
    ok = client.get("/api/v1/admin/config/users", headers=auth)
    assert ok.status_code == 200, ok.text
    assert any(u["email"] == body["email"] and "admin" in u["roles"] for u in ok.json())


def test_seed_admin_login_works(client):
    seeded = client.post("/api/v1/admin/dev/seed-admin",
                         json={"email": "ops-cockpit@example.com", "password": "Cockpit2026!"},
                         headers=_HDR).json()
    # the returned credentials authenticate at the real login endpoint
    login = client.post("/api/v1/auth/login",
                        data={"username": "ops-cockpit@example.com", "password": "Cockpit2026!"},
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert login.status_code == 200, login.text
    assert login.json().get("access_token")
    assert seeded["role"] == "admin"


def test_seed_staff_role(client):
    r = client.post("/api/v1/admin/dev/seed-admin", json={"role": "staff"}, headers=_HDR)
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "staff"


def test_duplicate_email_conflicts(client):
    client.post("/api/v1/admin/dev/seed-admin", json={"email": "dupe-admin@example.com"}, headers=_HDR)
    again = client.post("/api/v1/admin/dev/seed-admin", json={"email": "dupe-admin@example.com"}, headers=_HDR)
    assert again.status_code == 409


def test_bad_role_rejected(client):
    r = client.post("/api/v1/admin/dev/seed-admin", json={"role": "superuser"}, headers=_HDR)
    assert r.status_code == 422


def test_missing_token_forbidden(client):
    r = client.post("/api/v1/admin/dev/seed-admin", json={})  # no X-Dev-Seed-Token
    assert r.status_code == 403


def test_wrong_token_forbidden(client):
    r = client.post("/api/v1/admin/dev/seed-admin", json={}, headers={"X-Dev-Seed-Token": "nope"})
    assert r.status_code == 403


def test_disabled_when_no_token_configured(client, monkeypatch):
    # even with a header, an UNSET DEV_SEED_TOKEN keeps the seeder inert
    monkeypatch.setattr(settings, "DEV_SEED_TOKEN", "")
    r = client.post("/api/v1/admin/dev/seed-admin", json={}, headers=_HDR)
    assert r.status_code == 403
