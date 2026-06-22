"""Lender/admin portal Phase 3 — config read surfaces (live test DB)."""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.api import api_router
from app.core.auth import get_current_user
from app.db.base import get_db
from app.models.user import Role, User, UserRoleLink

_BASE = "/api/v1/admin/config"


def _admin():
    return SimpleNamespace(id=uuid.uuid4(), roles=[SimpleNamespace(role=SimpleNamespace(name="admin"))])


def _seed_user(db, email, role_name="staff"):
    role = db.query(Role).filter(Role.name == role_name).first()
    if role is None:
        role = Role(name=role_name, description=f"{role_name} role", is_system=True)
        db.add(role); db.commit(); db.refresh(role)
    user = User(email=email, first_name="Casey", last_name="Ops", is_active=True, is_verified=True)
    db.add(user); db.commit(); db.refresh(user)
    db.add(UserRoleLink(user_id=user.id, role_id=role.id)); db.commit()
    return user


@pytest.fixture
def client(db_session: Session):
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = _admin
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_users_includes_roles(client, db_session):
    _seed_user(db_session, f"ops-{uuid.uuid4().hex[:8]}@example.com", "staff")
    r = client.get(f"{_BASE}/users")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) >= 1
    sample = rows[0]
    assert {"id", "email", "name", "is_active", "roles", "last_login"} <= set(sample)
    assert any("staff" in u["roles"] for u in rows)


def test_list_roles(client, db_session):
    _seed_user(db_session, f"ops-{uuid.uuid4().hex[:8]}@example.com", "admin")
    r = client.get(f"{_BASE}/roles")
    assert r.status_code == 200, r.text
    names = {row["name"] for row in r.json()}
    assert "admin" in names


def test_config_is_admin_only(client, db_session):
    client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=uuid.uuid4(), roles=[SimpleNamespace(role=SimpleNamespace(name="staff"))])
    r = client.get(f"{_BASE}/users")
    assert r.status_code == 403
