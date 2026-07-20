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


# ---------------------------------------------------------------------------
# Notification rules (Dave-template integration)
# ---------------------------------------------------------------------------


def test_notification_rules_lists_full_catalog(client, db_session):
    """Every registry type appears with resolved config — zero seeding."""
    from app.services.notification_render import NOTIFICATION_TYPES

    r = client.get(f"{_BASE}/notification-rules")
    assert r.status_code == 200, r.text
    rows = {row["notification_type"]: row for row in r.json()}
    assert set(rows) == set(NOTIFICATION_TYPES)
    # dunning types resolve Dave's cadence from the typed fallback
    assert rows["payment_due_reminder"]["offsets"] == [7, 2]
    assert rows["payment_overdue"]["offsets"] == [7, 14, 21, 30, 60, 90]
    # a lifecycle type defaults to email-only, enabled, no offsets
    row = rows["loan_activated"]
    assert row["email_enabled"] and not row["sms_enabled"] and row["offsets"] == []
    assert row["customized"] is False
    assert rows["loan_activated"]["has_sms_template"] is True
    assert rows["loan_repaid"]["has_sms_template"] is False


def test_notification_rule_upsert_and_roundtrip(client, db_session):
    # first edit materializes a row seeded from current effective config
    r = client.put(
        f"{_BASE}/notification-rules/payment_overdue",
        json={"offsets": [5, 10, 30, 60, 90], "sms_enabled": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["offsets"] == [5, 10, 30, 60, 90]
    assert body["sms_enabled"] is True and body["email_enabled"] is True
    assert body["customized"] is True

    # partial update keeps other fields
    r2 = client.put(
        f"{_BASE}/notification-rules/payment_overdue", json={"enabled": False}
    )
    assert r2.json()["offsets"] == [5, 10, 30, 60, 90]
    assert r2.json()["enabled"] is False

    # and the resolver honours the row
    from app.services.notification_config import get_rule

    rule = get_rule(db_session, "payment_overdue")
    assert rule.offsets == (5, 10, 30, 60, 90) and not rule.enabled


def test_notification_rule_unknown_type_404(client, db_session):
    r = client.put(f"{_BASE}/notification-rules/nope", json={"enabled": False})
    assert r.status_code == 404


def test_notification_rule_rejects_bad_jinja_override(client, db_session):
    r = client.put(
        f"{_BASE}/notification-rules/loan_activated",
        json={"content_overrides": {"email": {"subject": "{% if %}"}}},
    )
    assert r.status_code == 422
    assert "invalid Jinja" in r.json()["detail"]


def test_notification_rule_valid_override_renders_with_globals(client, db_session):
    r = client.put(
        f"{_BASE}/notification-rules/loan_activated",
        json={"content_overrides": {"email": {"subject": "{{ company_name }}: loan {{ loan_id }} live"}}},
    )
    assert r.status_code == 200, r.text
    from app.services.notification_render import render_string

    out = render_string(
        r.json()["content_overrides"]["email"]["subject"], {"loan_id": "ab12"}
    )
    assert out == "PaySpyre Financial Inc.: loan ab12 live"
