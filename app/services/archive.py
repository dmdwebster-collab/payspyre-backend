"""Archive workplace (WS-I) — Turnkey video 06 parity.

The Archive is the read-only terminal workplace: every non-active record —
closed APPLICATIONS (rejected / cancelled / expired) and closed LOAN accounts
(repaid / written off / cancelled) — listed with close-reason filters, plus a
FULL FROZEN detail view. Nothing here writes; the underlying immutability is
already enforced upstream (event-sourced statuses, WORM ledger trigger 049,
decision snapshots on the application row).

Close-reason vocabulary (the archive "S" column) — REGISTRY-DRIVEN.

The vocabulary is no longer a hand-written list. It is derived from Dave's
status registry (``app.services.application_status``): every member of
``CLOSED_STATUSES`` (the six closed states hanging off Active) and
``OFF_MODEL_TERMINALS`` (declined / cancelled / expired) must appear in
``_CANONICAL_TO_REASON`` or this module raises at import — the two lists can no
longer drift. Adding a closed status to the registry is what adds an Archive
chip; the UI renders chips from the ``close_reasons`` array we return.

    rejected                   ← canonical declined     (engine: declined)
    cancelled                  ← canonical cancelled    (engine: withdrawn,
                                 or loan.status = cancelled)
    expired                    ← canonical expired      (offer expired)
    bank_verification_expired  ← canonical expired AND
                                 flow_state.expiry_reason = 'bank_verification'
                                 (an archive-only REFINEMENT of ``expired``,
                                 populated by the verification-expiry job; an
                                 expired row without the marker files under
                                 plain ``expired``)
    repaid                     ← canonical repaid      / loan.status = paid_off
    written_off                ← canonical written_off / loan.status = charged_off
    renewed                    ← canonical renewed
    refinanced                 ← canonical refinanced
    transferred                ← canonical transferred
    settlement                 ← canonical settlement

The last four have no ``platform_loan_status`` enum value — the loan enum only
knows paid_off / charged_off / cancelled. They are recorded on the APPLICATION
(``flow_orchestrator.mark_closed``), so a loan whose application sits in one of
Dave's closed states is archived under that reason even when its own status is
still ``active``. The application status wins whenever it names a closed state.

``rejected`` is kept as the wire key for the canonical ``declined`` status: the
Archive UI already ships that key, and the API contract is additive-only.

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
transition). Loans carry a real ``closed_at`` since migration 069, stamped by
``stamp_loan_closed`` from the path that closes them and backfilled for older
rows; ``updated_at`` remains the fallback for anything the backfill could not
reach. Every row reports its provenance in ``closed_at_source``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.loan import Vendor
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import PlatformLoan
from app.models.platform.patient import PlatformPatient
from app.models.user import User
from app.services.application_status import (
    CLOSED_STATUSES,
    OFF_MODEL_TERMINALS,
    STATUS_REGISTRY,
    CanonicalStatus,
    canonical_for,
)

# ---------------------------------------------------------------------------
# Vocabulary — derived from Dave's status registry (see the module docstring)
# ---------------------------------------------------------------------------

#: Canonical closed/terminal status -> the Archive's wire key for it.
#: ``declined`` keeps the legacy wire key ``rejected`` (already shipped in the UI).
_CANONICAL_TO_REASON: dict[CanonicalStatus, str] = {
    CanonicalStatus.DECLINED: "rejected",
    CanonicalStatus.CANCELLED: "cancelled",
    CanonicalStatus.EXPIRED: "expired",
    CanonicalStatus.REPAID: "repaid",
    CanonicalStatus.RENEWED: "renewed",
    CanonicalStatus.REFINANCED: "refinanced",
    CanonicalStatus.TRANSFERRED: "transferred",
    CanonicalStatus.SETTLEMENT: "settlement",
    CanonicalStatus.WRITTEN_OFF: "written_off",
}

#: Every registry status that closes a file. Order is the registry's flow order.
_REGISTRY_CLOSED: tuple[CanonicalStatus, ...] = (*OFF_MODEL_TERMINALS, *CLOSED_STATUSES)

_unmapped = [s.value for s in _REGISTRY_CLOSED if s not in _CANONICAL_TO_REASON]
if _unmapped:  # pragma: no cover - import-time drift guard
    raise RuntimeError(
        "app.services.archive is out of sync with the status registry: no archive "
        f"close reason for {_unmapped}. Add it to _CANONICAL_TO_REASON."
    )

#: The archive-only refinement of ``expired`` (not a registry status: the
#: registry has one Expired, the Archive splits it by expiry cause).
BANK_VERIFICATION_EXPIRED = "bank_verification_expired"

#: Wire vocabulary for the ``S`` column / ``All statuses`` filter. Registry
#: order, with the expiry refinement immediately after its parent.
CLOSE_REASONS: tuple[str, ...] = tuple(
    reason
    for status in _REGISTRY_CLOSED
    for reason in (
        (_CANONICAL_TO_REASON[status], BANK_VERIFICATION_EXPIRED)
        if status is CanonicalStatus.EXPIRED
        else (_CANONICAL_TO_REASON[status],)
    )
)

#: reason -> {value,label,canonical_status,sources} for chip rendering.
CLOSE_REASON_OPTIONS: tuple[dict, ...] = tuple(
    {
        "value": reason,
        "label": (
            "Expired (bank verification)"
            if reason == BANK_VERIFICATION_EXPIRED
            else STATUS_REGISTRY[status].label
        ),
        "canonical_status": status.value,
    }
    for status in _REGISTRY_CLOSED
    for reason in (
        (_CANONICAL_TO_REASON[status], BANK_VERIFICATION_EXPIRED)
        if status is CanonicalStatus.EXPIRED
        else (_CANONICAL_TO_REASON[status],)
    )
)

#: reason -> canonical status (the refinement folds back onto EXPIRED).
_REASON_TO_CANONICAL: dict[str, CanonicalStatus] = {
    **{reason: status for status, reason in _CANONICAL_TO_REASON.items()},
    BANK_VERIFICATION_EXPIRED: CanonicalStatus.EXPIRED,
}

# Terminal status sets (the archive's population).
TERMINAL_APPLICATION_STATUSES = ("declined", "withdrawn", "expired")
TERMINAL_LOAN_STATUSES = ("paid_off", "charged_off", "cancelled")
#: Engine values of Dave's six closed states, as written by
#: ``flow_orchestrator.mark_closed`` onto the APPLICATION.
CLOSED_APPLICATION_STATUSES: tuple[str, ...] = tuple(
    STATUS_REGISTRY[s].engine_statuses[0] for s in CLOSED_STATUSES
)

#: Reasons served by the application half of the queue…
APPLICATION_REASONS: frozenset[str] = frozenset(
    {"rejected", "cancelled", "expired", BANK_VERIFICATION_EXPIRED}
)
#: …and by the loan half (``cancelled`` is served by both).
LOAN_REASONS: frozenset[str] = frozenset(
    {"repaid", "written_off", "cancelled"}
    | {_CANONICAL_TO_REASON[s] for s in CLOSED_STATUSES}
)

_LOAN_STATUS_TO_REASON = {
    "paid_off": "repaid",
    "charged_off": "written_off",
    "cancelled": "cancelled",
}

#: Supported sort keys for the queue (Dave's Archive sorts on ID and Close date).
SORT_FIELDS: tuple[str, ...] = ("closed_at", "id")

#: ``assignee_id=unassigned`` — Dave's "All assignations" filter, narrowed to
#: the files nobody owns.
UNASSIGNED = "unassigned"


def close_reason_for_application(status: str, flow_state: Optional[dict]) -> Optional[str]:
    """Pure close-reason derivation for an application row (None = not terminal).

    Handles the legacy engine values (``declined``/``withdrawn``/``expired``)
    AND Dave's six closed states, which the flow orchestrator writes onto the
    application. Registry-driven: an unmapped status returns ``None`` rather
    than raising, so an enum addition can never 500 the queue.
    """
    if status == "expired" and (flow_state or {}).get("expiry_reason") == "bank_verification":
        return BANK_VERIFICATION_EXPIRED
    canonical = canonical_for(status)
    if canonical is None:
        return None
    return _CANONICAL_TO_REASON.get(canonical)


def close_reason_for_loan(
    status: str, application_status: Optional[str] = None
) -> Optional[str]:
    """Pure close-reason derivation for a loan row (None = not terminal).

    The APPLICATION status wins when it names one of Dave's closed states:
    ``renewed`` / ``refinanced`` / ``transferred`` / ``settlement`` have no
    ``platform_loan_status`` enum value, so the loan column alone cannot
    express them.
    """
    if application_status is not None:
        canonical = canonical_for(application_status)
        if canonical in CLOSED_STATUSES:
            return _CANONICAL_TO_REASON[canonical]
    return _LOAN_STATUS_TO_REASON.get(status)


def stamp_loan_closed(loan: PlatformLoan, *, when: Optional[datetime] = None) -> None:
    """Stamp the real close timestamp on a loan reaching a terminal status.

    Called from the paths that close a loan (payoff, charge-off). Idempotent:
    an already-stamped loan keeps its first close date, so a re-run or a second
    terminal write cannot move it. No commit — the caller owns the unit of work.
    """
    # ``getattr`` (not attribute access): the servicing tests drive the payoff
    # path with lightweight in-memory loan doubles that carry only the columns
    # under test, and a stamping helper must never be the reason one breaks.
    if getattr(loan, "closed_at", None) is not None:
        return
    loan.closed_at = when or datetime.now(timezone.utc)
    loan.closed_at_source = "transition"


def _patient_name(p) -> str:
    if p is None:
        return "—"
    parts = [getattr(p, "legal_first_name", None), getattr(p, "legal_last_name", None)]
    return " ".join(x for x in parts if x).strip() or "—"


def _assignee_block(user_id, first_name, last_name, email) -> Optional[dict]:
    """The Archive's ``A`` column: the application's assignee (WS-E), or None."""
    if user_id is None:
        return None
    name = " ".join(x for x in (first_name, last_name) if x).strip()
    return {
        "user_id": str(user_id),
        "name": name or (email or "—"),
        "email": email,
        "initials": "".join(x[0].upper() for x in (first_name, last_name) if x) or "?",
    }


def _apply_assignee_filter(q, assignee_id: Optional[Any]):
    """Dave's ``All assignations`` chip, narrowed. ``UNASSIGNED`` selects the
    files nobody owns; anything else is a user id."""
    if assignee_id is None:
        return q
    if assignee_id == UNASSIGNED:
        return q.filter(PlatformCreditApplication.assigned_to_user_id.is_(None))
    return q.filter(PlatformCreditApplication.assigned_to_user_id == assignee_id)


#: The loan's close timestamp: the real column, falling back to the pre-069
#: proxy for rows the backfill could not reach.
_LOAN_CLOSED_AT = func.coalesce(PlatformLoan.closed_at, PlatformLoan.updated_at)


def _application_query(db: Session, close_reason: Optional[str], assignee_id):
    q = (
        db.query(
            PlatformCreditApplication,
            PlatformPatient,
            Vendor.business_name,
            User.first_name,
            User.last_name,
            User.email,
        )
        .outerjoin(
            PlatformPatient,
            PlatformCreditApplication.patient_id == PlatformPatient.id,
        )
        .outerjoin(Vendor, PlatformCreditApplication.vendor_id == Vendor.id)
        .outerjoin(User, PlatformCreditApplication.assigned_to_user_id == User.id)
        .filter(PlatformCreditApplication.status.in_(TERMINAL_APPLICATION_STATUSES))
    )
    # The expired/bank_verification_expired split is a SQL filter (not a
    # post-filter) so ``total`` and the page window stay exact.
    bank_verification_expiry = (
        PlatformCreditApplication.flow_state["expiry_reason"].astext
        == "bank_verification"
    )
    if close_reason == "rejected":
        q = q.filter(PlatformCreditApplication.status == "declined")
    elif close_reason == "cancelled":
        q = q.filter(PlatformCreditApplication.status == "withdrawn")
    elif close_reason == "expired":
        q = q.filter(
            PlatformCreditApplication.status == "expired",
            or_(
                PlatformCreditApplication.flow_state["expiry_reason"].astext.is_(None),
                ~bank_verification_expiry,
            ),
        )
    elif close_reason == BANK_VERIFICATION_EXPIRED:
        q = q.filter(
            PlatformCreditApplication.status == "expired",
            bank_verification_expiry,
        )
    return _apply_assignee_filter(q, assignee_id)


def _loan_query(db: Session, close_reason: Optional[str], assignee_id):
    q = (
        db.query(
            PlatformLoan,
            PlatformPatient,
            Vendor.business_name,
            PlatformCreditApplication.status,
            PlatformCreditApplication.assigned_to_user_id,
            User.first_name,
            User.last_name,
            User.email,
        )
        .outerjoin(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .outerjoin(
            PlatformPatient,
            PlatformCreditApplication.patient_id == PlatformPatient.id,
        )
        .outerjoin(Vendor, PlatformCreditApplication.vendor_id == Vendor.id)
        .outerjoin(User, PlatformCreditApplication.assigned_to_user_id == User.id)
        .filter(
            or_(
                PlatformLoan.status.in_(TERMINAL_LOAN_STATUSES),
                PlatformCreditApplication.status.in_(CLOSED_APPLICATION_STATUSES),
            )
        )
    )
    if close_reason is not None:
        canonical = _REASON_TO_CANONICAL[close_reason]
        if canonical in CLOSED_STATUSES:
            engine = STATUS_REGISTRY[canonical].engine_statuses[0]
            loan_status = {
                "repaid": "paid_off",
                "written_off": "charged_off",
            }.get(close_reason)
            # The application status wins, so a reason with a loan-status
            # equivalent matches EITHER the app status OR (loan status AND the
            # app not naming a different closed state).
            if loan_status is not None:
                q = q.filter(
                    or_(
                        PlatformCreditApplication.status == engine,
                        (PlatformLoan.status == loan_status)
                        & or_(
                            PlatformCreditApplication.status.is_(None),
                            ~PlatformCreditApplication.status.in_(
                                CLOSED_APPLICATION_STATUSES
                            ),
                        ),
                    )
                )
            else:
                q = q.filter(PlatformCreditApplication.status == engine)
        elif close_reason == "cancelled":
            q = q.filter(
                PlatformLoan.status == "cancelled",
                or_(
                    PlatformCreditApplication.status.is_(None),
                    ~PlatformCreditApplication.status.in_(CLOSED_APPLICATION_STATUSES),
                ),
            )
    return _apply_assignee_filter(q, assignee_id)


def _sorted(q, *, entity, sort: str, descending: bool):
    if sort == "id":
        column = entity.id
    else:
        column = (
            _LOAN_CLOSED_AT
            if entity is PlatformLoan
            else PlatformCreditApplication.status_updated_at
        )
    return q.order_by(column.desc() if descending else column.asc())


def list_archive(
    db: Session,
    *,
    close_reason: Optional[str] = None,
    assignee_id: Optional[Any] = None,
    sort: str = "closed_at",
    order: str = "desc",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """The archive queue, SERVER-PAGINATED: terminal applications + terminal
    loans, merged, sorted, windowed to ``[offset, offset+limit)``, with an exact
    ``total`` so the UI can render "N record(s) · page X of Y".

    Both halves are pulled sorted and capped at ``offset + limit`` rows, then
    merged and sliced — a two-way merge of sorted sources, so the window is
    exact without a UNION. Every filter is applied in SQL (nothing is dropped
    after the fetch), which is what keeps ``total`` and the window consistent.

    Read-only; static ORM SQL (bandit B608 clean).
    """
    if close_reason is not None and close_reason not in CLOSE_REASONS:
        raise ValueError(
            f"unknown close_reason {close_reason!r} (expected one of {CLOSE_REASONS})"
        )
    if sort not in SORT_FIELDS:
        raise ValueError(f"unknown sort {sort!r} (expected one of {SORT_FIELDS})")
    if order not in ("asc", "desc"):
        raise ValueError(f"unknown order {order!r} (expected 'asc' or 'desc')")

    descending = order == "desc"
    want_apps = close_reason is None or close_reason in APPLICATION_REASONS
    want_loans = close_reason is None or close_reason in LOAN_REASONS
    window = offset + limit

    rows: list[dict] = []
    total = 0

    if want_apps:
        q = _application_query(db, close_reason, assignee_id)
        total += q.count()
        for app_row, patient, vendor_name, first, last, email in (
            _sorted(q, entity=PlatformCreditApplication, sort=sort, descending=descending)
            .limit(window)
            .all()
        ):
            rows.append(
                {
                    "record_type": "application",
                    "record_id": str(app_row.id),
                    "name": _patient_name(patient),
                    "vendor_name": vendor_name or "—",
                    "amount_cents": app_row.requested_amount_cents,
                    "close_reason": close_reason_for_application(
                        app_row.status, app_row.flow_state
                    ),
                    "status": app_row.status,
                    "closed_at": (
                        app_row.status_updated_at.isoformat()
                        if app_row.status_updated_at
                        else None
                    ),
                    "closed_at_source": "status_updated_at",
                    "assignee": _assignee_block(
                        app_row.assigned_to_user_id, first, last, email
                    ),
                }
            )

    if want_loans:
        q = _loan_query(db, close_reason, assignee_id)
        total += q.count()
        for loan, patient, vendor_name, app_status, assigned_id, first, last, email in (
            _sorted(q, entity=PlatformLoan, sort=sort, descending=descending)
            .limit(window)
            .all()
        ):
            closed_at = loan.closed_at or loan.updated_at
            rows.append(
                {
                    "record_type": "loan",
                    "record_id": str(loan.id),
                    "name": _patient_name(patient),
                    "vendor_name": vendor_name or "—",
                    "amount_cents": loan.principal_cents,
                    "close_reason": close_reason_for_loan(loan.status, app_status),
                    "status": loan.status,
                    "closed_at": closed_at.isoformat() if closed_at else None,
                    # 'transition' / 'backfill_*' when real; 'updated_at' when
                    # we are still falling back to the pre-069 proxy.
                    "closed_at_source": (
                        loan.closed_at_source
                        if loan.closed_at is not None
                        else "updated_at"
                    ),
                    "assignee": _assignee_block(assigned_id, first, last, email),
                }
            )

    if sort == "id":
        rows.sort(key=lambda r: r["record_id"], reverse=descending)
    elif descending:
        # NULL close dates sort LAST either way (SQL's NULLS LAST on DESC).
        rows.sort(key=lambda r: (r["closed_at"] is not None, r["closed_at"] or ""), reverse=True)
    else:
        rows.sort(key=lambda r: (r["closed_at"] is None, r["closed_at"] or ""))

    return {
        "records": rows[offset : offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "order": order,
    }


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
    if loan is None:
        return None

    application = None
    if loan.application_id is not None:
        application = (
            db.query(PlatformCreditApplication)
            .filter(PlatformCreditApplication.id == loan.application_id)
            .first()
        )
    application_status = application.status if application is not None else None

    # Archived when the loan itself is terminal OR its application names one of
    # Dave's six closed states (renewed / refinanced / transferred / settlement
    # have no loan-status equivalent).
    if (
        loan.status not in TERMINAL_LOAN_STATUSES
        and application_status not in CLOSED_APPLICATION_STATUSES
    ):
        return None

    closed_at = loan.closed_at or loan.updated_at
    return {
        "record_type": "loan",
        "detail_kind": "servicing",
        "close_reason": close_reason_for_loan(loan.status, application_status),
        # 'transition' / 'backfill_*' once migration 069 has a value; 'updated_at'
        # while we are still falling back to the pre-069 proxy.
        "closed_at_source": (
            loan.closed_at_source if loan.closed_at is not None else "updated_at"
        ),
        "closed_at": closed_at.isoformat() if closed_at else None,
        "servicing": get_loan_status(db, loan.id),
        # The final ledger state: every immutable transaction + running
        # balances + the closing balance view (all four buckets, normally 0/0/0/0
        # for a repaid loan; the written-off residual for a charge-off).
        "ledger": loan_ledger.ledger_view(loan, as_of or date.today()),
        "origination": _origination_block(application) if application else None,
        "decision_snapshot": decision_snapshot_block(application),
    }
