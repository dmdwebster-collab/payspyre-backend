"""Archive queue (WS-I) — server-side pagination, sorting, assignee, and the
registry-driven close-reason vocabulary.

DB-backed: the queue is a two-table merge with SQL-side filters, and the whole
point of this workstream is that ``total`` and the page window agree, so the
tests have to run the real query. The pure derivations live in
``tests/test_parity_i_archive_misc.py`` (DB-free).

Run just this file:

    source .venv/bin/activate && \
        python -m pytest tests/test_admin_archive_queue.py -p no:warnings -q
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from types import SimpleNamespace

from app.api.v1.api import api_router
from app.core.auth import get_current_user
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.loan import PlatformLoan
from app.models.platform.patient import PlatformPatient
from app.models.user import User
from app.services import archive

_BASE = "/api/v1/admin/archive"


def _admin():
    return SimpleNamespace(
        id=uuid.uuid4(), roles=[SimpleNamespace(role=SimpleNamespace(name="admin"))]
    )


@pytest.fixture
def client(db_session: Session):
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = _admin
    yield TestClient(app)
    app.dependency_overrides.clear()


def _product_id(db):
    return (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
        .id
    )


def _user(db) -> User:
    user = User(
        email=f"arch-{uuid.uuid4().hex[:8]}@example.com",
        first_name="Ada",
        last_name="Lovelace",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _application(
    db,
    *,
    status: str,
    closed_at: datetime,
    flow_state: dict | None = None,
    assignee: User | None = None,
    amount: int = 500_000,
) -> PlatformCreditApplication:
    patient = PlatformPatient(
        email=f"arch-{uuid.uuid4().hex[:8]}@example.com",
        legal_first_name="Closed",
        legal_last_name="Record",
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)
    app_row = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=_product_id(db),
        credit_product_version=1,
        requested_amount_cents=amount,
        requested_amount_source="clinic",
        status=status,
        flow_state=flow_state or {},
        status_updated_at=closed_at,
        assigned_to_user_id=assignee.id if assignee else None,
    )
    db.add(app_row)
    db.commit()
    db.refresh(app_row)
    return app_row


def _loan(db, application, *, status="paid_off", closed_at=None) -> PlatformLoan:
    loan = PlatformLoan(
        application_id=application.id,
        principal_cents=application.requested_amount_cents,
        annual_rate_bps=1290,
        term_months=24,
        status=status,
        principal_balance_cents=0,
        currency="CAD",
        closed_at=closed_at,
        closed_at_source="transition" if closed_at else None,
    )
    db.add(loan)
    db.commit()
    db.refresh(loan)
    return loan


def _dt(day: int) -> datetime:
    return datetime(2026, 6, day, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------


def test_close_reasons_exposed_are_registry_driven(client):
    r = client.get(_BASE)
    assert r.status_code == 200, r.text
    body = r.json()
    # The chips the UI renders. Registry-driven: the four states our loan enum
    # cannot express must be here.
    for reason in ("renewed", "refinanced", "transferred", "settlement"):
        assert reason in body["close_reasons"]
    assert {o["value"] for o in body["close_reason_options"]} == set(body["close_reasons"])
    assert body["sort_fields"] == ["closed_at", "id"]


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------


def test_pagination_total_and_window(client, db_session):
    for day in (1, 2, 3, 4, 5):
        _application(db_session, status="declined", closed_at=_dt(day))

    first = client.get(_BASE, params={"limit": 2, "offset": 0}).json()
    assert first["total"] >= 5
    assert first["limit"] == 2 and first["offset"] == 0
    assert len(first["records"]) == 2

    second = client.get(_BASE, params={"limit": 2, "offset": 2}).json()
    assert second["total"] == first["total"]
    ids_first = {r["record_id"] for r in first["records"]}
    ids_second = {r["record_id"] for r in second["records"]}
    assert not (ids_first & ids_second)  # no overlap between pages


def test_offset_beyond_total_is_empty_not_an_error(client, db_session):
    _application(db_session, status="declined", closed_at=_dt(1))
    body = client.get(_BASE, params={"limit": 10, "offset": 10_000}).json()
    assert body["records"] == []
    assert body["total"] >= 1


# ---------------------------------------------------------------------------
# sorting
# ---------------------------------------------------------------------------


def test_sort_close_date_both_directions(client, db_session):
    made = [
        _application(db_session, status="declined", closed_at=_dt(day)).id
        for day in (7, 9, 8)
    ]
    wanted = {str(i) for i in made}

    desc = [
        r
        for r in client.get(_BASE, params={"limit": 500}).json()["records"]
        if r["record_id"] in wanted
    ]
    assert [r["closed_at"][:10] for r in desc] == ["2026-06-09", "2026-06-08", "2026-06-07"]

    asc = [
        r
        for r in client.get(_BASE, params={"limit": 500, "order": "asc"}).json()["records"]
        if r["record_id"] in wanted
    ]
    assert [r["closed_at"][:10] for r in asc] == ["2026-06-07", "2026-06-08", "2026-06-09"]


def test_sort_by_id(client, db_session):
    _application(db_session, status="declined", closed_at=_dt(3))
    _application(db_session, status="declined", closed_at=_dt(4))
    rows = client.get(_BASE, params={"sort": "id", "limit": 500}).json()["records"]
    ids = [r["record_id"] for r in rows]
    assert ids == sorted(ids, reverse=True)


def test_unknown_sort_is_422(client):
    assert client.get(_BASE, params={"sort": "close_date"}).status_code == 422
    assert client.get(_BASE, params={"order": "sideways"}).status_code == 422
    assert client.get(_BASE, params={"close_reason": "nope"}).status_code == 422


# ---------------------------------------------------------------------------
# assignee
# ---------------------------------------------------------------------------


def test_assignee_on_rows_and_filter(client, db_session):
    user = _user(db_session)
    mine = _application(db_session, status="declined", closed_at=_dt(11), assignee=user)
    theirs = _application(db_session, status="declined", closed_at=_dt(12))

    body = client.get(_BASE, params={"assignee_id": str(user.id), "limit": 500}).json()
    ids = {r["record_id"] for r in body["records"]}
    assert str(mine.id) in ids
    assert str(theirs.id) not in ids
    row = next(r for r in body["records"] if r["record_id"] == str(mine.id))
    assert row["assignee"]["name"] == "Ada Lovelace"
    assert row["assignee"]["initials"] == "AL"

    unassigned = client.get(
        _BASE, params={"assignee_id": archive.UNASSIGNED, "limit": 500}
    ).json()
    unassigned_ids = {r["record_id"] for r in unassigned["records"]}
    assert str(theirs.id) in unassigned_ids
    assert str(mine.id) not in unassigned_ids
    assert body["total"] + unassigned["total"] >= 2


def test_assignee_filter_rejects_garbage(client):
    assert client.get(_BASE, params={"assignee_id": "not-a-uuid"}).status_code == 422


# ---------------------------------------------------------------------------
# close reasons / population
# ---------------------------------------------------------------------------


def test_expired_split_is_a_sql_filter_so_total_is_exact(client, db_session):
    plain = _application(db_session, status="expired", closed_at=_dt(13))
    bank = _application(
        db_session,
        status="expired",
        closed_at=_dt(14),
        flow_state={"expiry_reason": "bank_verification"},
    )

    body = client.get(_BASE, params={"close_reason": "expired", "limit": 500}).json()
    ids = {r["record_id"] for r in body["records"]}
    assert str(plain.id) in ids and str(bank.id) not in ids
    assert body["total"] == len(body["records"])  # exact: nothing post-filtered away

    body = client.get(
        _BASE, params={"close_reason": "bank_verification_expired", "limit": 500}
    ).json()
    ids = {r["record_id"] for r in body["records"]}
    assert str(bank.id) in ids and str(plain.id) not in ids


def test_daves_closed_states_populate_the_new_chips(client, db_session):
    """A loan whose APPLICATION was settled/renewed archives under that reason
    even though ``platform_loan_status`` has no such value."""
    app_row = _application(db_session, status="settlement", closed_at=_dt(15))
    loan = _loan(db_session, app_row, status="active", closed_at=_dt(15))

    body = client.get(_BASE, params={"close_reason": "settlement", "limit": 500}).json()
    row = next(r for r in body["records"] if r["record_id"] == str(loan.id))
    assert row["record_type"] == "loan"
    assert row["close_reason"] == "settlement"
    assert row["closed_at_source"] == "transition"


def test_loan_closed_at_source_falls_back_to_the_proxy(client, db_session):
    app_row = _application(db_session, status="approved", closed_at=_dt(16))
    loan = _loan(db_session, app_row, status="paid_off", closed_at=None)

    body = client.get(_BASE, params={"close_reason": "repaid", "limit": 500}).json()
    row = next(r for r in body["records"] if r["record_id"] == str(loan.id))
    assert row["closed_at_source"] == "updated_at"  # honesty flag preserved
    assert row["closed_at"] is not None


def test_loan_detail_reports_the_real_close_timestamp(client, db_session):
    app_row = _application(db_session, status="repaid", closed_at=_dt(17))
    loan = _loan(db_session, app_row, status="paid_off", closed_at=_dt(17))
    body = client.get(f"{_BASE}/loan/{loan.id}").json()
    assert body["close_reason"] == "repaid"
    assert body["closed_at_source"] == "transition"
    assert body["closed_at"].startswith("2026-06-17")
