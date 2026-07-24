"""Activation rework Wave 2 — loan booked at ACTIVATION, not approval.

Two tiers of proof, both behind the ``ACTIVATION_BOOKS_LOAN`` feature flag:

  * FAKE-SESSION unit tests (no DB) pin the flag-gated fork at
    ``loan_offers.accept_offer``: flag ON never books (returns no loan, advances
    the file to Agreement Signature); flag OFF is the UNCHANGED booking path.

  * A THROWAWAY, UTC-pinned, migrated Postgres DB drives the full flag-ON path
    end to end — approve/offers → accept (no loan) → application agreement
    signed (no loan) → ``activate`` (maker-checker) BOOKS the loan
    ``booked_at_activation=True`` / ``status=active`` with the agreement
    provenance copied + schedule generated → application ``active``. Idempotent
    double-activate + second-approver-differs are asserted too.

The throwaway DB is created fresh, ``ALTER DATABASE ... SET TimeZone 'UTC'``'d,
upgraded to head, and dropped — it never touches the shared ``payspyre_test`` DB.
If no local Postgres is reachable the DB tier is skipped (the fake-session tier
still runs, and CI provides the DB).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import loan_offers
from app.services.loan_offers import OfferSpec

NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


# ===========================================================================
# TIER 1 — fake-session unit tests (no DB): the accept_offer flag fork
# ===========================================================================


class _FakeQuery:
    def __init__(self, session, entity):
        self.session = session
        self.entity = entity

    def filter(self, *a):
        return self

    def with_for_update(self, **kw):
        return self

    def order_by(self, *a):
        return self

    def all(self):
        from app.models.platform.loan_offer import PlatformLoanOffer

        if self.entity[0] is PlatformLoanOffer:
            return list(self.session.offers)
        return []

    def first(self):
        rows = self.all()
        return rows[0] if rows else None


class _FakeSession:
    def __init__(self, offers):
        self.offers = offers
        self.added = []
        self.flushed = False
        self.committed = False

    def query(self, *entities):
        return _FakeQuery(self, entities)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushed = True

    def commit(self):
        self.committed = True


def _offer(**kw):
    d = dict(
        id=uuid.uuid4(),
        status="offered",
        amount_cents=600_000,
        term_months=30,
        annual_rate_bps=1699,
        first_due_date=None,
        expires_at=NOW.replace(year=2026, month=8),
        accepted_at=None,
    )
    d.update(kw)
    return SimpleNamespace(**d)


def _application(status="approved"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        patient_id=uuid.uuid4(),
        status=status,
        status_updated_at=None,
        decision={"outcome": "approved"},
        decision_by=None,
        decision_at=None,
    )


def test_accept_offer_flag_off_books_unchanged(monkeypatch):
    """Flag OFF (default): acceptance books the loan exactly as before."""
    monkeypatch.setattr(loan_offers.settings, "ACTIVATION_BOOKS_LOAN", False)
    picked, sibling = _offer(), _offer()
    session = _FakeSession([picked, sibling])
    app = _application(status="approved")

    booked = SimpleNamespace(id=uuid.uuid4(), status="pending_disbursement")
    import app.services.loan_lifecycle as ll

    monkeypatch.setattr(ll, "book_loan", lambda db, application, first_due_date=None: booked)

    offer, loan = loan_offers.accept_offer(session, app, picked.id, actor="patient:x", now=NOW)

    assert offer.status == "accepted"
    assert sibling.status == "void"
    assert loan is booked  # a loan WAS booked
    # accepted terms merged onto the decision (unchanged booking path reads them)
    assert app.decision["amount_cents"] == 600_000


def test_accept_offer_flag_on_does_not_book(monkeypatch):
    """Flag ON: acceptance books NOTHING and advances to agreement_signature."""
    monkeypatch.setattr(loan_offers.settings, "ACTIVATION_BOOKS_LOAN", True)
    picked, sibling = _offer(), _offer()
    session = _FakeSession([picked, sibling])
    app = _application(status="approved")

    import app.services.loan_lifecycle as ll

    def _boom(*a, **k):  # book_loan must NOT be called under the flag
        raise AssertionError("book_loan must not be called when ACTIVATION_BOOKS_LOAN is on")

    monkeypatch.setattr(ll, "book_loan", _boom)

    offer, loan = loan_offers.accept_offer(session, app, picked.id, actor="patient:x", now=NOW)

    assert offer.status == "accepted"
    assert sibling.status == "void"
    assert loan is None  # NO loan booked
    assert app.status == "agreement_signature"  # advanced to the signing stage


# ===========================================================================
# TIER 2 — throwaway UTC-pinned migrated DB: the full flag-ON activation path
# ===========================================================================


def _base_pg_url() -> str:
    base = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+psycopg2://payspyre:dev123@localhost:5432/payspyre_test"
    )
    return base.rsplit("/", 1)[0] if "/" in base.split("@")[-1] else base


@pytest.fixture()
def wave2_db():
    """A fresh, UTC-pinned, migrated Postgres DB — created, upgraded, dropped.

    Never the shared payspyre_test DB. Skips if no local Postgres is reachable.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    base = _base_pg_url()
    db_name = f"wave2_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    try:
        conn = admin.connect()
    except Exception as exc:  # noqa: BLE001 — no local PG: skip this tier
        pytest.skip(f"no local Postgres for throwaway DB: {type(exc).__name__}: {exc}")

    with conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        # The mandate: pin the throwaway DB to UTC so date-only arithmetic is
        # deterministic regardless of the host's local time zone.
        conn.execute(text(f'ALTER DATABASE "{db_name}" SET TimeZone TO \'UTC\''))

    url = f"{base}/{db_name}"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        admin.dispose()


# --- seed helpers ----------------------------------------------------------


def _seed_application(db, *, status="under_review"):
    # No vendor row: vendor_id is nullable on the application, and the ``vendors``
    # table's ORM model has drifted ahead of the migrations (a known repo issue) —
    # activation needs neither a vendor nor the drifted columns.
    from app.models.platform.credit_application import PlatformCreditApplication
    from app.models.platform.credit_product import PlatformCreditProduct
    from app.models.platform.patient import PlatformPatient

    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )
    assert product is not None, "migration 022 should have seeded dental_full_arch_v1"

    patient = PlatformPatient(legal_first_name="Jordan", legal_last_name="Lee")
    db.add(patient)
    db.flush()

    app = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=product.id,
        credit_product_version=1,
        requested_amount_cents=1_800_000,
        requested_amount_source="clinic",
        status=status,
        # Explicit, deterministic booking terms (well under the s.347 cap).
        decision={"outcome": "approved", "amount_cents": 1_800_000,
                  "apr_bps": 1299, "term_months": 24},
    )
    db.add(app)
    db.commit()
    return app


def _sign_application_agreement(db, app):
    """Send + simulate-sign the pre-loan application agreement (simulator mode)."""
    from app.services import application_agreement

    application_agreement.send_agreement_for_application(db, app, actor="admin-1")
    application_agreement.simulate_signing_for_application(db, app, actor="admin-1")
    db.refresh(app)
    assert app.agreement_status == "signed"
    assert app.agreement_signed_at is not None


def _loan_count(db, application_id):
    from app.models.platform.loan import PlatformLoan

    return (
        db.query(PlatformLoan)
        .filter(PlatformLoan.application_id == application_id)
        .count()
    )


def test_flag_on_offers_accept_sign_never_book_then_activate_books(wave2_db, monkeypatch):
    """The whole flag-ON path: no loan until ACTIVATION books it."""
    monkeypatch.setattr(loan_offers.settings, "ACTIVATION_BOOKS_LOAN", True)
    from app.api.v1.endpoints import admin_actions
    from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem

    db = wave2_db
    app = _seed_application(db)

    # 1) APPROVE via offers — creates offers, books NO loan.
    offers = loan_offers.create_offers(
        db, app,
        [OfferSpec(amount_cents=1_800_000, term_months=24, annual_rate_bps=1299)],
        actor="admin-1", now=NOW,
    )
    db.commit()
    assert app.status == "approved"
    assert _loan_count(db, app.id) == 0  # no loan at approval

    # 2) ACCEPT the offer — flag ON: no loan, advance to agreement_signature.
    offer, loan = loan_offers.accept_offer(
        db, app, offers[0].id, actor="patient:x", now=NOW
    )
    db.commit()
    assert loan is None
    assert app.status == "agreement_signature"
    assert _loan_count(db, app.id) == 0  # still no loan at acceptance

    # 3) SIGN the application agreement — still no loan.
    _sign_application_agreement(db, app)
    assert _loan_count(db, app.id) == 0

    # 4) ACTIVATE via maker-checker (a SECOND admin approves) — BOOKS the loan.
    maker = SimpleNamespace(id=uuid.uuid4())
    checker = SimpleNamespace(id=uuid.uuid4())
    req = admin_actions.request_activate(
        app.id, admin_actions.ActionRequestBody(note="go live"), db, maker
    )
    assert req["action"] == "activate"
    result = admin_actions.approve_action(req["pending_action_id"], db, checker)
    assert result["executed"] is True
    assert result["booked_at_activation"] is True

    # The loan now exists, booked at activation, active + virtually disbursed,
    # with the agreement provenance copied and the schedule generated.
    loan_row = (
        db.query(PlatformLoan).filter(PlatformLoan.application_id == app.id).one()
    )
    db.refresh(app)
    assert loan_row.booked_at_activation is True
    assert loan_row.status == "active"
    assert loan_row.disbursement_status == "completed"
    assert loan_row.disbursed_at is not None
    assert loan_row.agreement_status == "signed"
    assert loan_row.agreement_ref == app.agreement_ref
    assert loan_row.agreement_signed_at == app.agreement_signed_at
    assert loan_row.term_months == 24
    n_items = (
        db.query(PlatformLoanScheduleItem)
        .filter(PlatformLoanScheduleItem.loan_id == loan_row.id)
        .count()
    )
    assert n_items == 24  # full amortization schedule generated
    assert app.status == "active"  # application advanced to active


def test_activate_is_idempotent(wave2_db):
    """A double-activate never double-books — the existing loan is returned."""
    from app.services import loan_lifecycle

    db = wave2_db
    app = _seed_application(db, status="agreement_signature")
    _sign_application_agreement(db, app)

    loan1 = loan_lifecycle.activate_loan(db, app, actor="admin-1")
    loan2 = loan_lifecycle.activate_loan(db, app, actor="admin-2")
    assert loan1.id == loan2.id
    assert _loan_count(db, app.id) == 1


def test_activate_requires_signed_agreement(wave2_db):
    """Precondition: an unsigned application cannot be activated (4xx)."""
    from app.services import loan_lifecycle

    db = wave2_db
    app = _seed_application(db, status="agreement_signature")  # agreement NOT signed
    with pytest.raises(loan_lifecycle.ActivationError):
        loan_lifecycle.activate_loan(db, app, actor="admin-1")
    assert _loan_count(db, app.id) == 0


def test_activate_second_approver_must_differ(wave2_db):
    """Maker-checker: the same admin cannot approve their own activate request."""
    from app.api.v1.endpoints import admin_actions

    db = wave2_db
    app = _seed_application(db, status="agreement_signature")
    _sign_application_agreement(db, app)

    maker = SimpleNamespace(id=uuid.uuid4())
    req = admin_actions.request_activate(
        app.id, admin_actions.ActionRequestBody(), db, maker
    )
    with pytest.raises(HTTPException) as exc:
        admin_actions.approve_action(req["pending_action_id"], db, maker)  # same actor
    assert exc.value.status_code == 403
    assert _loan_count(db, app.id) == 0  # nothing booked


def test_activate_approve_rejects_unsigned_application(wave2_db):
    """approve of an ``activate`` on an unsigned file 409s before booking."""
    from app.api.v1.endpoints import admin_actions

    db = wave2_db
    app = _seed_application(db, status="agreement_signature")  # NOT signed

    maker = SimpleNamespace(id=uuid.uuid4())
    checker = SimpleNamespace(id=uuid.uuid4())
    req = admin_actions.request_activate(
        app.id, admin_actions.ActionRequestBody(), db, maker
    )
    with pytest.raises(HTTPException) as exc:
        admin_actions.approve_action(req["pending_action_id"], db, checker)
    assert exc.value.status_code == 409
    assert _loan_count(db, app.id) == 0
