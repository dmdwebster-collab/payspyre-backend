"""Archive workplace (WS-I) — Turnkey video 06 parity.

The Archive is the read-only terminal workplace: every non-active record —
closed APPLICATIONS (rejected / cancelled / expired) and closed LOAN accounts
(repaid / written off / cancelled) — listed with close-reason filters, plus a
FULL FROZEN detail view. Nothing here writes; the underlying immutability is
already enforced upstream (event-sourced statuses, WORM ledger trigger 049,
decision snapshots on the application row).

Close-reason vocabulary (the archive "S" column):

    rejected                   ← application.status = declined
    cancelled                  ← application.status = withdrawn  OR loan.status = cancelled
    expired                    ← application.status = expired    (offer expired)
    bank_verification_expired  ← application.status = expired AND
                                 flow_state.expiry_reason = 'bank_verification'
                                 (populated by the verification-expiry job; an
                                 expired row without the marker files under
                                 plain ``expired``)
    repaid                     ← loan.status = paid_off
    written_off                ← loan.status = charged_off

POLYMORPHIC DETAIL (Dave: "if it was a paid account, it would have the amount
of information that would be in the servicing workplace; if it's just an
application that was rejected, it would have the ... originations" detail):

  * ``detail_kind = 'origination'`` — application records: the application's
    origination data + the frozen decision snapshot. No servicing blocks.
  * ``detail_kind = 'servicing'``  — loan records: full servicing detail
    (schedule roll-up, final immutable-ledger state + balances) PLUS the
    originating application's decision snapshot.

The decision snapshot is frozen AS OF the credit decision (application row:
``decision`` / ``decision_at`` / ``decision_by`` / ``product_config_snapshot``
/ ``credit_product_version``) — "a historical record of why and how we made
the decision"; it never updates with later applications.

Close DATE: applications use ``status_updated_at`` (stamped on every terminal
transition). Loans have no dedicated ``closed_at`` column (known inventory
gap) — ``updated_at`` is the best-available proxy and is documented as such
in the payload (``closed_at_source``).
"""
from __future__ import annotations

from datetime import date
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.loan import Vendor
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import PlatformLoan
from app.models.platform.patient import PlatformPatient

# Terminal status sets (the archive's population).
TERMINAL_APPLICATION_STATUSES = ("declined", "withdrawn", "expired")
TERMINAL_LOAN_STATUSES = ("paid_off", "charged_off", "cancelled")

CLOSE_REASONS = (
    "rejected",
    "cancelled",
    "expired",
    "bank_verification_expired",
    "repaid",
    "written_off",
)

_APP_STATUS_TO_REASON = {"declined": "rejected", "withdrawn": "cancelled"}
_LOAN_STATUS_TO_REASON = {
    "paid_off": "repaid",
    "charged_off": "written_off",
    "cancelled": "cancelled",
}


def close_reason_for_application(status: str, flow_state: Optional[dict]) -> Optional[str]:
    """Pure close-reason derivation for an application row (None = not terminal)."""
    if status in _APP_STATUS_TO_REASON:
        return _APP_STATUS_TO_REASON[status]
    if status == "expired":
        if (flow_state or {}).get("expiry_reason") == "bank_verification":
            return "bank_verification_expired"
        return "expired"
    return None


def close_reason_for_loan(status: str) -> Optional[str]:
    """Pure close-reason derivation for a loan row (None = not terminal)."""
    return _LOAN_STATUS_TO_REASON.get(status)


def _patient_name(p) -> str:
    if p is None:
        return "—"
    parts = [getattr(p, "legal_first_name", None), getattr(p, "legal_last_name", None)]
    return " ".join(x for x in parts if x).strip() or "—"


def list_archive(
    db: Session,
    *,
    close_reason: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """The archive queue: terminal applications + terminal loans, merged and
    sorted by close date (newest first). ``close_reason`` filters to one
    archive status. Read-only; static ORM SQL."""
    if close_reason is not None and close_reason not in CLOSE_REASONS:
        raise ValueError(
            f"unknown close_reason {close_reason!r} (expected one of {CLOSE_REASONS})"
        )

    rows: list[dict] = []

    app_reasons = {"rejected", "cancelled", "expired", "bank_verification_expired"}
    loan_reasons = {"repaid", "written_off", "cancelled"}
    want_apps = close_reason is None or close_reason in app_reasons
    want_loans = close_reason is None or close_reason in loan_reasons

    if want_apps:
        q = (
            db.query(PlatformCreditApplication, PlatformPatient, Vendor.business_name)
            .outerjoin(
                PlatformPatient,
                PlatformCreditApplication.patient_id == PlatformPatient.id,
            )
            .outerjoin(Vendor, PlatformCreditApplication.vendor_id == Vendor.id)
            .filter(
                PlatformCreditApplication.status.in_(TERMINAL_APPLICATION_STATUSES)
            )
        )
        if close_reason == "rejected":
            q = q.filter(PlatformCreditApplication.status == "declined")
        elif close_reason == "cancelled":
            q = q.filter(PlatformCreditApplication.status == "withdrawn")
        elif close_reason in ("expired", "bank_verification_expired"):
            q = q.filter(PlatformCreditApplication.status == "expired")
        for app_row, patient, vendor_name in (
            q.order_by(PlatformCreditApplication.status_updated_at.desc())
            .limit(limit)
            .all()
        ):
            reason = close_reason_for_application(app_row.status, app_row.flow_state)
            if close_reason is not None and reason != close_reason:
                continue  # expired vs bank_verification_expired split
            rows.append(
                {
                    "record_type": "application",
                    "record_id": str(app_row.id),
                    "name": _patient_name(patient),
                    "vendor_name": vendor_name or "—",
                    "amount_cents": app_row.requested_amount_cents,
                    "close_reason": reason,
                    "status": app_row.status,
                    "closed_at": (
                        app_row.status_updated_at.isoformat()
                        if app_row.status_updated_at
                        else None
                    ),
                }
            )

    if want_loans:
        q = (
            db.query(PlatformLoan, PlatformPatient, Vendor.business_name)
            .outerjoin(
                PlatformCreditApplication,
                PlatformLoan.application_id == PlatformCreditApplication.id,
            )
            .outerjoin(
                PlatformPatient,
                PlatformCreditApplication.patient_id == PlatformPatient.id,
            )
            .outerjoin(Vendor, PlatformCreditApplication.vendor_id == Vendor.id)
            .filter(PlatformLoan.status.in_(TERMINAL_LOAN_STATUSES))
        )
        if close_reason == "repaid":
            q = q.filter(PlatformLoan.status == "paid_off")
        elif close_reason == "written_off":
            q = q.filter(PlatformLoan.status == "charged_off")
        elif close_reason == "cancelled":
            q = q.filter(PlatformLoan.status == "cancelled")
        for loan, patient, vendor_name in (
            q.order_by(PlatformLoan.updated_at.desc()).limit(limit).all()
        ):
            rows.append(
                {
                    "record_type": "loan",
                    "record_id": str(loan.id),
                    "name": _patient_name(patient),
                    "vendor_name": vendor_name or "—",
                    "amount_cents": loan.principal_cents,
                    "close_reason": close_reason_for_loan(loan.status),
                    "status": loan.status,
                    "closed_at": loan.updated_at.isoformat() if loan.updated_at else None,
                }
            )

    rows.sort(key=lambda r: r["closed_at"] or "", reverse=True)
    return rows[:limit]


def decision_snapshot_block(application) -> Optional[dict]:
    """The frozen decision snapshot — why and how the decision was made, as of
    THAT application. Never recomputed (Dave: "It does not update if they do a
    new application")."""
    if application is None:
        return None
    return {
        "decision": application.decision,
        "decision_at": (
            application.decision_at.isoformat() if application.decision_at else None
        ),
        "decision_by": application.decision_by,
        "credit_product_version": application.credit_product_version,
        "product_config_snapshot": application.product_config_snapshot,
    }


def _origination_block(application) -> dict:
    """Origination-grade detail (the rejected/cancelled/expired application view)."""
    return {
        "application_id": str(application.id),
        "status": application.status,
        "vendor_id": str(application.vendor_id) if application.vendor_id else None,
        "requested_amount_cents": application.requested_amount_cents,
        "requested_amount_source": application.requested_amount_source,
        "requested_term_months": application.requested_term_months,
        "treatment_cost_cents": application.treatment_cost_cents,
        "insurance_coverage_cents": application.insurance_coverage_cents,
        "down_payment_cents": application.down_payment_cents,
        "provider_name": application.provider_name,
        "created_at": (
            application.created_at.isoformat()
            if getattr(application, "created_at", None)
            else None
        ),
        "closed_at": (
            application.status_updated_at.isoformat()
            if application.status_updated_at
            else None
        ),
    }


def application_archive_detail(db: Session, application_id: UUID) -> Optional[dict]:
    """Frozen detail for a terminal APPLICATION (detail_kind='origination').
    Returns None when the application doesn't exist or is not terminal."""
    application = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if application is None or application.status not in TERMINAL_APPLICATION_STATUSES:
        return None
    return {
        "record_type": "application",
        "detail_kind": "origination",
        "close_reason": close_reason_for_application(
            application.status, application.flow_state
        ),
        "closed_at_source": "status_updated_at",
        "origination": _origination_block(application),
        "decision_snapshot": decision_snapshot_block(application),
    }


def loan_archive_detail(db: Session, loan_id: UUID, *, as_of: Optional[date] = None) -> Optional[dict]:
    """Frozen detail for a terminal LOAN (detail_kind='servicing'): servicing
    roll-up + FINAL immutable-ledger state + the originating application's
    decision snapshot. Returns None when the loan doesn't exist or is active."""
    # Local imports keep this module import-light (loan_servicing pulls the
    # pricing/quote stack).
    from app.services import loan_ledger
    from app.services.loan_servicing import get_loan_status

    loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    if loan is None or loan.status not in TERMINAL_LOAN_STATUSES:
        return None

    application = None
    if loan.application_id is not None:
        application = (
            db.query(PlatformCreditApplication)
            .filter(PlatformCreditApplication.id == loan.application_id)
            .first()
        )

    return {
        "record_type": "loan",
        "detail_kind": "servicing",
        "close_reason": close_reason_for_loan(loan.status),
        # Best-available close date (no dedicated closed_at column — known gap).
        "closed_at_source": "updated_at",
        "closed_at": loan.updated_at.isoformat() if loan.updated_at else None,
        "servicing": get_loan_status(db, loan.id),
        # The final ledger state: every immutable transaction + running
        # balances + the closing balance view (all four buckets, normally 0/0/0/0
        # for a repaid loan; the written-off residual for a charge-off).
        "ledger": loan_ledger.ledger_view(loan, as_of or date.today()),
        "origination": _origination_block(application) if application else None,
        "decision_snapshot": decision_snapshot_block(application),
    }
