"""Collections work-surface (WS-C full parity) — collector assignment,
action plans, promise-to-pay, header math, insolvency portfolio.

Dave's collections modernization (04__WP_Collections), built ON the existing
primitives — the month-end bucket state machine (``delinquency_buckets``,
migration 053), the actuals ledger + daily-simple-interest engine
(``loan_ledger`` / ``interest_engine``, 049) and the repayment modes /
add-on fee bucket (050). NOTHING here modifies ``record_payment`` or the
interest engine — money writes go THROUGH the existing hardened entry points
(trustee payments call ``loan_servicing.record_payment``; the maintenance fee
appends an immutable add-on ledger ``fee`` row exactly like the NSF-fee
primitive in ``auto_collection``).

Split of responsibilities (flow_engine / delinquency_buckets idiom):

  * PURE CORE — tier gating, alpha-range matching, promise evaluation,
    queue sort keys: no DB, no I/O, no clock reads.
  * ORCHESTRATION — assignment writes, promise-status refresh, trustee
    payments, the monthly maintenance-fee hook. Functions add + flush only;
    CALLERS own commits (endpoint idiom).

POLICY (flagged for Dave, all in one place):

  * ``JUNIOR_ALLOWED_BUCKETS`` — Dave: "a junior collector is going to deal
    with something that is maybe current month late or potentially 30 days
    late and then anything beyond that is going to be somebody that is a more
    senior collector". Encoded as: junior may work current /
    current_month_late / pot_30; a senior may work everything.
  * Promise window — payments qualify from the promise's creation date
    through its promised date (inclusive). No grace days (knob available).
  * ``no_late_fees`` on a promise is a RECORDED flag; automated late-fee
    waiver plugs in when the fee engine lands.

Money is integer cents. No PII in event payloads beyond staff ids.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.platform.collections_work import (
    PlatformCollectorAssignment,
    PlatformInsolvencyMaintenanceFee,
    PlatformPromiseToPay,
)
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan, PlatformLoanTransaction
from app.services import loan_ledger, loan_servicing
from app.services.delinquency_buckets import (
    BUCKET_CURRENT,
    BUCKET_CURRENT_MONTH_LATE,
    BUCKET_POT_30,
    INSOLVENCY_STATUSES,
    NOT_OWED_STATUSES,
    effective_bucket,
    month_start,
)

# ---------------------------------------------------------------------------
# Vocabulary / events
# ---------------------------------------------------------------------------

TIER_JUNIOR = "junior"
TIER_SENIOR = "senior"
TIERS = (TIER_JUNIOR, TIER_SENIOR)

ASSIGN_METHOD_MANUAL = "manual"
ASSIGN_METHOD_BULK_VENDOR = "bulk_vendor"
ASSIGN_METHOD_BULK_ALPHA = "bulk_alpha"
ASSIGN_METHOD_BULK_BUCKET = "bulk_bucket"
ASSIGN_METHODS = (
    ASSIGN_METHOD_MANUAL,
    ASSIGN_METHOD_BULK_VENDOR,
    ASSIGN_METHOD_BULK_ALPHA,
    ASSIGN_METHOD_BULK_BUCKET,
)

# Dave's junior/senior split — the buckets a JUNIOR collector may work.
JUNIOR_ALLOWED_BUCKETS = (
    BUCKET_CURRENT,
    BUCKET_CURRENT_MONTH_LATE,
    BUCKET_POT_30,
)

COLLECTOR_ASSIGNED_EVENT = "collector_assigned"
COLLECTOR_UNASSIGNED_EVENT = "collector_unassigned"
ACTION_CREATED_EVENT = "collection_action_created"
ACTION_COMPLETED_EVENT = "collection_action_completed"
ACTION_CANCELLED_EVENT = "collection_action_cancelled"
PTP_CREATED_EVENT = "promise_to_pay_created"
PTP_CANCELLED_EVENT = "promise_to_pay_cancelled"
TRUSTEE_PAYMENT_EVENT = "insolvency_trustee_payment_recorded"
MAINTENANCE_FEE_EVENT = "insolvency_maintenance_fee_charged"

PTP_OPEN = "open"
PTP_KEPT = "kept"
PTP_BROKEN = "broken"
PTP_CANCELLED = "cancelled"


class CollectionsError(ValueError):
    """A collections rule violation (bad state / bad params) — maps to 4xx."""


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------


def tier_allows_bucket(tier: str, bucket: str) -> bool:
    """May a collector of ``tier`` work a loan in ``bucket``? PURE.

    Seniors work everything (including the insolvency portfolio and
    written-off recoveries); juniors only the shallow ladder
    (current / current-month-late / pot-30).
    """
    if tier not in TIERS:
        raise CollectionsError(f"Unknown collector tier {tier!r}")
    if tier == TIER_SENIOR:
        return True
    return bucket in JUNIOR_ALLOWED_BUCKETS


def normalize_alpha(letter: Optional[str]) -> str:
    """Validate + normalize an alpha-range bound to one uppercase A-Z letter."""
    value = (letter or "").strip().upper()
    if len(value) != 1 or not ("A" <= value <= "Z"):
        raise CollectionsError(
            f"Alpha bound must be a single letter A-Z, got {letter!r}"
        )
    return value


def alpha_matches(last_name: Optional[str], alpha_from: str, alpha_to: str) -> bool:
    """Does ``last_name`` fall in the inclusive first-letter range? PURE.

    Dave: "the way that I like to assign my collection cues to users is by
    last name … per vendor or per vendor and alpha". Non-letter or empty
    names never match (they surface as unassigned for manual triage).
    """
    lo, hi = normalize_alpha(alpha_from), normalize_alpha(alpha_to)
    if lo > hi:
        raise CollectionsError(
            f"Alpha range is inverted ({lo!r} > {hi!r})"
        )
    name = (last_name or "").strip().upper()
    if not name or not ("A" <= name[0] <= "Z"):
        return False
    return lo <= name[0] <= hi


def loan_has_past_due_unpaid(loan, today: date) -> bool:
    """Any installment strictly past due at ``today`` with an unpaid balance
    (mirrors the bucket machine's past-due test). PURE over the loan's
    in-memory schedule."""
    for item in loan.schedule or []:
        if item.status in NOT_OWED_STATUSES:
            continue
        if (item.paid_cents or 0) < item.total_cents and item.due_date < today:
            return True
    return False


def loan_days_past_due(loan, today: date) -> int:
    """Live DPD from the oldest past-due unpaid installment. PURE."""
    oldest: Optional[date] = None
    for item in loan.schedule or []:
        if item.status in NOT_OWED_STATUSES:
            continue
        if (item.paid_cents or 0) < item.total_cents and item.due_date < today:
            if oldest is None or item.due_date < oldest:
                oldest = item.due_date
    return (today - oldest).days if oldest else 0


def display_bucket(loan, today: date) -> str:
    """The loan's effective (display) bucket right now — last month-end
    snapshot with the immediate insolvency / written-off / full-cure
    overrides applied (delegates to the bucket machine's rule)."""
    return effective_bucket(
        loan.status,
        loan.insolvency_status,
        loan.current_bucket,
        loan_has_past_due_unpaid(loan, today),
    )


@dataclass(frozen=True)
class PromiseEvaluation:
    """The derived status of one promise against actual payments. PURE."""

    status: str
    covered_cents: int


def evaluate_promise(
    amount_cents: int,
    promised_date: date,
    window_start: date,
    payments: Iterable,
    today: date,
    *,
    grace_days: int = 0,
) -> PromiseEvaluation:
    """Evaluate one promise against ACTUAL ledger payments. PURE.

    Qualifying payments are those received between ``window_start`` (the
    promise's creation date) and ``promised_date`` inclusive (+``grace_days``
    if a tolerance is ever configured). ``payments`` are objects with
    ``amount_cents`` and ``received_at`` (datetime or date).

      * covered >= promised amount → ``kept`` (even before the date passes);
      * date (+grace) passed without cover → ``broken``;
      * otherwise → ``open``.
    """
    deadline = promised_date
    if grace_days:
        from datetime import timedelta

        deadline = promised_date + timedelta(days=grace_days)

    covered = 0
    for payment in payments:
        received = payment.received_at
        received_date = received.date() if isinstance(received, datetime) else received
        if window_start <= received_date <= deadline:
            covered += payment.amount_cents

    if covered >= amount_cents:
        return PromiseEvaluation(status=PTP_KEPT, covered_cents=covered)
    if today > deadline:
        return PromiseEvaluation(status=PTP_BROKEN, covered_cents=covered)
    return PromiseEvaluation(status=PTP_OPEN, covered_cents=covered)


def insolvency_sort_key(principal_balance_cents: int, days_past_due: int):
    """Dave's insolvency-queue ordering: outstanding principal first, then
    DPD, both descending. PURE (use with ``sorted(..., key=...)``)."""
    return (-principal_balance_cents, -days_past_due)


# ---------------------------------------------------------------------------
# Orchestration — collector assignment
# ---------------------------------------------------------------------------


def active_assignment(
    db: Session, loan_id
) -> Optional[PlatformCollectorAssignment]:
    return (
        db.query(PlatformCollectorAssignment)
        .filter(
            PlatformCollectorAssignment.loan_id == loan_id,
            PlatformCollectorAssignment.active.is_(True),
        )
        .first()
    )


def assign_loan(
    db: Session,
    loan: PlatformLoan,
    collector_user_id,
    tier: str,
    *,
    method: str,
    actor_id: str,
    reassign: bool = False,
    today: Optional[date] = None,
) -> PlatformCollectorAssignment:
    """Assign one loan to a collector (tier-gated). Adds + flushes; caller
    commits.

    * ``tier`` must be allowed to work the loan's effective bucket (junior →
      shallow ladder only).
    * An existing ACTIVE assignment blocks unless ``reassign=True`` (then it
      is deactivated, keeping history).

    Raises :class:`CollectionsError` on a rule violation.
    """
    if tier not in TIERS:
        raise CollectionsError(f"Unknown collector tier {tier!r}")
    if method not in ASSIGN_METHODS:
        raise CollectionsError(f"Unknown assignment method {method!r}")

    today = today or date.today()
    bucket = display_bucket(loan, today)
    if not tier_allows_bucket(tier, bucket):
        raise CollectionsError(
            f"A {tier} collector cannot work bucket {bucket!r} "
            f"(junior tier is limited to {', '.join(JUNIOR_ALLOWED_BUCKETS)})"
        )

    existing = active_assignment(db, loan.id)
    replaced = None
    if existing is not None:
        if not reassign:
            raise CollectionsError(
                "Loan already has an active collector assignment "
                "(pass reassign to replace it)"
            )
        existing.active = False
        existing.unassigned_by = actor_id
        existing.unassigned_at = datetime.now(timezone.utc)
        replaced = existing

    assignment = PlatformCollectorAssignment(
        id=uuid4(),
        loan_id=loan.id,
        collector_user_id=collector_user_id,
        tier=tier,
        method=method,
        active=True,
        assigned_by=actor_id,
        assigned_at=datetime.now(timezone.utc),
    )
    db.add(assignment)
    db.add(
        PlatformEvent(
            event_type=COLLECTOR_ASSIGNED_EVENT,
            actor=actor_id,
            application_id=getattr(loan, "application_id", None),
            payload={
                "v": 1,
                "actor": {"type": "staff", "id": actor_id},
                "loan_id": str(loan.id),
                "collector_user_id": str(collector_user_id),
                "tier": tier,
                "method": method,
                "bucket": bucket,
                "replaced_assignment_id": str(replaced.id) if replaced else None,
            },
        )
    )
    db.flush()
    return assignment


def unassign_loan(
    db: Session,
    loan: PlatformLoan,
    *,
    actor_id: str,
    comment: Optional[str] = None,
) -> PlatformCollectorAssignment:
    """Deactivate the loan's active assignment (history kept). Adds + flushes;
    caller commits."""
    existing = active_assignment(db, loan.id)
    if existing is None:
        raise CollectionsError("Loan has no active collector assignment")
    existing.active = False
    existing.unassigned_by = actor_id
    existing.unassigned_at = datetime.now(timezone.utc)
    db.add(
        PlatformEvent(
            event_type=COLLECTOR_UNASSIGNED_EVENT,
            actor=actor_id,
            application_id=getattr(loan, "application_id", None),
            payload={
                "v": 1,
                "actor": {"type": "staff", "id": actor_id},
                "loan_id": str(loan.id),
                "collector_user_id": str(existing.collector_user_id),
                "comment": (comment or "").strip() or None,
            },
        )
    )
    db.flush()
    return existing


# ---------------------------------------------------------------------------
# Orchestration — promise-to-pay
# ---------------------------------------------------------------------------


def refresh_promise_statuses(
    db: Session,
    loan: PlatformLoan,
    promises: Iterable[PlatformPromiseToPay],
    *,
    today: Optional[date] = None,
) -> list[PlatformPromiseToPay]:
    """Re-derive open promises' statuses from the loan's ACTUAL payments.

    Terminal statuses (kept / broken / cancelled) are never reopened —
    evaluation only moves a promise forward. Mutates rows in the session;
    caller commits. Returns the promises whose status changed.
    """
    today = today or date.today()
    now = datetime.now(timezone.utc)
    changed: list[PlatformPromiseToPay] = []
    payments = list(loan.payments or [])
    for promise in promises:
        if promise.status != PTP_OPEN:
            continue
        window_start = (
            promise.created_at.date()
            if isinstance(promise.created_at, datetime)
            else (promise.created_at or today)
        )
        result = evaluate_promise(
            promise.amount_cents,
            promise.promised_date,
            window_start,
            payments,
            today,
        )
        promise.covered_cents = result.covered_cents
        if result.status != PTP_OPEN:
            promise.status = result.status
            promise.evaluated_at = now
            changed.append(promise)
    return changed


# ---------------------------------------------------------------------------
# Orchestration — header math
# ---------------------------------------------------------------------------


def header_view(loan: PlatformLoan, as_of: date, *, today: Optional[date] = None) -> dict:
    """The delinquent-loan header (Dave's identity, 04__WP_Collections):

        outstanding principal + interest due + fees due + add-on balance
            = total payout

    READ-ONLY over the actuals ledger (``loan_ledger.loan_balances``); the
    interest engine is the single source of the math."""
    today = today or as_of
    balances = loan_ledger.loan_balances(loan, as_of=as_of)
    return {
        "loan_id": str(loan.id),
        "as_of": as_of.isoformat(),
        "outstanding_principal_cents": balances.outstanding_principal_cents,
        "interest_due_cents": balances.interest_due_cents,
        "fees_due_cents": balances.fees_due_cents,
        "add_on_balance_cents": balances.add_on_balance_cents,
        # The four buckets sum EXACTLY to the payout (header invariant).
        "total_payout_cents": balances.payoff_cents,
        "days_past_due": loan_days_past_due(loan, today),
        "bucket": display_bucket(loan, today),
        "insolvency_status": loan.insolvency_status,
    }


# ---------------------------------------------------------------------------
# Orchestration — insolvency portfolio money paths
# ---------------------------------------------------------------------------


def _require_insolvent(loan: PlatformLoan) -> None:
    if loan.insolvency_status not in INSOLVENCY_STATUSES:
        raise CollectionsError(
            "Loan is not in the insolvency portfolio "
            "(mark consumer_proposal / bankruptcy / credit_counseling first)"
        )


def record_trustee_payment(
    db: Session,
    loan: PlatformLoan,
    amount_cents: int,
    *,
    received_at: Optional[datetime] = None,
    external_ref: Optional[str] = None,
    comment: Optional[str] = None,
    actor_id: str,
):
    """Record a payment received FROM AN INSOLVENCY TRUSTEE into the ledger.

    Delegates ENTIRELY to the hardened ``loan_servicing.record_payment``
    money path (regular allocation: accrued interest → principal → fees) with
    ``method='trustee'`` so trustee recoveries are separable in
    reconciliation. Guarded to insolvency-portfolio loans. Adds rows +
    audit event; CALLER commits.
    """
    _require_insolvent(loan)
    if amount_cents <= 0:
        raise CollectionsError("amount_cents must be positive")
    received_at = received_at or datetime.now(timezone.utc)
    payment = loan_servicing.record_payment(
        db,
        loan,
        amount_cents,
        received_at,
        "trustee",
        external_ref,
        created_by=actor_id,
        comment=(comment or "Trustee payment (insolvency portfolio)"),
        repayment_mode="regular",
    )
    db.add(
        PlatformEvent(
            event_type=TRUSTEE_PAYMENT_EVENT,
            actor=actor_id,
            application_id=getattr(loan, "application_id", None),
            payload={
                "v": 1,
                "actor": {"type": "staff", "id": actor_id},
                "loan_id": str(loan.id),
                "payment_id": str(payment.id),
                "amount_cents": amount_cents,
                "insolvency_status": loan.insolvency_status,
                "external_ref": external_ref,
            },
        )
    )
    return payment


def _vendor_id_for_loan(db: Session, loan: PlatformLoan):
    """The loan's vendor id via its application (None for migrated loans) —
    same derivation record_payment uses for the ledger reference."""
    if getattr(loan, "application_id", None) is None:
        return None
    from app.models.platform.credit_application import PlatformCreditApplication

    row = (
        db.query(PlatformCreditApplication.vendor_id)
        .filter(PlatformCreditApplication.id == loan.application_id)
        .first()
    )
    return row[0] if row else None


def apply_maintenance_fee(
    db: Session,
    loan: PlatformLoan,
    amount_cents: int,
    *,
    fee_month: Optional[date] = None,
    actor_id: str,
    today: Optional[date] = None,
) -> Optional[PlatformLoanTransaction]:
    """Charge the monthly insolvency maintenance fee (Dave: "a maintenance
    fee to follow up and maintain bankrupt accounts").

    * Guarded to insolvency-portfolio loans.
    * IDEMPOTENT per (loan, month) via ``platform_insolvency_maintenance_fees``
      — a second call for the same month returns None and charges nothing.
    * The money is an immutable ledger ``fee`` row in the NON-ACCRUING ADD-ON
      bucket (triggered fees live under add-on balance, never loan fees —
      exactly the NSF-fee treatment in ``auto_collection.charge_nsf_fee``).

    Adds + flushes; CALLER commits. This is the hook the monthly cron will
    call once crons are unparked (loop insolvent loans → apply).
    """
    _require_insolvent(loan)
    if amount_cents <= 0:
        raise CollectionsError("amount_cents must be positive")
    today = today or date.today()
    fee_month = month_start(fee_month or today)

    existing = (
        db.query(PlatformInsolvencyMaintenanceFee)
        .filter(
            PlatformInsolvencyMaintenanceFee.loan_id == loan.id,
            PlatformInsolvencyMaintenanceFee.fee_month == fee_month,
        )
        .first()
    )
    if existing is not None:
        return None  # already charged for this month

    seq = loan_ledger.next_seq(loan)
    vendor_id = _vendor_id_for_loan(db, loan)
    txn = PlatformLoanTransaction(
        id=uuid4(),
        loan_id=loan.id,
        seq=seq,
        reference=loan_ledger.build_reference(vendor_id, loan.id, seq),
        txn_type="fee",
        payment_type=None,
        repayment_mode=None,
        amount_cents=amount_cents,
        principal_cents=0,
        interest_cents=0,
        fees_cents=0,
        add_on_cents=amount_cents,  # non-accruing add-on bucket
        effective_date=today,
        processing_date=today,
        created_by=actor_id,
        comment=f"Insolvency maintenance fee — {fee_month:%Y-%m}",
    )
    loan.transactions.append(txn)  # keep the in-session ledger view consistent
    db.add(txn)
    db.add(
        PlatformInsolvencyMaintenanceFee(
            id=uuid4(),
            loan_id=loan.id,
            fee_month=fee_month,
            amount_cents=amount_cents,
            transaction_id=txn.id,
            created_by=actor_id,
        )
    )
    db.add(
        PlatformEvent(
            event_type=MAINTENANCE_FEE_EVENT,
            actor=actor_id,
            application_id=getattr(loan, "application_id", None),
            payload={
                "v": 1,
                "actor": {"type": "staff", "id": actor_id},
                "loan_id": str(loan.id),
                "fee_month": fee_month.isoformat(),
                "amount_cents": amount_cents,
                "transaction_id": str(txn.id),
            },
        )
    )
    db.flush()
    return txn


def run_monthly_maintenance_fees(
    db: Session,
    amount_cents: int,
    *,
    fee_month: Optional[date] = None,
    actor_id: str = "insolvency_maintenance",
    today: Optional[date] = None,
) -> dict:
    """The monthly maintenance-fee HOOK: charge every insolvency-portfolio
    loan once for ``fee_month`` (idempotent — re-runs skip already-charged
    loans). Commits once. Intended caller: the (currently parked) monthly
    cron, or an admin backfill."""
    today = today or date.today()
    fee_month = month_start(fee_month or today)
    loans = (
        db.query(PlatformLoan)
        .filter(PlatformLoan.insolvency_status.in_(INSOLVENCY_STATUSES))
        .all()
    )
    charged, skipped = 0, 0
    for loan in loans:
        txn = apply_maintenance_fee(
            db,
            loan,
            amount_cents,
            fee_month=fee_month,
            actor_id=actor_id,
            today=today,
        )
        if txn is None:
            skipped += 1
        else:
            charged += 1
    db.commit()
    return {
        "fee_month": fee_month.isoformat(),
        "loans_charged": charged,
        "loans_skipped": skipped,
    }
