"""GOLDEN END-TO-END: the platform's loan lifecycle after the Wave 6 cutover.

This is the owner's exact flow, run against a REAL migrated Postgres, with the
REAL default configuration — ``ACTIVATION_BOOKS_LOAN`` is NOT monkeypatched
anywhere in this file. If someone flips the default back to ``False``, this test
fails, which is the point: it is the executable statement of what the platform
does now.

    application
      → approve (staff issues offers)          … assert NO platform_loans row
      → offer presented to the borrower        … assert NO loan
      → borrower ACCEPTS (borrower endpoint)   … assert NO loan
      → agreement SENT on the application      … assert NO loan
      → borrower SIGNS (Simulate Signing)      … assert NO loan, status 'signed'
      → ACTIVATE via maker-checker (2 admins)  … the loan is created HERE
      → assert loan exists, active, booked_at_activation, agreement provenance
        copied, full amortization schedule, application 'active'

Plus the Wave 6 servicing gate: "Make a Payment" is available on the activated
loan and REJECTED on a loan that is not active yet.

Every step goes through the same entry point the product uses: the borrower
steps through the applicant API endpoint functions (auth/scope included), the
activation through the admin maker-checker endpoints — no shortcuts through
private helpers.

DB: a throwaway, UTC-pinned, migrated database created and dropped per test. It
NEVER touches the shared ``payspyre_test`` DB. Skipped when no local Postgres is
reachable (CI provides one).

Run just this file:
    source .venv/bin/activate && \
        python -m pytest tests/test_activation_golden_lifecycle.py -p no:warnings -q
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.services.loan_offers import OfferSpec

NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Throwaway, UTC-pinned, migrated Postgres
# ---------------------------------------------------------------------------


def _base_pg_url() -> str:
    base = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+psycopg2://payspyre:dev123@localhost:5432/payspyre_test"
    )
    return base.rsplit("/", 1)[0] if "/" in base.split("@")[-1] else base


@pytest.fixture()
def golden_db():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    base = _base_pg_url()
    db_name = f"golden_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    try:
        conn = admin.connect()
    except Exception as exc:  # noqa: BLE001 — no local PG: skip
        pytest.skip(f"no local Postgres for throwaway DB: {type(exc).__name__}: {exc}")

    with conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
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


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_application(db, *, status="under_review"):
    """A patient + an application on the seeded dental product.

    No vendor row: vendor_id is nullable and the ``vendors`` ORM model has
    drifted ahead of the migrations (known repo issue); this lifecycle needs
    neither.
    """
    from app.models.platform.credit_application import PlatformCreditApplication
    from app.models.platform.credit_product import PlatformCreditProduct
    from app.models.platform.patient import PlatformPatient

    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )
    assert product is not None, "migration 022 should have seeded dental_full_arch_v1"

    patient = PlatformPatient(legal_first_name="Dana", legal_last_name="Okafor")
    db.add(patient)
    db.flush()

    application = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=product.id,
        credit_product_version=1,
        requested_amount_cents=1_800_000,
        requested_amount_source="clinic",
        status=status,
        decision={"outcome": "approved", "amount_cents": 1_800_000,
                  "apr_bps": 1299, "term_months": 36},
    )
    db.add(application)
    db.commit()
    return application


def _loan_count(db, application_id) -> int:
    from app.models.platform.loan import PlatformLoan

    return (
        db.query(PlatformLoan)
        .filter(PlatformLoan.application_id == application_id)
        .count()
    )


def _claims(application):
    """The borrower's JWT claims for their own application."""
    from app.api.applicant.v1.deps import ApplicantClaims

    return ApplicantClaims(
        patient_id=application.patient_id, app_ids=[application.id]
    )


# ---------------------------------------------------------------------------
# THE GOLDEN LIFECYCLE
# ---------------------------------------------------------------------------


def test_default_config_books_the_loan_at_activation():
    """The cutover is the DEFAULT — not an opt-in experiment."""
    assert settings.ACTIVATION_BOOKS_LOAN is True


def test_golden_lifecycle_application_to_active_loan(golden_db):
    """application → approve → accept → sign → ACTIVATE. No loan until the end."""
    from app.api.applicant.v1.endpoints import agreement as agreement_ep
    from app.api.applicant.v1.endpoints import offers as offers_ep
    from app.api.v1.endpoints import admin_actions
    from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem
    from app.services import loan_offers

    db = golden_db
    application = _seed_application(db)
    claims = _claims(application)

    # ---- 1) APPROVE: staff issues the offer. NO loan is booked. ------------
    created = loan_offers.create_offers(
        db,
        application,
        [OfferSpec(amount_cents=1_800_000, term_months=36, annual_rate_bps=1299)],
        actor="admin-maker",
        now=NOW,
    )
    db.commit()
    assert application.status == "approved"
    assert _loan_count(db, application.id) == 0, "approval must NOT book a loan"

    # ---- 2) OFFER PRESENTED: the borrower sees it on their dashboard. ------
    listed = offers_ep.list_offers(application.id, claims, db)
    assert [o.offer_id for o in listed.offers] == [created[0].id]
    assert listed.offers[0].amount_cents == 1_800_000
    assert listed.offers[0].status == "offered"
    assert _loan_count(db, application.id) == 0

    # ---- 3) BORROWER ACCEPTS (borrower endpoint). STILL no loan. -----------
    accepted = offers_ep.accept_offer(
        application.id,
        created[0].id,
        offers_ep.AcceptOfferBody(confirm=True),
        claims,
        db,
    )
    db.refresh(application)
    assert accepted.loan_id is None, "acceptance must NOT book a loan"
    assert _loan_count(db, application.id) == 0
    assert application.status == "agreement_signature"

    # ---- 4) AGREEMENT SENT on the APPLICATION (by the accept step). --------
    assert accepted.agreement_status == "sent"
    assert application.agreement_status == "sent"
    assert application.agreement_ref is not None
    assert _loan_count(db, application.id) == 0

    # ---- 5) BORROWER SIGNS (Simulate Signing). STILL no loan. --------------
    signed = agreement_ep.simulate_sign(application.id, claims, db)
    db.refresh(application)
    assert signed.agreement_status == "signed"
    assert application.agreement_status == "signed"
    assert application.agreement_signed_at is not None
    assert _loan_count(db, application.id) == 0, "signing must NOT book a loan"

    # ---- 6) ACTIVATE via maker-checker — TWO DIFFERENT admins. -------------
    maker = SimpleNamespace(id=uuid.uuid4())
    checker = SimpleNamespace(id=uuid.uuid4())
    request = admin_actions.request_activate(
        application.id, admin_actions.ActionRequestBody(note="owner e2e"), db, maker
    )
    assert request["action"] == "activate"
    assert _loan_count(db, application.id) == 0, "requesting activation books nothing"

    result = admin_actions.approve_action(request["pending_action_id"], db, checker)
    assert result["executed"] is True
    assert result["booked_at_activation"] is True
    assert result["loan_status"] == "active"

    # ---- 7) THE LOAN NOW EXISTS -------------------------------------------
    loan = db.query(PlatformLoan).filter(
        PlatformLoan.application_id == application.id
    ).one()
    db.refresh(application)

    assert loan.status == "active"
    assert loan.booked_at_activation is True
    # Virtual disbursement: activation IS the disbursement (no separate leg).
    assert loan.disbursement_status == "completed"
    assert loan.disbursed_at is not None
    # Agreement provenance copied from the application it was booked from.
    assert loan.agreement_status == "signed"
    assert loan.agreement_ref == application.agreement_ref
    assert loan.agreement_signed_at == application.agreement_signed_at
    # Booked on the ACCEPTED offer's terms.
    assert loan.principal_cents == 1_800_000
    assert loan.term_months == 36
    assert loan.annual_rate_bps == 1299
    # Full amortization schedule generated.
    schedule = (
        db.query(PlatformLoanScheduleItem)
        .filter(PlatformLoanScheduleItem.loan_id == loan.id)
        .order_by(PlatformLoanScheduleItem.installment_number)
        .all()
    )
    assert len(schedule) == 36
    assert schedule[0].total_cents > 0
    # The application is live.
    assert application.status == "active"

    # ---- 8) SERVICING is now open on the ACTIVE loan ----------------------
    from app.services import loan_payments

    options = loan_payments.payment_options(db, loan)
    assert options["payable"] is True
    assert "regular" in options["modes"]


def test_servicing_actions_are_refused_before_activation(golden_db):
    """"Make a Payment" must not work on a loan that is not active yet.

    A GRANDFATHERED loan (booked at approval under the legacy path, still
    ``pending_disbursement``) is the only way a pre-active loan row can exist
    after the cutover — and it must be refused by both payment entry points.
    """
    from app.api.v1.endpoints import admin_actions
    from app.services import loan_payments, loan_servicing

    db = golden_db
    application = _seed_application(db, status="approved")
    loan = loan_servicing.create_loan_from_application(db, application)
    db.commit()
    assert loan.status == "pending_disbursement"
    assert loan.booked_at_activation is False  # grandfathered cohort

    # Borrower Pay Now: no modes offered, and initiation is refused.
    options = loan_payments.payment_options(db, loan)
    assert options["payable"] is False
    assert options["modes"] == []
    assert "pending activation" in options["not_payable_reason"]

    with pytest.raises(loan_payments.PaymentValidationError) as exc:
        loan_payments.initiate_payment(db, loan, 10_000)
    assert "pending activation" in str(exc.value)

    # Admin manual posting: 409, before anything is written.
    admin = SimpleNamespace(id=uuid.uuid4(), roles=[])
    with pytest.raises(HTTPException) as http_exc:
        admin_actions.record_payment(
            loan.id,
            admin_actions.PaymentBody(amount_cents=10_000, method="manual"),
            db,
            admin,
        )
    assert http_exc.value.status_code == 409
    assert "pending activation" in http_exc.value.detail


def test_work_queue_shows_the_new_pre_loan_backlog(golden_db):
    """The flip empties ``pending_disbursement`` — the admin home screen must
    surface the queues that replaced it, or staff see nothing to do."""
    from app.api.v1.endpoints import admin_dashboard
    from app.services import application_agreement

    db = golden_db

    # (a) approved, no offers issued yet → awaiting_offers.
    _seed_application(db, status="approved")
    # (b) agreement signed, not activated yet → pending_activation.
    ready = _seed_application(db, status="agreement_signature")
    application_agreement.send_agreement_for_application(db, ready, actor="admin-1")
    application_agreement.simulate_signing_for_application(db, ready, actor="admin-1")
    db.commit()

    overview = admin_dashboard.overview(db, None)
    assert overview.work_queue.pending_disbursement == 0  # nothing books early now
    assert overview.work_queue.awaiting_offers == 1
    assert overview.work_queue.pending_activation == 1
    assert overview.loan_book.total == 0


def test_grandfathered_loan_still_services_once_active(golden_db):
    """The gate must not touch loans that are genuinely active."""
    from app.services import loan_payments, loan_servicing

    db = golden_db
    application = _seed_application(db, status="approved")
    loan = loan_servicing.create_loan_from_application(db, application)
    loan.status = "active"
    loan.disbursement_status = "completed"
    loan.disbursed_at = datetime.now(timezone.utc)
    db.commit()

    options = loan_payments.payment_options(db, loan)
    assert options["payable"] is True
    assert options["not_payable_reason"] is None
    assert "regular" in options["modes"] and "payoff" in options["modes"]
