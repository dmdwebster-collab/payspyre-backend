"""Endpoint tests for the borrower in-app (dashboard) notification read API.

Seeds ``dashboard_notification`` platform_events directly (matching the shape
``NotificationProcessor._record_dashboard`` writes), then exercises list /
unread-count / mark-read and the per-patient scope. Auth + DB are dep-overridden
exactly as in test_loans_api.py.
"""
import itertools
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import ApplicantClaims, get_current_applicant
from app.db.base import get_db
from app.main import app
from app.models.platform.event import PlatformEvent
from app.models.platform.patient import PlatformPatient

_BASE = "/api/applicant/v1/notifications"


_phone_seq = itertools.count(1000)


def _patient(db: Session, email: str) -> PlatformPatient:
    # Unique phone per patient — phone_e164 is unique-constrained, and some tests
    # seed two patients (scope checks).
    p = PlatformPatient(
        email=email, phone_e164=f"+1555555{next(_phone_seq):04d}", legal_first_name="Bo"
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _seed_dashboard(
    db: Session,
    patient_id,
    *,
    subject: str,
    body: str,
    notification_type: str = "application_approved",
    loan_id: str | None = None,
) -> PlatformEvent:
    """Insert a dashboard_notification event matching _record_dashboard's shape."""
    ev = PlatformEvent(
        event_type="dashboard_notification",
        actor="system",
        patient_id=patient_id,
        application_id=None,
        payload={
            "v": 1,
            "actor": {"type": "system", "id": "system"},
            "application_id": None,
            "patient_id": str(patient_id),
            "channel": "dashboard",
            "notification_type": notification_type,
            "source_event_id": 1,
            "loan_id": loan_id,
            "subject": subject,
            "body": body,
            "read": False,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


@pytest.fixture
def tc():
    return TestClient(app)


def _auth_as(patient_id, app_ids=None):
    app.dependency_overrides[get_current_applicant] = lambda: ApplicantClaims(
        patient_id=patient_id, app_ids=app_ids or []
    )


def _teardown():
    app.dependency_overrides.pop(get_current_applicant, None)
    app.dependency_overrides.pop(get_db, None)


class TestList:
    def test_list_newest_first_with_read_flags(self, tc, db_session):
        p = _patient(db_session, "list@example.com")
        e1 = _seed_dashboard(db_session, p.id, subject="First", body="b1", loan_id="loan-1")
        e2 = _seed_dashboard(db_session, p.id, subject="Second", body="b2")
        app.dependency_overrides[get_db] = lambda: db_session
        _auth_as(p.id)
        try:
            r = tc.get(_BASE)
            assert r.status_code == 200
            body = r.json()
            assert [n["id"] for n in body] == [e2.id, e1.id]  # newest first
            first = body[1]
            assert first["subject"] == "First"
            assert first["body"] == "b1"
            assert first["notification_type"] == "application_approved"
            assert first["loan_id"] == "loan-1"
            assert first["created_at"] is not None
            assert all(n["read"] is False for n in body)
            assert body[0]["loan_id"] is None  # absent loan_id serializes to None
        finally:
            _teardown()

    def test_unread_only_filter(self, tc, db_session):
        p = _patient(db_session, "unreadonly@example.com")
        e1 = _seed_dashboard(db_session, p.id, subject="A", body="a")
        _e2 = _seed_dashboard(db_session, p.id, subject="B", body="b")
        app.dependency_overrides[get_db] = lambda: db_session
        _auth_as(p.id)
        try:
            assert tc.post(f"{_BASE}/{e1.id}/read").status_code == 200
            r = tc.get(_BASE, params={"unread_only": "true"})
            assert r.status_code == 200
            ids = [n["id"] for n in r.json()]
            assert e1.id not in ids
            assert len(ids) == 1
        finally:
            _teardown()


class TestUnreadCount:
    def test_count_reflects_reads(self, tc, db_session):
        p = _patient(db_session, "count@example.com")
        e1 = _seed_dashboard(db_session, p.id, subject="A", body="a")
        _e2 = _seed_dashboard(db_session, p.id, subject="B", body="b")
        app.dependency_overrides[get_db] = lambda: db_session
        _auth_as(p.id)
        try:
            assert tc.get(f"{_BASE}/unread-count").json()["unread_count"] == 2
            tc.post(f"{_BASE}/{e1.id}/read")
            assert tc.get(f"{_BASE}/unread-count").json()["unread_count"] == 1
        finally:
            _teardown()


class TestMarkRead:
    def test_mark_read_flips_flag_and_count(self, tc, db_session):
        p = _patient(db_session, "mark@example.com")
        e1 = _seed_dashboard(db_session, p.id, subject="A", body="a")
        app.dependency_overrides[get_db] = lambda: db_session
        _auth_as(p.id)
        try:
            r = tc.post(f"{_BASE}/{e1.id}/read")
            assert r.status_code == 200
            assert r.json() == {"id": e1.id, "read": True}

            listed = tc.get(_BASE).json()
            assert listed[0]["read"] is True
            assert tc.get(f"{_BASE}/unread-count").json()["unread_count"] == 0
        finally:
            _teardown()

    def test_mark_read_is_idempotent(self, tc, db_session):
        p = _patient(db_session, "idem@example.com")
        e1 = _seed_dashboard(db_session, p.id, subject="A", body="a")
        app.dependency_overrides[get_db] = lambda: db_session
        _auth_as(p.id)
        try:
            assert tc.post(f"{_BASE}/{e1.id}/read").status_code == 200
            assert tc.post(f"{_BASE}/{e1.id}/read").status_code == 200
            # Exactly one read receipt was written despite two calls.
            n = (
                db_session.query(PlatformEvent)
                .filter(PlatformEvent.event_type == "dashboard_notification_read")
                .filter(PlatformEvent.payload["read_event_id"].astext == str(e1.id))
                .count()
            )
            assert n == 1
            assert tc.get(f"{_BASE}/unread-count").json()["unread_count"] == 0
        finally:
            _teardown()

    def test_mark_read_unknown_event_404(self, tc, db_session):
        p = _patient(db_session, "missing@example.com")
        app.dependency_overrides[get_db] = lambda: db_session
        _auth_as(p.id)
        try:
            assert tc.post(f"{_BASE}/99999999/read").status_code == 404
        finally:
            _teardown()


class TestScope:
    def test_other_patient_notifications_not_visible(self, tc, db_session):
        owner = _patient(db_session, "owner@example.com")
        other = _patient(db_session, "other@example.com")
        owned = _seed_dashboard(db_session, owner.id, subject="Mine", body="m")
        _seed_dashboard(db_session, other.id, subject="Theirs", body="t")
        app.dependency_overrides[get_db] = lambda: db_session
        _auth_as(other.id)  # log in as the OTHER patient
        try:
            listed = tc.get(_BASE).json()
            ids = [n["id"] for n in listed]
            assert owned.id not in ids
            assert len(ids) == 1  # only their own
            assert tc.get(f"{_BASE}/unread-count").json()["unread_count"] == 1
            # Cannot mark the owner's notification read (404, no leak).
            assert tc.post(f"{_BASE}/{owned.id}/read").status_code == 404
        finally:
            _teardown()

    def test_owner_unaffected_by_other_read_receipt(self, tc, db_session):
        owner = _patient(db_session, "owner2@example.com")
        owned = _seed_dashboard(db_session, owner.id, subject="Mine", body="m")
        app.dependency_overrides[get_db] = lambda: db_session
        _auth_as(owner.id)
        try:
            assert tc.get(f"{_BASE}/unread-count").json()["unread_count"] == 1
            assert tc.get(_BASE).json()[0]["read"] is False
        finally:
            _teardown()
