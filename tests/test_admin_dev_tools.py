"""Dev-only admin seeder — proves a seeded admin can sign into the cockpit."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.api import api_router
from app.db.base import get_db


@pytest.fixture
def client(db_session: Session):
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    # NOTE: get_current_user is NOT overridden — auth runs for real, so this
    # exercises the seeded JWT + RBAC role link end-to-end.
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_seed_admin_then_access_cockpit(client):
    r = client.post("/api/v1/admin/dev/seed-admin", json={})
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
                         json={"email": "ops-cockpit@example.com", "password": "Cockpit2026!"}).json()
    # the returned credentials authenticate at the real login endpoint
    login = client.post("/api/v1/auth/login",
                        data={"username": "ops-cockpit@example.com", "password": "Cockpit2026!"},
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert login.status_code == 200, login.text
    assert login.json().get("access_token")
    assert seeded["role"] == "admin"


def test_seed_staff_role(client):
    r = client.post("/api/v1/admin/dev/seed-admin", json={"role": "staff"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "staff"


def test_duplicate_email_conflicts(client):
    client.post("/api/v1/admin/dev/seed-admin", json={"email": "dupe-admin@example.com"})
    again = client.post("/api/v1/admin/dev/seed-admin", json={"email": "dupe-admin@example.com"})
    assert again.status_code == 409


def test_bad_role_rejected(client):
    r = client.post("/api/v1/admin/dev/seed-admin", json={"role": "superuser"})
    assert r.status_code == 422
