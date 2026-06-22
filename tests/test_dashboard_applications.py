"""Tests for the clinic dashboard — applications analytics (Phase 1).

Covers ``app/api/clinic/v1/endpoints/dashboard_applications.py``:
  * pure helpers (``build_timeseries``, ``applications_block``) with MagicMock
    sessions — fast, no DB, mirroring ``tests/test_clinic_api.py``;
  * the timeseries endpoint via a mocked-DB ``TestClient``;
  * a real DB-backed scoping test (``db_session`` fixture) proving that one
    vendor never sees another vendor's applications.

Run (only this file):
    python -m pytest tests/test_dashboard_applications.py -x -q
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.api.clinic.v1.endpoints import dashboard_applications as dash
from app.db.base import get_db

_BASE = "/api/clinic/v1"
_VENDOR_ID = uuid.uuid4()

_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


# --- fakes -----------------------------------------------------------------


def _fake_app(status="approved", amount=1_000_000, created_at=None, decision_at=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        requested_amount_cents=amount,
        created_at=created_at or _NOW,
        decision_at=decision_at,
    )


@pytest.fixture
def app_under_test():
    app = FastAPI()
    # Mount only the module under test (the user wires dash.router into
    # clinic_router separately; we don't depend on that here).
    app.include_router(dash.router, prefix="/api/clinic/v1")
    app.dependency_overrides[get_current_clinic_user] = lambda: ClinicPrincipal(
        user=SimpleNamespace(id=uuid.uuid4()), vendor_id=_VENDOR_ID
    )
    yield app
    app.dependency_overrides.clear()


def _wire_created(app_under_test, created_rows, decided_rows=None):
    """Wire a MagicMock DB so the two vendor-scoped queries return given rows.

    ``query_applications_in_window`` ends in ``.order_by().all()`` (no decision
    filter); ``query_decided_in_window`` ends in ``.filter().all()`` (no
    order_by). We route by whether ``.order_by`` was chained.
    """
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = (
        created_rows
    )
    db.query.return_value.filter.return_value.all.return_value = (
        decided_rows if decided_rows is not None else []
    )
    app_under_test.dependency_overrides[get_db] = lambda: db
    return db


# --- _outcome_bucket / status vocab ----------------------------------------


class TestOutcomeBucket:
    def test_terminal_decisions(self):
        assert dash._outcome_bucket("approved") == "approved"
        assert dash._outcome_bucket("declined") == "declined"

    def test_started_stands_alone(self):
        assert dash._outcome_bucket("started") == "started"

    def test_full_review_vocab_maps_to_in_review(self):
        for s in ("verifying", "pre_qualified", "awaiting_hard_pull", "under_review"):
            assert dash._outcome_bucket(s) == "in_review"

    def test_dead_and_unknown_states_are_none(self):
        assert dash._outcome_bucket("withdrawn") is None
        assert dash._outcome_bucket("expired") is None
        assert dash._outcome_bucket("brand_new_enum") is None


# --- _bucket_start ----------------------------------------------------------


class TestBucketStart:
    def test_day(self):
        dt = datetime(2026, 6, 15, 23, 59, tzinfo=timezone.utc)
        assert dash._bucket_start(dt, "day") == datetime(2026, 6, 15, tzinfo=timezone.utc)

    def test_week_starts_monday(self):
        # 2026-06-15 is a Monday; 2026-06-21 (Sun) buckets to the same Monday.
        sun = datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc)
        assert dash._bucket_start(sun, "week") == datetime(2026, 6, 15, tzinfo=timezone.utc)

    def test_month(self):
        dt = datetime(2026, 6, 27, 8, 0, tzinfo=timezone.utc)
        assert dash._bucket_start(dt, "month") == datetime(2026, 6, 1, tzinfo=timezone.utc)


# --- build_timeseries -------------------------------------------------------


class TestBuildTimeseries:
    def test_buckets_by_day_with_outcomes_and_amounts(self):
        d1 = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
        d2 = datetime(2026, 6, 2, 9, 0, tzinfo=timezone.utc)
        rows = [
            _fake_app(status="approved", amount=100, created_at=d1),
            _fake_app(status="declined", amount=200, created_at=d1),
            _fake_app(status="under_review", amount=300, created_at=d1),
            _fake_app(status="started", amount=400, created_at=d2),
            _fake_app(status="withdrawn", amount=500, created_at=d2),  # amount only
        ]
        ts = dash.build_timeseries(rows, "day")
        assert ts.granularity == "day"
        assert [p.bucket for p in ts.points] == ["2026-06-01", "2026-06-02"]

        p1, p2 = ts.points
        assert (p1.approved, p1.declined, p1.in_review, p1.started) == (1, 1, 1, 0)
        assert p1.requested_amount_cents == 600
        # withdrawn contributes amount but no outcome count
        assert (p2.started, p2.approved, p2.declined, p2.in_review) == (1, 0, 0, 0)
        assert p2.requested_amount_cents == 900

    def test_empty(self):
        ts = dash.build_timeseries([], "day")
        assert ts.points == []

    def test_month_granularity_collapses(self):
        rows = [
            _fake_app(status="approved", created_at=datetime(2026, 5, 3, tzinfo=timezone.utc)),
            _fake_app(status="approved", created_at=datetime(2026, 5, 28, tzinfo=timezone.utc)),
            _fake_app(status="declined", created_at=datetime(2026, 6, 1, tzinfo=timezone.utc)),
        ]
        ts = dash.build_timeseries(rows, "month")
        assert [p.bucket for p in ts.points] == ["2026-05-01", "2026-06-01"]
        assert ts.points[0].approved == 2
        assert ts.points[1].declined == 1


# --- applications_block (pure, mocked DB) -----------------------------------


class TestApplicationsBlock:
    def _block(self, created, decided):
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = created
        db.query.return_value.filter.return_value.all.return_value = decided
        frm = _NOW - timedelta(days=30)
        return dash.applications_block(db, _VENDOR_ID, frm, _NOW)

    def test_exact_keys(self):
        block = self._block([], [])
        assert set(block.keys()) == {
            "total", "approved", "declined", "in_review", "started",
            "approval_rate", "avg_requested_amount_cents",
            "total_requested_amount_cents",
        }

    def test_counts_and_amounts_over_created(self):
        created = [
            _fake_app(status="approved", amount=1_000),
            _fake_app(status="declined", amount=2_000),
            _fake_app(status="verifying", amount=3_000),
            _fake_app(status="under_review", amount=1_000),
            _fake_app(status="started", amount=0),
            _fake_app(status="expired", amount=4_000),  # total only, not in_review
        ]
        block = self._block(created, [])
        assert block["total"] == 6
        assert block["approved"] == 1
        assert block["declined"] == 1
        assert block["in_review"] == 2  # verifying + under_review
        assert block["started"] == 1
        assert block["total_requested_amount_cents"] == 11_000
        assert block["avg_requested_amount_cents"] == 11_000 // 6

    def test_approval_rate_over_decided_population(self):
        # created population is irrelevant to approval_rate; decided drives it.
        decided = [
            _fake_app(status="approved"),
            _fake_app(status="approved"),
            _fake_app(status="approved"),
            _fake_app(status="declined"),
        ]
        block = self._block([], decided)
        assert block["approval_rate"] == 0.75

    def test_approval_rate_none_when_no_decisions(self):
        block = self._block([_fake_app(status="started")], [])
        assert block["approval_rate"] is None

    def test_empty_window(self):
        block = self._block([], [])
        assert block["total"] == 0
        assert block["avg_requested_amount_cents"] == 0
        assert block["total_requested_amount_cents"] == 0
        assert block["approval_rate"] is None


# --- timeseries endpoint (mocked DB) ----------------------------------------


class TestTimeseriesEndpoint:
    def test_default_window_day(self, app_under_test):
        rows = [
            _fake_app(status="approved", amount=100, created_at=_NOW),
            _fake_app(status="declined", amount=200, created_at=_NOW),
        ]
        self._wire = _wire_created(app_under_test, rows)
        client = TestClient(app_under_test)
        resp = client.get(f"{_BASE}/dashboard/applications/timeseries")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["granularity"] == "day"
        assert len(body["points"]) == 1
        pt = body["points"][0]
        assert pt["approved"] == 1 and pt["declined"] == 1
        assert pt["requested_amount_cents"] == 300

    def test_granularity_passthrough(self, app_under_test):
        _wire_created(app_under_test, [])
        client = TestClient(app_under_test)
        resp = client.get(
            f"{_BASE}/dashboard/applications/timeseries?granularity=week"
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["granularity"] == "week"

    def test_requires_auth(self, app_under_test):
        app_under_test.dependency_overrides.pop(get_current_clinic_user, None)
        app_under_test.dependency_overrides[get_db] = lambda: MagicMock()
        client = TestClient(app_under_test)
        resp = client.get(f"{_BASE}/dashboard/applications/timeseries")
        assert resp.status_code in (401, 403)


# --- real DB scoping (no cross-vendor leakage) ------------------------------


def _seed_vendor(db, *, suffix):
    vid = uuid.uuid4()
    db.execute(
        text(
            """
            INSERT INTO vendors
                (id, business_name, business_type, contact_name, email, phone,
                 address_line1, city, province, postal_code, status)
            VALUES
                (:id, :name, 'corporation', :contact, :email, '+15555550100',
                 '1 Main St', 'Kelowna', 'BC', 'V1Y0A1', 'active')
            """
        ),
        {
            "id": str(vid),
            "name": f"Clinic {suffix}",
            "contact": f"Contact {suffix}",
            "email": f"clinic-{suffix}-{vid}@example.com",
        },
    )
    return vid


def _seed_patient(db):
    pid = uuid.uuid4()
    db.execute(
        text(
            """
            INSERT INTO platform_patients (id, legal_first_name, legal_last_name, email)
            VALUES (:id, 'Pat', 'Ient', :email)
            """
        ),
        {"id": str(pid), "email": f"pat-{pid}@example.com"},
    )
    return pid


def _seed_app(db, *, vendor_id, patient_id, product_id, product_version,
              status, amount, created_at, decision_at=None):
    aid = uuid.uuid4()
    db.execute(
        text(
            """
            INSERT INTO platform_credit_applications
                (id, patient_id, credit_product_id, credit_product_version,
                 requested_amount_cents, requested_amount_source, vendor_id,
                 status, created_at, decision_at)
            VALUES
                (:id, :pid, :prod, :pver, :amt, 'clinic', :vid,
                 CAST(:status AS platform_application_status), :created, :decided)
            """
        ),
        {
            "id": str(aid), "pid": str(patient_id), "prod": str(product_id),
            "pver": product_version, "amt": amount, "vid": str(vendor_id),
            "status": status, "created": created_at, "decided": decision_at,
        },
    )
    return aid


class TestRealDbScoping:
    def test_vendor_isolation(self, db_session):
        db = db_session
        # The dental_full_arch_v1 product is re-seeded by conftest after TRUNCATE.
        prod = db.execute(
            text("SELECT id, version FROM platform_credit_products "
                 "WHERE code = 'dental_full_arch_v1'")
        ).first()
        assert prod is not None, "expected seeded dental_full_arch_v1 product"
        product_id, product_version = prod[0], prod[1]

        patient_id = _seed_patient(db)
        mine = _seed_vendor(db, suffix="mine")
        other = _seed_vendor(db, suffix="other")

        now = datetime.now(timezone.utc)
        c1 = now - timedelta(days=5)
        c2 = now - timedelta(days=3)

        # My vendor: 1 approved + 1 declined (decided in window) + 1 in_review.
        _seed_app(db, vendor_id=mine, patient_id=patient_id, product_id=product_id,
                  product_version=product_version, status="approved", amount=1_000,
                  created_at=c1, decision_at=c1)
        _seed_app(db, vendor_id=mine, patient_id=patient_id, product_id=product_id,
                  product_version=product_version, status="declined", amount=3_000,
                  created_at=c2, decision_at=c2)
        _seed_app(db, vendor_id=mine, patient_id=patient_id, product_id=product_id,
                  product_version=product_version, status="under_review", amount=2_000,
                  created_at=c2)
        # Other vendor: loud data that must NEVER appear in my results.
        for _ in range(5):
            _seed_app(db, vendor_id=other, patient_id=patient_id,
                      product_id=product_id, product_version=product_version,
                      status="approved", amount=9_999_999,
                      created_at=c1, decision_at=c1)
        db.flush()

        frm = now - timedelta(days=30)
        block = dash.applications_block(db, mine, frm, now)

        assert block["total"] == 3
        assert block["approved"] == 1
        assert block["declined"] == 1
        assert block["in_review"] == 1
        assert block["total_requested_amount_cents"] == 6_000  # NOT polluted by other
        assert block["approval_rate"] == 0.5  # 1 approved / (1+1) decided

        # Timeseries is likewise scoped: only my 3 apps, none of the 5 others'.
        rows = dash.query_applications_in_window(db, mine, frm, now)
        assert len(rows) == 3
        ts = dash.build_timeseries(rows, "day")
        total_amt = sum(p.requested_amount_cents for p in ts.points)
        assert total_amt == 6_000

        # And the other vendor sees only its own.
        other_block = dash.applications_block(db, other, frm, now)
        assert other_block["total"] == 5
        assert other_block["total_requested_amount_cents"] == 5 * 9_999_999
