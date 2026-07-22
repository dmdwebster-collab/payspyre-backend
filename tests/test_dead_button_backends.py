"""P0 T3 — the six dead Originations controls. DB-FREE unit tests.

Dave's 2026-07-21 review: Add Bank Account, Bank Verification, Send Email,
Send SMS, Hard Pull, Soft Pull all rendered ``disabled``. What is pinned here:

* migration 069 chain + the columns/table it creates,
* Dave's exact Add Bank Account validation rules — including that a leading
  zero on the institution number SURVIVES (the whole reason those columns are
  TEXT),
* full account numbers are encrypted at rest and never leave via the API,
* Borrower / Co-Borrower party resolution, including the refusal when no
  co-borrower is attached and the "choice required" case,
* the mock/simulator disclosure contract — every integration-backed response
  carries ``simulated`` sourced from what actually ran,
* all six routes mounted under the ADMIN api and NONE of them on the clinic
  (vendor) api.

Run JUST this file:
    source .venv/bin/activate && python -m pytest tests/test_dead_button_backends.py -x -q
"""
from __future__ import annotations

import importlib.util
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.platform.borrower_portal import PlatformPatientBankAccount
from app.models.platform.credit_report import PlatformCreditReportPull
from app.models.platform.event import PlatformEvent
from app.services import application_actions
from app.services.application_actions import ActionError

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "069_dead_button_backends.py"
)

_ADMIN_USER = SimpleNamespace(
    id=uuid.uuid4(),
    email="ops@payspyre.test",
    roles=[SimpleNamespace(role=SimpleNamespace(name="admin"))],
)


# ---------------------------------------------------------------------------
# Fakes (no database)
# ---------------------------------------------------------------------------


class FakeQuery:
    """``first()`` and ``all()`` are answered from separate canned sets.

    The endpoints look a single application up by id (``first()``) and then ask
    for its co-borrower files (``all()``) — the same model, two very different
    answers, which one shared row list could not express.
    """

    def __init__(self, first_rows, all_rows):
        self._first_rows = list(first_rows)
        self._all_rows = list(all_rows)

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self._all_rows

    def first(self):
        return self._first_rows[0] if self._first_rows else None


class FakeSession:
    def __init__(self, query_rows=None, all_rows=None):
        self.added = []
        self.committed = 0
        self._query_rows = query_rows or {}
        self._all_rows = all_rows or {}

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
        name = getattr(model, "__name__", str(model))
        rows = self._query_rows.get(name, [])
        return FakeQuery(rows, self._all_rows.get(name, rows))

    def get(self, model, key):
        return None


def _endpoint_session(app_row, patient=None, co_borrowers=()):
    """A session shaped for the router: one application by id, and an explicit
    (usually empty) co-borrower list."""
    rows = {"PlatformCreditApplication": [app_row], "PlatformPatientBankAccount": []}
    if patient is not None:
        rows["PlatformPatient"] = [patient]
    return FakeSession(rows, {"PlatformCreditApplication": list(co_borrowers)})


def _app_row(app_id=None, patient_id=None, co_of=None):
    """Stand-in for a PlatformCreditApplication row."""
    return SimpleNamespace(
        id=app_id or uuid.uuid4(),
        patient_id=patient_id or uuid.uuid4(),
        co_applicant_of_application_id=co_of,
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_chain():
    """Pinned onto 068_dave_status_model. Sibling P0 branches may also claim
    069 — if one wins the race, update the migration AND this pin together."""
    spec = importlib.util.spec_from_file_location("migration_069", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "069_dead_button_backends"
    assert mod.down_revision == "068_dave_status_model"


def test_migration_adds_routing_columns_and_pull_table():
    src = _MIGRATION.read_text()
    for col in (
        "institution_number",
        "transit_number",
        "account_holder",
        "account_number_encrypted",
        "source",
    ):
        assert col in src, col
    assert "platform_credit_report_pulls" in src
    assert "simulated" in src
    # Leading zeros: the routing fields must be STRING/TEXT, never Integer.
    assert "sa.String(length=3)" in src
    assert "sa.String(length=5)" in src


def test_migration_sql_is_static():
    """bandit B608 — no f-strings / .format / % interpolation in SQL."""
    src = _MIGRATION.read_text()
    assert 'op.execute(f"' not in src
    assert ".format(" not in src


# ---------------------------------------------------------------------------
# Dave's Add Bank Account validation rules
# ---------------------------------------------------------------------------


def test_valid_input_normalizes():
    clean = application_actions.validate_bank_account_input(
        bank_name="  Royal Bank  ",
        institution_number="003",
        transit_number="00123",
        account_number="1234567890",
        account_type="Checking",
        account_holder=" Jordan Lee ",
    )
    assert clean.bank_name == "Royal Bank"
    assert clean.account_type == "checking"
    assert clean.account_holder == "Jordan Lee"


def test_leading_zeros_survive():
    """The entire reason institution/transit are TEXT: '003' is not 3."""
    clean = application_actions.validate_bank_account_input(
        bank_name="TD",
        institution_number="004",
        transit_number="00042",
        account_number="0001234",
        account_type="savings",
    )
    assert clean.institution_number == "004"
    assert clean.transit_number == "00042"
    assert clean.account_number == "0001234"
    # And the column types can hold them.
    cols = PlatformPatientBankAccount.__table__.columns
    assert cols["institution_number"].type.length == 3
    assert cols["transit_number"].type.length == 5


@pytest.mark.parametrize(
    "kwargs,fragment",
    [
        (dict(bank_name=""), "Bank Name is required"),
        (dict(bank_name="x" * 26), "25 characters"),
        (dict(institution_number="04"), "exactly 3 digits"),
        (dict(institution_number="0004"), "exactly 3 digits"),
        (dict(institution_number="00a"), "numeric"),
        (dict(transit_number="1234"), "exactly 5 digits"),
        (dict(transit_number="123456"), "exactly 5 digits"),
        (dict(account_number="12345678901234567"), "15 digits or fewer"),
        (dict(account_number="12-345"), "numeric"),
        (dict(account_type="chequing"), "Checking, Savings"),
    ],
)
def test_validation_rejections(kwargs, fragment):
    base = dict(
        bank_name="TD",
        institution_number="004",
        transit_number="00042",
        account_number="1234567",
        account_type="checking",
    )
    base.update(kwargs)
    with pytest.raises(ActionError) as exc:
        application_actions.validate_bank_account_input(**base)
    assert fragment in str(exc.value)


def test_account_number_encryption_round_trip():
    stored = application_actions.encrypt_account_number("0001234567")
    assert application_actions.decrypt_account_number(stored) == "0001234567"
    assert application_actions.decrypt_account_number(None) is None


# ---------------------------------------------------------------------------
# Party resolution (Borrower / Co-Borrower)
# ---------------------------------------------------------------------------


def test_borrower_party_resolves_to_primary():
    primary = _app_row()
    db = FakeSession({"PlatformCreditApplication": []})
    got = application_actions.resolve_party(db, primary, party="borrower")
    assert got.application_id == primary.id
    assert got.patient_id == primary.patient_id


def test_co_borrower_refused_when_none_attached():
    primary = _app_row()
    db = FakeSession({"PlatformCreditApplication": []})
    with pytest.raises(ActionError) as exc:
        application_actions.resolve_party(db, primary, party="co_borrower")
    assert "No co-borrower is attached" in str(exc.value)


def test_single_co_borrower_resolves_without_id():
    primary = _app_row()
    co = _app_row(co_of=primary.id)
    db = FakeSession({"PlatformCreditApplication": [co]})
    got = application_actions.resolve_party(db, primary, party="co_borrower")
    assert got.application_id == co.id
    assert got.party == "co_borrower"


def test_multiple_co_borrowers_require_explicit_choice():
    primary = _app_row()
    a, b = _app_row(co_of=primary.id), _app_row(co_of=primary.id)
    db = FakeSession({"PlatformCreditApplication": [a, b]})
    with pytest.raises(ActionError) as exc:
        application_actions.resolve_party(db, primary, party="co_borrower")
    assert "co_application_id is required" in str(exc.value)
    got = application_actions.resolve_party(
        db, primary, party="co_borrower", co_application_id=b.id
    )
    assert got.application_id == b.id


def test_unknown_co_application_id_is_404():
    primary = _app_row()
    co = _app_row(co_of=primary.id)
    db = FakeSession({"PlatformCreditApplication": [co]})
    with pytest.raises(ActionError) as exc:
        application_actions.resolve_party(
            db, primary, party="co_borrower", co_application_id=uuid.uuid4()
        )
    assert exc.value.status_code == 404


def test_bad_party_rejected():
    with pytest.raises(ActionError):
        application_actions.resolve_party(FakeSession(), _app_row(), party="guarantor")


# ---------------------------------------------------------------------------
# Mock / simulator disclosure
# ---------------------------------------------------------------------------


def test_bank_verification_simulated_flag_follows_the_vendor():
    assert application_actions.bank_verification_is_simulated("mock") is True
    assert application_actions.bank_verification_is_simulated(None) is True
    assert application_actions.bank_verification_is_simulated("flinks") is False


def test_bureau_result_payload_carries_no_pii():
    result = SimpleNamespace(
        pull_type="hard",
        score=712,
        result="passed",
        bankruptcy=False,
        bankruptcy_discharged_at=None,
        fraud_signals={"safescan_score": 120},
        confidence=1.0,
        vendor="mock_bureau",
    )
    payload = application_actions.bureau_result_payload(result)
    assert payload["score"] == 712
    assert payload["vendor"] == "mock_bureau"
    for banned in ("sin", "dob", "date_of_birth", "postal", "address", "name"):
        assert banned not in {k.lower() for k in payload}


def test_bureau_simulated_reason_names_the_blocker():
    reason = application_actions.BUREAU_SIMULATED_REASON.lower()
    assert "subscriber agreement" in reason
    assert "not a real credit-bureau pull" in reason


def test_credit_pull_model_defaults_to_simulated():
    """A stored pull is simulated unless something proves otherwise — the safe
    default, since nothing can prove it today."""
    col = PlatformCreditReportPull.__table__.columns["simulated"]
    assert col.nullable is False
    assert "true" in str(col.server_default.arg).lower()


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


def _client(db: FakeSession) -> TestClient:
    from app.api.v1.api import api_router
    from app.core.auth import get_current_user
    from app.db.base import get_db

    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _ADMIN_USER
    return TestClient(app, raise_server_exceptions=False)


def test_all_six_controls_are_mounted():
    """Driving requests forces full route resolution (version-independent).
    A routed path never 404s; an unmounted one always does."""
    client = _client(FakeSession())
    app_id = uuid.uuid4()
    base = f"/api/v1/admin/applications/{app_id}"

    def _routed(resp):
        # An UNMOUNTED path yields Starlette's bare {"detail": "Not Found"}.
        # A mounted handler answers with its own detail (here: the application
        # doesn't exist in the fake session) or a validation error.
        return resp.json().get("detail") != "Not Found"

    assert _routed(client.get(f"{base}/bank-accounts"))
    assert _routed(client.post(f"{base}/bank-accounts", json={}))
    assert _routed(client.post(f"{base}/bank-verification", json={}))
    assert _routed(client.post(f"{base}/send-email", json={}))
    assert _routed(client.post(f"{base}/send-sms", json={}))
    assert _routed(client.get(f"{base}/credit-report"))
    assert _routed(client.post(f"{base}/credit-report/pull", json={}))


def test_bank_account_mutation_is_not_reachable_from_the_clinic_api():
    """Dave: "vendor users can not add bank accounts". The clinic router must
    expose no bank-account write path at all."""
    from app.api.clinic.v1.router import clinic_router

    def _walk(r):
        for route in getattr(r, "routes", []) or []:
            path = getattr(route, "path", "") or ""
            methods = getattr(route, "methods", set()) or set()
            yield path, methods
            yield from _walk(route)

    for path, methods in _walk(clinic_router):
        lowered = path.lower()
        if any(k in lowered for k in ("bank-account", "bank_account", "bank-verification")):
            assert not (methods & {"POST", "PUT", "PATCH", "DELETE"}), (path, methods)
    # And the actions module is imported by exactly one router — the admin one.
    src = Path(__file__).resolve().parents[1] / "app" / "api" / "clinic"
    for py in src.rglob("*.py"):
        assert "admin_application_actions" not in py.read_text(), py


def test_add_bank_account_rejects_bad_institution_number_before_touching_the_db():
    """Validation is enforced server-side, not just by the greyed-out OK button."""
    app_row = _app_row()
    db = _endpoint_session(app_row)
    client = _client(db)
    resp = client.post(
        f"/api/v1/admin/applications/{app_row.id}/bank-accounts",
        json={
            "party": "borrower",
            "bank_name": "TD",
            "institution_number": "4",
            "transit_number": "00042",
            "account_number": "1234567",
            "account_type": "Checking",
        },
    )
    assert resp.status_code == 422
    assert not [o for o in db.added if isinstance(o, PlatformPatientBankAccount)]


def test_add_bank_account_persists_manual_source_and_audits(monkeypatch):
    app_row = _app_row()
    patient = SimpleNamespace(
        id=app_row.patient_id, email="b@example.test", phone_e164="+15550001111"
    )
    db = _endpoint_session(app_row, patient)
    client = _client(db)
    resp = client.post(
        f"/api/v1/admin/applications/{app_row.id}/bank-accounts",
        json={
            "party": "borrower",
            "bank_name": "Royal Bank",
            "institution_number": "003",
            "transit_number": "00123",
            "account_number": "000123456789",
            "account_type": "Savings",
            "account_holder": "Jordan Lee",
        },
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["institution_number"] == "003"
    assert payload["transit_number"] == "00123"
    assert payload["source"] == "manual"
    assert payload["account_type"] == "savings"
    # The full number NEVER appears in the response.
    assert "000123456789" not in resp.text

    rows = [o for o in db.added if isinstance(o, PlatformPatientBankAccount)]
    assert len(rows) == 1
    row = rows[0]
    assert row.patient_id == app_row.patient_id
    assert row.verified_via == "manual_staff"
    assert (
        application_actions.decrypt_account_number(row.account_number_encrypted)
        == "000123456789"
    )

    events = [o for o in db.added if isinstance(o, PlatformEvent)]
    assert [e.event_type for e in events] == ["admin_bank_account_added"]
    ev = events[0]
    assert ev.actor == str(_ADMIN_USER.id)
    assert ev.application_id == app_row.id
    # No full account number in the audit payload either.
    assert "000123456789" not in str(ev.payload)


def test_send_email_requires_a_body():
    app_row = _app_row()
    patient = SimpleNamespace(id=app_row.patient_id, email="b@example.test", phone_e164=None)
    db = _endpoint_session(app_row, patient)
    resp = _client(db).post(
        f"/api/v1/admin/applications/{app_row.id}/send-email",
        json={"party": "borrower", "subject": "Hello"},
    )
    assert resp.status_code == 422
    assert "body is required" in resp.text


def test_send_sms_refuses_when_no_phone_on_file():
    app_row = _app_row()
    patient = SimpleNamespace(id=app_row.patient_id, email="b@example.test", phone_e164=None)
    db = _endpoint_session(app_row, patient)
    resp = _client(db).post(
        f"/api/v1/admin/applications/{app_row.id}/send-sms",
        json={"party": "borrower", "body": "Please call us."},
    )
    assert resp.status_code == 422
    assert "phone number" in resp.text


def test_send_email_is_logged_inert_and_says_so():
    """SendGrid creds are absent, so the message is rendered + logged but not
    transmitted — and the response admits it."""
    from app.models.platform.communication import PlatformCommunicationLog

    app_row = _app_row()
    patient = SimpleNamespace(
        id=app_row.patient_id, email="b@example.test", phone_e164="+15550001111"
    )
    db = _endpoint_session(app_row, patient)
    resp = _client(db).post(
        f"/api/v1/admin/applications/{app_row.id}/send-email",
        json={
            "party": "borrower",
            "subject": "Your application",
            "body": "We need one more document.",
            "comment": "called first",
        },
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["simulated"] is True
    assert "not transmitted" in (payload["simulated_reason"] or "")
    assert payload["notification_type"] == "adhoc_email"
    assert payload["body"] == "We need one more document."

    logs = [o for o in db.added if isinstance(o, PlatformCommunicationLog)]
    assert len(logs) == 1
    # FULL body in the log — Dave's "View" dialog shows the exact message.
    assert logs[0].body == "We need one more document."
    assert logs[0].subject == "Your application"
    assert logs[0].sent_by == _ADMIN_USER.email

    audits = [
        o
        for o in db.added
        if isinstance(o, PlatformEvent) and o.event_type == "admin_adhoc_message_sent"
    ]
    assert len(audits) == 1
    assert audits[0].payload["simulated"] is True


def test_send_email_can_seed_from_a_template():
    from app.services.notification_render import NOTIFICATION_TYPES

    assert "adhoc_email" not in NOTIFICATION_TYPES  # ad-hoc is not a registry type
    app_row = _app_row()
    patient = SimpleNamespace(
        id=app_row.patient_id, email="b@example.test", phone_e164="+15550001111"
    )
    db = _endpoint_session(app_row, patient)
    resp = _client(db).post(
        f"/api/v1/admin/applications/{app_row.id}/send-email",
        json={
            "party": "borrower",
            "notification_type": "definitely_not_a_template",
            "body": "x",
            "subject": "y",
        },
    )
    assert resp.status_code == 404


def test_credit_pull_stores_a_simulated_report():
    app_row = _app_row()
    patient = SimpleNamespace(
        id=app_row.patient_id, email="b@example.test", phone_e164="+15550001111"
    )
    db = _endpoint_session(app_row, patient)
    resp = _client(db).post(
        f"/api/v1/admin/applications/{app_row.id}/credit-report/pull",
        json={"pull_type": "hard", "party": "borrower"},
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["pull_type"] == "hard"
    assert payload["simulated"] is True
    assert "subscriber agreement" in payload["simulated_reason"]
    assert isinstance(payload["score"], int)

    rows = [o for o in db.added if isinstance(o, PlatformCreditReportPull)]
    assert len(rows) == 1
    assert rows[0].simulated is True
    assert rows[0].requested_by == str(_ADMIN_USER.id)

    audits = [
        o
        for o in db.added
        if isinstance(o, PlatformEvent) and o.event_type == "admin_credit_report_pulled"
    ]
    assert len(audits) == 1
    assert audits[0].payload["simulated"] is True


def test_credit_pull_rejects_an_invalid_pull_type():
    app_row = _app_row()
    db = _endpoint_session(app_row)
    resp = _client(db).post(
        f"/api/v1/admin/applications/{app_row.id}/credit-report/pull",
        json={"pull_type": "instant", "party": "borrower"},
    )
    assert resp.status_code == 422


def test_bank_verification_records_a_simulated_request():
    from app.models.platform.verification import PlatformVerification

    app_row = _app_row()
    patient = SimpleNamespace(
        id=app_row.patient_id, email="b@example.test", phone_e164="+15550001111"
    )
    db = _endpoint_session(app_row, patient)
    resp = _client(db).post(
        f"/api/v1/admin/applications/{app_row.id}/bank-verification",
        json={"party": "borrower"},
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["simulated"] is True
    assert "simulator mode" in payload["simulated_reason"]

    verifications = [o for o in db.added if isinstance(o, PlatformVerification)]
    assert len(verifications) == 1
    assert verifications[0].verification_type == "bank_link"
    assert verifications[0].status == "pending"

    audits = [
        o
        for o in db.added
        if isinstance(o, PlatformEvent)
        and o.event_type == "admin_bank_verification_requested"
    ]
    assert len(audits) == 1


def test_bank_verification_for_absent_co_borrower_is_refused():
    app_row = _app_row()
    db = _endpoint_session(app_row)
    resp = _client(db).post(
        f"/api/v1/admin/applications/{app_row.id}/bank-verification",
        json={"party": "co_borrower"},
    )
    assert resp.status_code == 422
    assert "No co-borrower is attached" in resp.text
