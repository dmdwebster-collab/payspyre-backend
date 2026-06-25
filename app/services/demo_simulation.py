"""Dev/staging demo simulation — drive a full application lifecycle in one call.

Packages the EXACT domain services the real flow uses (no HTTP, no faked
internals) so a business user can watch an application go
created → consented → verified → decisioned → loan booked → payment posted →
statement, and SEE the calculation engine produce real amortized numbers.

This is the "fully simulated experience" — it runs entirely on mock adapters
(scores/verifications are synthesised), so it works with zero integration creds.
Mounted only off production (see the dev router gate).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import app.services.consent_service as consent_service
from app.models.platform.credit_application import PlatformCreditApplication  # noqa: F401
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem
from app.models.platform.patient import PlatformPatient
from app.models.platform.verification import PlatformVerification
from app.services import loan_servicing
from app.services.flow_orchestrator import CONSENT_TO_VERIFICATION_TYPE, FlowOrchestrator
from app.services.verifications.mock_dispatcher import MockVerificationDispatcher

# The four verification purposes, in the order the journey runs them. The final
# terminal result triggers the automated decision.
_PURPOSES = ["id_verification", "soft_bureau_pull", "bank_verification", "hard_bureau_pull"]
_BUREAU_TYPES = ("bureau_soft", "bureau_hard")


def run_demo_application(
    db,
    *,
    score: int = 720,
    amount_cents: int = 2_500_000,
    post_payment: bool = True,
) -> dict:
    """Run one full simulated application and return a step-by-step trace.

    score: synthetic bureau score — 720 approves, 640 → manual review, 550 declines.
    Reuses FlowOrchestrator + loan_servicing exactly as production does; the only
    'mock' is the synthesised verification result (MockVerificationDispatcher).
    """
    orch = FlowOrchestrator(db, consent_service, MockVerificationDispatcher())

    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.status == "active")
        .order_by(PlatformCreditProduct.created_at.asc())
        .first()
    )
    if product is None:
        raise ValueError("No active credit product to simulate against — create one first.")

    suffix = uuid.uuid4().hex[:8]
    patient = PlatformPatient(
        legal_first_name="Demo", legal_last_name="Applicant",
        email=f"demo-{suffix}@example.com",
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)

    application = orch.create_application(
        patient_id=patient.id,
        credit_product_id=product.id,
        requested_amount_cents=amount_cents,
        requested_amount_source="clinic",
    )
    db.commit()
    app_id = application.id

    steps = [
        {"step": "application_created", "detail": f"Application for {product.name}, "
                                                  f"{_dollars(amount_cents)} requested"},
    ]

    for purpose in _PURPOSES + ["automated_decision_making"]:
        consent_service.record_consent(db, patient.id, purpose, True, application_id=app_id)
    db.commit()
    steps.append({"step": "consents_granted", "detail": "All required consents recorded"})

    for purpose in _PURPOSES:
        orch.initiate_verification(app_id, purpose)
        db.commit()
    steps.append({"step": "verifications_initiated", "detail": f"{len(_PURPOSES)} verifications started"})

    decision = None
    for purpose in _PURPOSES:
        mapped = CONSENT_TO_VERIFICATION_TYPE[purpose]
        verification = (
            db.query(PlatformVerification)
            .filter(
                PlatformVerification.application_id == app_id,
                PlatformVerification.verification_type == mapped,
                PlatformVerification.status.in_(("pending", "in_progress")),
            )
            .order_by(PlatformVerification.started_at.desc())
            .first()
        )
        if verification is None:
            continue
        override = {"credit_score": score} if mapped in _BUREAU_TYPES else None
        rich = MockVerificationDispatcher().simulate_callback(mapped, "passed", override)["rich_payload"]
        result = orch.handle_verification_result(
            app_id, verification.id,
            vendor_event_id=f"demo-{purpose}-{uuid.uuid4().hex[:8]}",
            result="passed", rich_payload=rich,
        )
        if result.decided:
            decision = result.decision
    db.refresh(application)
    steps.append({"step": "verifications_completed", "detail": "All verifications passed (synthesised)"})
    steps.append({"step": "decision", "detail": f"Automated decision: {application.status.upper()}"})

    trace: dict = {
        "application_id": str(app_id),
        "patient_id": str(patient.id),
        "product": product.name,
        "requested_amount_cents": amount_cents,
        "score_used": score,
        "status": application.status,
        "decision": decision,
        "steps": steps,
    }

    loan = db.query(PlatformLoan).filter(PlatformLoan.application_id == app_id).first()
    if loan is not None:
        # Simulate funding so the demo produces a LIVE, active loan — the dashboard's
        # active book then reflects it. This is a LABELLED simulation; real
        # disbursement (SignNow agreement + Zumrails payout) is not exercised in
        # mock mode, so we advance the lifecycle directly.
        loan.status = "active"
        loan.disbursement_status = "completed"
        loan.disbursed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(loan)

        schedule = (
            db.query(PlatformLoanScheduleItem)
            .filter(PlatformLoanScheduleItem.loan_id == loan.id)
            .order_by(PlatformLoanScheduleItem.installment_number)
            .all()
        )
        steps.append({"step": "loan_booked",
                      "detail": f"{loan.term_months}-month loan, {_dollars(schedule[0].total_cents)}/mo — funded"
                                if schedule else "Loan booked + funded"})

        if post_payment and schedule:
            payment = loan_servicing.record_payment(
                db, loan, schedule[0].total_cents, datetime.now(timezone.utc),
                "demo", external_ref=f"demo-{uuid.uuid4().hex[:8]}",
            )
            db.refresh(loan)
            trace["payment"] = {
                "payment_id": str(payment.id),
                "amount_cents": schedule[0].total_cents,
                "balance_after_cents": loan.principal_balance_cents,
            }
            steps.append({"step": "payment_posted",
                          "detail": f"{_dollars(schedule[0].total_cents)} applied; "
                                    f"balance {_dollars(loan.principal_balance_cents)}"})

            statement = loan_servicing.generate_statement(
                db, loan, (date.today() - timedelta(days=30), date.today())
            )
            trace["statement"] = {
                "opening_balance_cents": statement.opening_balance_cents,
                "principal_paid_cents": statement.principal_paid_cents,
                "interest_paid_cents": statement.interest_paid_cents,
                "closing_balance_cents": statement.closing_balance_cents,
            }
            steps.append({"step": "statement_generated",
                          "detail": f"Principal {_dollars(statement.principal_paid_cents)} + "
                                    f"interest {_dollars(statement.interest_paid_cents)}"})

        # Build the loan snapshot AFTER any payment so principal_balance_cents is the
        # CURRENT balance — consistent with the payment / statement / payoff below.
        total_repayment = sum(s.total_cents for s in schedule)
        trace["loan"] = {
            "loan_id": str(loan.id),
            "status": loan.status,
            "principal_cents": loan.principal_cents,
            "annual_rate_bps": loan.annual_rate_bps,
            "term_months": loan.term_months,
            "principal_balance_cents": loan.principal_balance_cents,
            "installments": len(schedule),
            "monthly_payment_cents": schedule[0].total_cents if schedule else None,
            "total_repayment_cents": total_repayment,
            "total_interest_cents": total_repayment - loan.principal_cents,
        }

        payoff = loan_servicing.compute_payoff(db, loan, date.today())
        trace["payoff"] = {
            "payoff_cents": payoff.payoff_cents,
            "principal_cents": payoff.principal_cents,
            "accrued_interest_cents": payoff.accrued_interest_cents,
        }

    return trace


def _dollars(cents: int | None) -> str:
    if cents is None:
        return "—"
    return f"${cents / 100:,.2f}"
