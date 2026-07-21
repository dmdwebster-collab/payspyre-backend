"""WS-A communications hub — DB-FREE unit tests.

Covers the append-only communications log (Dave mandate #4: full message
bodies, per account, forever) WITHOUT touching a database — the shared test DB
is not yet migrated to 055 and the fan-out convention keeps agent test runs
DB-free. What is pinned here:

* migration 055 chain (down_revision = 054_hardship) + the append-only
  UPDATE/DELETE trigger in its source,
* ``communications_log.record_communication`` field mapping + validation,
* the dispatcher hooks: mock + real ``send_notification`` and the magic-link
  paths append a log row with the FULL rendered body (magic-link body hidden),
* the processor's dashboard lane appends a log row,
* the admin router surface: routes mounted, NO update/delete routes,
  templates / preview / offline endpoints end-to-end with fake sessions.

Run JUST this file:
    source .venv/bin/activate && python -m pytest tests/test_communications_log.py -x -q
"""
from __future__ import annotations

import importlib.util
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.platform.communication import (
    CHANNELS,
    PlatformCommunicationLog,
)
from app.services.communications_log import HIDDEN_BODY, record_communication
from app.services.mock_notification_dispatcher import MockNotificationDispatcher

_MIGRATION = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "055_communications_log.py"
)


# ---------------------------------------------------------------------------
# Fakes (no database)
# ---------------------------------------------------------------------------


class FakeQuery:
    """Chainable stand-in for Session.query(...) that returns canned rows."""

    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def offset(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """Captures added objects; canned responses for query()/get()."""

    def __init__(self, query_rows=None, get_map=None):
        self.added = []
        self.committed = 0
        self._query_rows = query_rows or {}
        self._get_map = get_map or {}

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass

    def refresh(self, obj):
        pass

    def query(self, model):
        rows = self._query_rows.get(getattr(model, "__name__", str(model)), [])
        return FakeQuery(rows)

    def get(self, model, key):
        return self._get_map.get((getattr(model, "__name__", str(model)), key))


def _comms_rows(session: FakeSession) -> list[PlatformCommunicationLog]:
    return [o for o in session.added if isinstance(o, PlatformCommunicationLog)]


def _payment_ctx() -> dict:
    """Full merge context for payment_due_reminder (mirrors the dunning producer)."""
    return dict(
        borrower_name="Jordan",
        loan_id="L-123",
        payment_amount="$250.00",
        due_date="2026-07-01",
        payment_method="Pre-authorized debit",
        payment_url="https://app.payspyre.com/pay/abc",
        late_fee="$25.00",
        account_url="https://app.payspyre.com/account",
        days_until_due=3,
    )


_ADMIN_USER = SimpleNamespace(
    id=uuid.uuid4(),
    email="ops@payspyre.test",
    roles=[SimpleNamespace(role=SimpleNamespace(name="admin"))],
)


# ---------------------------------------------------------------------------
# Migration chain pin (merge-train convention)
# ---------------------------------------------------------------------------


def test_migration_chain():
    """Pin 055_communications_log onto the current single head 054_hardship.
    If a parallel wave-1 PR wins the chain race, the orchestrator re-chains —
    update BOTH the migration's down_revision and this pin together."""
    spec = importlib.util.spec_from_file_location("migration_055", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "055_communications_log"
    assert mod.down_revision == "054_hardship"


def test_migration_installs_append_only_trigger():
    """The table must be append-only at the DB level: the migration installs a
    trigger firing BEFORE UPDATE OR DELETE that raises."""
    src = _MIGRATION.read_text()
    assert "BEFORE UPDATE OR DELETE ON platform_communications_log" in src
    assert "RAISE EXCEPTION" in src


# ---------------------------------------------------------------------------
# Model + service
# ---------------------------------------------------------------------------


def test_model_shape():
    cols = {c.name for c in PlatformCommunicationLog.__table__.columns}
    assert {
        "id", "channel", "direction", "recipient", "subject", "body",
        "notification_type", "patient_id", "application_id", "loan_id",
        "sent_by", "sent_by_user_id", "comment", "contact_method", "purpose",
        "result", "status", "vendor", "vendor_message_id", "source_event_id",
        "occurred_at", "created_at",
    } <= cols
    assert PlatformCommunicationLog.__table__.name == "platform_communications_log"
    assert set(CHANNELS) == {"email", "sms", "dashboard", "offline"}


def test_record_communication_maps_fields_and_flushes():
    db = FakeSession()
    app_id = uuid.uuid4()
    row = record_communication(
        db,
        channel="email",
        recipient="jordan@example.com",
        subject="PaySpyre - Payment Reminder",
        body="<html>full body</html>",
        notification_type="payment_due_reminder",
        application_id=app_id,
        loan_id=str(uuid.uuid4()),  # pipeline carries loan ids as strings
        sent_by="system",
        status="sent",
        vendor="mock",
        source_event_id=42,
    )
    assert _comms_rows(db) == [row]
    assert row.id is not None and row.occurred_at is not None and row.created_at is not None
    assert row.body == "<html>full body</html>"
    assert row.application_id == app_id
    assert isinstance(row.loan_id, uuid.UUID)  # coerced from str
    assert row.source_event_id == 42


def test_record_communication_rejects_unknown_channel():
    with pytest.raises(ValueError, match="unknown communications channel"):
        record_communication(FakeSession(), channel="fax", body="x")


def test_record_communication_honors_supplied_occurred_at():
    when = datetime(2026, 7, 1, 15, 30, tzinfo=timezone.utc)
    row = record_communication(
        FakeSession(), channel="offline", body="called borrower", occurred_at=when
    )
    assert row.occurred_at == when


# ---------------------------------------------------------------------------
# Dispatcher hooks — every dispatched notification lands in the log
# ---------------------------------------------------------------------------


def _patient_stub():
    return SimpleNamespace(
        email="jordan@example.com", phone_e164="+15555550123", legal_first_name="Jordan"
    )


def test_mock_dispatcher_send_notification_logs_full_body():
    db = FakeSession(query_rows={"PlatformPatient": [_patient_stub()]})
    staff_id = uuid.uuid4()
    MockNotificationDispatcher(db).send_notification(
        patient_id=uuid.uuid4(),
        notification_type="payment_due_reminder",
        contact_method="email",
        context=_payment_ctx(),
        sent_by="ops@payspyre.test",
        sent_by_user_id=staff_id,
        comment="courtesy resend",
    )
    rows = _comms_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.channel == "email"
    assert row.recipient == "jordan@example.com"
    assert row.notification_type == "payment_due_reminder"
    assert "Jordan" in row.body and len(row.body) > 200  # FULL rendered html
    assert "Payment Reminder" in row.subject
    assert row.sent_by == "ops@payspyre.test"
    assert row.sent_by_user_id == staff_id
    assert row.comment == "courtesy resend"


def test_mock_dispatcher_sms_channel_logs_sms_body():
    db = FakeSession(query_rows={"PlatformPatient": [_patient_stub()]})
    MockNotificationDispatcher(db).send_notification(
        patient_id=uuid.uuid4(),
        notification_type="payment_due_reminder",
        contact_method="sms",
        context=_payment_ctx(),
    )
    (row,) = _comms_rows(db)
    assert row.channel == "sms"
    assert row.recipient == "+15555550123"
    assert "$250.00" in row.body and "2026-07-01" in row.body
    assert row.sent_by == "system"


def test_mock_dispatcher_magic_link_body_hidden():
    """Credential-bearing bodies are stored hidden — the on-camera TL behavior."""
    db = FakeSession(query_rows={"PlatformPatient": [_patient_stub()]})
    MockNotificationDispatcher(db).send_magic_link(
        patient_id=uuid.uuid4(),
        application_id=uuid.uuid4(),
        contact_method="email",
        token="123456",
    )
    (row,) = _comms_rows(db)
    assert row.body == HIDDEN_BODY
    assert "123456" not in (row.body + (row.subject or ""))
    assert row.notification_type == "magic_link"


def test_real_dispatcher_send_notification_logs_full_body(monkeypatch):
    from app.core.config import settings
    from app.services.real_notification_dispatcher import (
        RealNotificationDispatcher,
        SendOutcome,
    )

    monkeypatch.setattr(settings, "USE_SUPPRESSION_CHECK", False)
    db = FakeSession(query_rows={"PlatformPatient": [_patient_stub()]})
    dispatcher = RealNotificationDispatcher(db)

    sent = {}

    class StubSender:
        def send_message(self, **kwargs):
            sent.update(kwargs)
            return SendOutcome(vendor="resend", vendor_message_id="msg_1", status="sent")

    with patch.object(dispatcher, "_build_sender", return_value=StubSender()):
        dispatcher.send_notification(
            patient_id=uuid.uuid4(),
            notification_type="payment_due_reminder",
            contact_method="email",
            context=_payment_ctx(),
            sent_by="ops@payspyre.test",
        )
    (row,) = _comms_rows(db)
    # The logged body is EXACTLY what the vendor was asked to send.
    assert row.body == sent["html"]
    assert row.subject == sent["subject"]
    assert row.recipient == "jordan@example.com"
    assert row.vendor == "resend" and row.vendor_message_id == "msg_1"
    assert row.status == "sent"
    assert row.sent_by == "ops@payspyre.test"


def test_processor_dashboard_lane_logs_card_content():
    from app.services.notification_processor import NotificationPlan, NotificationProcessor

    db = FakeSession()  # no PlatformNotificationRule rows -> registry defaults
    processor = NotificationProcessor(db, dispatcher=object())
    ev = {
        "id": 7,
        "event_type": "payment_due_reminder",
        "application_id": uuid.uuid4(),
        "patient_id": uuid.uuid4(),
        "payload": {},
    }
    plan = NotificationPlan("payment_due_reminder", "dashboard", _payment_ctx(), None)
    processor._record_dashboard(ev, plan)
    (row,) = _comms_rows(db)
    assert row.channel == "dashboard"
    assert row.notification_type == "payment_due_reminder"
    assert "$250.00" in row.body
    assert row.patient_id == ev["patient_id"]
    assert row.application_id == ev["application_id"]


# ---------------------------------------------------------------------------
# Admin API surface
# ---------------------------------------------------------------------------


def _client(db: FakeSession) -> TestClient:
    from app.api.v1.api import api_router
    from app.core.auth import get_current_user
    from app.db.base import get_db

    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _ADMIN_USER
    return TestClient(app)


def _route_methods(app_router):
    """(path, methods) pairs, descending into nested routers."""
    for r in app_router.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None)
        if path and methods:
            yield path, methods
        for sub in getattr(r, "routes", []) or []:
            sp = getattr(sub, "path", None)
            sm = getattr(sub, "methods", None)
            if sp and sm:
                yield sp, sm


def test_routes_mounted_and_append_only_surface():
    # Verify the surface through the TestClient rather than introspecting route
    # objects: FastAPI 0.139 resolves include_router lazily, so walking
    # app.routes doesn't reliably materialize nested routes across versions.
    # Driving requests forces full route resolution and is version-independent.
    from app.api.v1.api import api_router
    from app.core.auth import get_current_user
    from app.db.base import get_db

    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: FakeSession()
    app.dependency_overrides[get_current_user] = lambda: _ADMIN_USER
    client = TestClient(app, raise_server_exceptions=False)

    base = "/api/v1/admin/communications"
    # Mounted: a routed path never 404s (an unmounted path would).
    assert client.get(base).status_code != 404
    assert client.get(f"{base}/templates").status_code != 404
    assert client.post(f"{base}/preview", json={}).status_code != 404
    assert client.post(f"{base}/send", json={}).status_code != 404
    assert client.post(f"{base}/offline", json={}).status_code != 404
    # Append-only: NO update/delete verbs on the comms surface. The paths ARE
    # mounted, so an absent mutation handler answers 405 (method not allowed),
    # never 200/2xx — proving the WORM log has no edit/delete route.
    for path in (base, f"{base}/send", f"{base}/templates"):
        for verb in ("put", "patch", "delete"):
            assert getattr(client, verb)(path).status_code in (404, 405), (verb, path)


def test_templates_endpoint_lists_registry():
    client = _client(FakeSession())
    resp = client.get("/api/v1/admin/communications/templates")
    assert resp.status_code == 200
    by_type = {t["notification_type"]: t for t in resp.json()}
    assert "payment_due_reminder" in by_type
    assert by_type["payment_due_reminder"]["channels"] == ["email", "sms"]
    assert by_type["loan_repaid"]["channels"] == ["email"]  # email-only type


def test_preview_renders_without_sending():
    db = FakeSession()
    client = _client(db)
    resp = client.post(
        "/api/v1/admin/communications/preview",
        json={
            "notification_type": "payment_due_reminder",
            "channel": "sms",
            "context": _payment_ctx(),
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "$250.00" in data["body"]
    assert _comms_rows(db) == []  # preview never writes the log


def test_preview_unknown_type_404_and_missing_context_422():
    client = _client(FakeSession())
    assert (
        client.post(
            "/api/v1/admin/communications/preview",
            json={"notification_type": "no_such_template", "channel": "email"},
        ).status_code
        == 404
    )
    # StrictUndefined: an empty merge context on a template with fields -> 422.
    assert (
        client.post(
            "/api/v1/admin/communications/preview",
            json={"notification_type": "payment_due_reminder", "channel": "email"},
        ).status_code
        == 422
    )


def test_send_endpoint_dispatches_and_logs(monkeypatch):
    from app.core.config import settings
    from app.models.platform.patient import PlatformPatient

    monkeypatch.setattr(settings, "USE_REAL_NOTIFICATIONS", False)
    patient_id = uuid.uuid4()
    db = FakeSession(
        query_rows={"PlatformPatient": [_patient_stub()]},
        get_map={("PlatformPatient", patient_id): _patient_stub()},
    )

    # The endpoint re-reads the logged row via query(PlatformCommunicationLog);
    # route the fake query for that model at the rows this session captured.
    real_query = db.query

    def query(model):
        if model is PlatformCommunicationLog:
            return FakeQuery(_comms_rows(db))
        if model is PlatformPatient:
            return real_query(model)
        return FakeQuery([])

    db.query = query

    client = _client(db)
    resp = client.post(
        "/api/v1/admin/communications/send",
        json={
            "patient_id": str(patient_id),
            "notification_type": "payment_due_reminder",
            "channel": "email",
            "context": _payment_ctx(),
            "comment": "borrower asked for a resend",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "sent"
    comm = data["communication"]
    assert comm["channel"] == "email"
    assert comm["notification_type"] == "payment_due_reminder"
    assert "Jordan" in comm["body"]
    assert comm["sent_by"] == "ops@payspyre.test"
    assert comm["comment"] == "borrower asked for a resend"
    assert db.committed == 1


def test_send_endpoint_unknown_patient_404():
    client = _client(FakeSession())  # get() returns None -> 404
    resp = client.post(
        "/api/v1/admin/communications/send",
        json={
            "patient_id": str(uuid.uuid4()),
            "notification_type": "payment_due_reminder",
            "channel": "email",
            "context": _payment_ctx(),
        },
    )
    assert resp.status_code == 404


def test_offline_contact_logged_with_mandatory_comment():
    from app.models.platform.patient import PlatformPatient  # noqa: F401

    patient_id = uuid.uuid4()
    db = FakeSession(get_map={("PlatformPatient", patient_id): _patient_stub()})
    client = _client(db)
    resp = client.post(
        "/api/v1/admin/communications/offline",
        json={
            "patient_id": str(patient_id),
            "contact_method": "phone",
            "direction": "inbound",
            "purpose": "Payout request",
            "result": "Provided payout figure as of 2026-08-01",
            "comment": "Borrower called asking for a payout amount as of Aug 1.",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["channel"] == "offline"
    assert data["direction"] == "inbound"
    assert data["contact_method"] == "phone"
    assert data["body"] == data["comment"]
    assert data["sent_by"] == "ops@payspyre.test"
    (row,) = _comms_rows(db)
    assert row.comment.startswith("Borrower called")


def test_offline_contact_requires_comment_and_anchor():
    client = _client(FakeSession())
    # Missing comment -> pydantic 422.
    assert (
        client.post(
            "/api/v1/admin/communications/offline",
            json={
                "patient_id": str(uuid.uuid4()),
                "contact_method": "phone",
                "purpose": "x",
            },
        ).status_code
        == 422
    )
    # No anchor at all -> 422.
    assert (
        client.post(
            "/api/v1/admin/communications/offline",
            json={"contact_method": "phone", "purpose": "x", "comment": "y"},
        ).status_code
        == 422
    )


def test_list_endpoint_returns_bodies():
    row = record_communication(
        FakeSession(),
        channel="email",
        recipient="jordan@example.com",
        subject="Subj",
        body="<html>evidence body</html>",
        notification_type="welcome",
        sent_by="system",
        status="sent",
    )
    db = FakeSession(query_rows={"PlatformCommunicationLog": [row]})
    client = _client(db)
    resp = client.get("/api/v1/admin/communications", params={"channel": "email"})
    assert resp.status_code == 200
    (item,) = resp.json()
    assert item["body"] == "<html>evidence body</html>"
    assert item["subject"] == "Subj"
    # Bad channel filter is rejected at the query-param layer.
    assert (
        client.get("/api/v1/admin/communications", params={"channel": "fax"}).status_code
        == 422
    )
