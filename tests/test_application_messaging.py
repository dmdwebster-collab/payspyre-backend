"""Application messaging (vendor ⇄ PaySpyre) — integration + unit tests.

DB-backed (live test DB): mounts the v1 api_router + clinic_router, overrides
get_db with the conftest db_session, and overrides the auth deps with principals
carrying REAL user ids (messages/reads FK to users.id, so the acting user must
exist). Exercises the round-trip, per-user unread + mark-read, clinic scoping,
and the thread ("channel") list. Plus pure-logic tests for the email ping's
recipient resolution + inert-by-default behavior (no network).

Run (this file only — the full suite hits a shared remote DB):
    python -m pytest tests/test_application_messaging.py -p no:warnings -q
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.api.clinic.v1.router import clinic_router
from app.api.v1.api import api_router
from app.core.auth import get_current_user
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.clinic_membership import PlatformClinicMembership
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.patient import PlatformPatient
from app.models.user import User

_ADMIN_BASE = "/api/v1/admin"
_CLINIC_BASE = "/api/clinic/v1"


# --- seeding ---------------------------------------------------------------


def _product_id(db: Session):
    p = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )
    assert p is not None, "seed product dental_full_arch_v1 missing"
    return p.id


def _mk_user(db: Session, first: str, last: str) -> User:
    user = User(
        email=f"{first.lower()}-{uuid.uuid4().hex[:8]}@example.com",
        first_name=first,
        last_name=last,
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_vendor(db: Session, name: str) -> Vendor:
    vendor = Vendor(
        business_name=name,
        business_type="corporation",
        contact_name="Owner",
        email=f"clinic-{uuid.uuid4().hex[:8]}@example.com",
        phone="+15555550100",
        address_line1="1 Main St",
        city="Kelowna",
        province="BC",
        postal_code="V1Y0A1",
        status="active",
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def _mk_application(db: Session, vendor_id, patient_first="Jordan") -> PlatformCreditApplication:
    patient = PlatformPatient(
        email=f"pt-{uuid.uuid4().hex[:8]}@example.com",
        legal_first_name=patient_first,
        legal_last_name="Lee",
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)
    application = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=_product_id(db),
        credit_product_version=1,
        requested_amount_cents=1_800_000,
        requested_amount_source="clinic",
        status="under_review",
        vendor_id=vendor_id,
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


def _admin_principal(user: User):
    return SimpleNamespace(
        id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        roles=[SimpleNamespace(role=SimpleNamespace(name="admin"))],
    )


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def app(db_session: Session):
    application = FastAPI()
    application.include_router(api_router, prefix="/api/v1")
    application.include_router(clinic_router, prefix="/api/clinic/v1")
    application.dependency_overrides[get_db] = lambda: db_session
    yield application
    application.dependency_overrides.clear()


def _as_admin(app, user: User):
    app.dependency_overrides[get_current_user] = lambda: _admin_principal(user)


def _as_clinic(app, user: User, vendor_id):
    app.dependency_overrides[get_current_clinic_user] = lambda: ClinicPrincipal(
        user=SimpleNamespace(
            id=user.id, first_name=user.first_name, last_name=user.last_name
        ),
        vendor_id=vendor_id,
    )


# --- round trip ------------------------------------------------------------


class TestRoundTrip:
    def test_vendor_and_admin_talk_back_and_forth(self, app, db_session):
        admin_user = _mk_user(db_session, "Dana", "Admin")
        clinic_user = _mk_user(db_session, "Casey", "Clinic")
        vendor = _mk_vendor(db_session, "Kelowna Dental Centre")
        application = _mk_application(db_session, vendor.id)
        client = TestClient(app)

        # 1) Clinic posts.
        _as_clinic(app, clinic_user, vendor.id)
        r = client.post(
            f"{_CLINIC_BASE}/applications/{application.id}/messages",
            json={"body": "Patient asked about their approval — any update?"},
        )
        assert r.status_code == 201, r.text
        posted = r.json()
        assert posted["sender_kind"] == "vendor"
        assert posted["mine"] is True
        assert posted["sender_name"] == "Casey Clinic"

        # 2) Admin sees it, then replies.
        _as_admin(app, admin_user)
        thread = client.get(f"{_ADMIN_BASE}/applications/{application.id}/messages").json()
        assert [m["sender_kind"] for m in thread] == ["vendor"]
        assert thread[0]["mine"] is False  # admin didn't write the vendor msg

        r = client.post(
            f"{_ADMIN_BASE}/applications/{application.id}/messages",
            json={"body": "Approved — funds release once the e-sign is done."},
        )
        assert r.status_code == 201, r.text
        assert r.json()["sender_kind"] == "admin"
        assert r.json()["mine"] is True

        # 3) Clinic now sees both, in order, with correct mine flags.
        _as_clinic(app, clinic_user, vendor.id)
        thread = client.get(f"{_CLINIC_BASE}/applications/{application.id}/messages").json()
        assert [m["sender_kind"] for m in thread] == ["vendor", "admin"]
        assert [m["mine"] for m in thread] == [True, False]
        assert thread[1]["sender_name"] == "Dana Admin"

    def test_empty_body_rejected(self, app, db_session):
        clinic_user = _mk_user(db_session, "Casey", "Clinic")
        vendor = _mk_vendor(db_session, "KDC")
        application = _mk_application(db_session, vendor.id)
        _as_clinic(app, clinic_user, vendor.id)
        r = TestClient(app).post(
            f"{_CLINIC_BASE}/applications/{application.id}/messages", json={"body": ""}
        )
        assert r.status_code == 422


# --- unread + mark read ----------------------------------------------------


class TestUnread:
    def test_incoming_counts_own_does_not_and_mark_read_clears(self, app, db_session):
        admin_user = _mk_user(db_session, "Dana", "Admin")
        clinic_user = _mk_user(db_session, "Casey", "Clinic")
        vendor = _mk_vendor(db_session, "KDC")
        application = _mk_application(db_session, vendor.id)
        client = TestClient(app)

        # Clinic posts → clinic's own unread stays 0, admin's unread becomes 1.
        _as_clinic(app, clinic_user, vendor.id)
        client.post(
            f"{_CLINIC_BASE}/applications/{application.id}/messages",
            json={"body": "ping"},
        )
        assert client.get(f"{_CLINIC_BASE}/messages/unread-count").json()["unread_count"] == 0

        _as_admin(app, admin_user)
        assert client.get(f"{_ADMIN_BASE}/messages/unread-count").json()["unread_count"] == 1

        # Admin marks the thread read → back to 0.
        assert (
            client.post(f"{_ADMIN_BASE}/applications/{application.id}/messages/read").status_code
            == 200
        )
        assert client.get(f"{_ADMIN_BASE}/messages/unread-count").json()["unread_count"] == 0

        # Admin replies → clinic now has 1 unread.
        client.post(
            f"{_ADMIN_BASE}/applications/{application.id}/messages", json={"body": "pong"}
        )
        _as_clinic(app, clinic_user, vendor.id)
        assert client.get(f"{_CLINIC_BASE}/messages/unread-count").json()["unread_count"] == 1


# --- clinic scoping --------------------------------------------------------


class TestScoping:
    def test_clinic_cannot_touch_another_clinics_thread(self, app, db_session):
        clinic_a = _mk_user(db_session, "Aa", "Clinic")
        vendor_a = _mk_vendor(db_session, "Clinic A")
        vendor_b = _mk_vendor(db_session, "Clinic B")
        app_b = _mk_application(db_session, vendor_b.id)

        # Clinic A authenticated, asking about Clinic B's application.
        _as_clinic(app, clinic_a, vendor_a.id)
        client = TestClient(app)
        assert client.get(f"{_CLINIC_BASE}/applications/{app_b.id}/messages").status_code == 404
        assert (
            client.post(
                f"{_CLINIC_BASE}/applications/{app_b.id}/messages", json={"body": "hi"}
            ).status_code
            == 404
        )

    def test_unread_and_threads_are_vendor_scoped(self, app, db_session):
        admin_user = _mk_user(db_session, "Dana", "Admin")
        clinic_a = _mk_user(db_session, "Aa", "Clinic")
        vendor_a = _mk_vendor(db_session, "Clinic A")
        vendor_b = _mk_vendor(db_session, "Clinic B")
        app_b = _mk_application(db_session, vendor_b.id)
        client = TestClient(app)

        # Admin posts on Clinic B's thread.
        _as_admin(app, admin_user)
        client.post(
            f"{_ADMIN_BASE}/applications/{app_b.id}/messages", json={"body": "for B"}
        )

        # Clinic A sees nothing (different vendor).
        _as_clinic(app, clinic_a, vendor_a.id)
        assert client.get(f"{_CLINIC_BASE}/messages/unread-count").json()["unread_count"] == 0
        assert client.get(f"{_CLINIC_BASE}/messages/threads").json() == []


# --- thread ("channel") list ----------------------------------------------


class TestThreads:
    def test_admin_threads_summary(self, app, db_session):
        admin_user = _mk_user(db_session, "Dana", "Admin")
        clinic_user = _mk_user(db_session, "Casey", "Clinic")
        vendor = _mk_vendor(db_session, "KDC")
        application = _mk_application(db_session, vendor.id, patient_first="Robin")
        client = TestClient(app)

        _as_clinic(app, clinic_user, vendor.id)
        client.post(
            f"{_CLINIC_BASE}/applications/{application.id}/messages",
            json={"body": "first message from the clinic"},
        )

        _as_admin(app, admin_user)
        threads = client.get(f"{_ADMIN_BASE}/messages/threads").json()
        assert len(threads) == 1
        t = threads[0]
        assert t["application_id"] == str(application.id)
        assert t["patient_name"] == "Robin Lee"
        assert t["message_count"] == 1
        assert t["unread_count"] == 1  # the clinic's message is unread for admin
        assert "first message" in t["last_message_preview"]


# --- email ping (pure logic, no network) -----------------------------------


class TestEmailPing:
    def test_inert_when_disabled_and_no_sender(self, monkeypatch):
        from app.core.config import settings
        from app.services import message_notifications as mn

        monkeypatch.setattr(settings, "USE_REAL_NOTIFICATIONS", False)
        # No sender injected + disabled → returns immediately, resolves nothing.
        called = {"resolve": False}
        monkeypatch.setattr(
            mn, "_resolve_recipients", lambda *a, **k: called.__setitem__("resolve", True) or []
        )
        mn.notify_new_message(
            MagicMock(),
            application=SimpleNamespace(id=uuid.uuid4(), vendor_id=uuid.uuid4()),
            message=SimpleNamespace(sender_kind="vendor", body="x"),
            sender_user=SimpleNamespace(first_name="A", last_name="B"),
        )
        assert called["resolve"] is False

    def test_vendor_message_pings_ops_inbox(self, monkeypatch):
        from app.core.config import settings
        from app.services import message_notifications as mn

        monkeypatch.setattr(settings, "PLATFORM_MESSAGES_INBOX", "ops@payspyre.com")
        sent = []
        sender = SimpleNamespace(
            send_message=lambda **kw: sent.append(kw) or SimpleNamespace()
        )
        mn.notify_new_message(
            MagicMock(),
            application=SimpleNamespace(id=uuid.uuid4(), vendor_id=uuid.uuid4()),
            message=SimpleNamespace(sender_kind="vendor", body="clinic asked a thing"),
            sender_user=SimpleNamespace(first_name="Casey", last_name="Clinic"),
            sender=sender,
        )
        assert len(sent) == 1
        assert sent[0]["to_email"] == "ops@payspyre.com"
        assert "Casey Clinic" in sent[0]["subject"]

    def test_admin_message_pings_clinic_staff(self, monkeypatch):
        from app.services import message_notifications as mn

        vendor_id = uuid.uuid4()
        monkeypatch.setattr(
            "app.services.clinic_membership.resolve_clinic_user_emails",
            lambda db, vid: ["staff1@clinic.com", "staff2@clinic.com"],
        )
        sent = []
        sender = SimpleNamespace(send_message=lambda **kw: sent.append(kw))
        mn.notify_new_message(
            MagicMock(),
            application=SimpleNamespace(id=uuid.uuid4(), vendor_id=vendor_id),
            message=SimpleNamespace(sender_kind="admin", body="approved"),
            sender_user=SimpleNamespace(first_name="Dana", last_name="Admin"),
            sender=sender,
        )
        assert {s["to_email"] for s in sent} == {"staff1@clinic.com", "staff2@clinic.com"}

    def test_email_failure_never_raises(self, monkeypatch):
        from app.core.config import settings
        from app.services import message_notifications as mn

        monkeypatch.setattr(settings, "PLATFORM_MESSAGES_INBOX", "ops@payspyre.com")

        def _boom(**kw):
            raise RuntimeError("smtp down")

        mn.notify_new_message(
            MagicMock(),
            application=SimpleNamespace(id=uuid.uuid4(), vendor_id=uuid.uuid4()),
            message=SimpleNamespace(sender_kind="vendor", body="x"),
            sender_user=SimpleNamespace(first_name="Casey", last_name="Clinic"),
            sender=SimpleNamespace(send_message=_boom),
        )  # no exception = pass
