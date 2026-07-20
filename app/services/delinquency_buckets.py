"""Dave's month-end delinquency bucket state machine (WS-H, Turnkey parity P0).

Replaces Turnkey's rolling 1-30/31-60/61-90/>91 DPD filters with Dave's model
(04__WP_Collections §4): buckets are assigned as MONTH-END SNAPSHOTS —

    current → current_month_late → pot_30 → pot_60 (credit-bureau reported)
            → pot_90 → default

plus a segregated **insolvency** classification (consumer proposal /
bankruptcy / credit counseling) that overrides bucket derivation, and terminal
**written_off** (follows the loan's ``charged_off`` status).

LAYERING: this is a REPORTING/COLLECTIONS layer on top of the existing nightly
DPD aging job (``app.jobs.delinquency`` / ``run_delinquency_aging``), which
keeps flipping installment/loan statuses (``late`` / ``delinquent``) exactly as
before. Nothing here changes loan money behaviour.

Split of responsibilities (flow_engine / interest_engine idiom):

  * PURE CORE — ``bucket_for`` and friends: no DB, no I/O, no clock reads.
    Deterministic derivation from installment state at a month-end date.
  * ORCHESTRATION — ``run_bucket_snapshot`` (called by the external-cron job
    ``app.jobs.bucket_snapshot``), ``set_insolvency`` (admin endpoint), and the
    DB-backed aggregate readers. Callers own commits.

DOCUMENTED POLICY ASSUMPTIONS (all encoded in :class:`BucketPolicy`; the exact
cure/rollover semantics are AMBIGUOUS pending Dave — flagged in the PR):

  1. Buckets are assigned ONLY at the month-end snapshot. Intra-month a loan
     keeps its last snapshot bucket (``platform_loans.current_bucket``); live
     DPD is reported separately alongside it.
  2. FULL catch-up (no past-due installment remains) cures to ``current``
     immediately — surfaced through :func:`effective_bucket` on the read path;
     the stored snapshot history is never rewritten.
  3. PARTIAL catch-up does NOT cure a bucket (Dave's "amount to move"
     tolerance algorithm is a separate Excel he still owes us — when it
     arrives it will plug in here as a policy knob).
  4. POT-N derivation is DAYS-past-due at month-end: pot_30 ⇐ 30-59 DPD,
     pot_60 ⇐ 60-89, pot_90 ⇐ 90-120, default ⇐ >120 (dpd ≥ 121). NOTE: Dave's
     video narration counts MONTHS unpaid ("due July 15 → CML until Jul 31 →
     pot-30 on Aug 1"); for due dates early in a month the two diverge (due
     Jul 1 unpaid at Jul 31 is 30 DPD → pot_30 here, but "still CML" by the
     month-count reading). DPD thresholds are implemented per the build spec;
     flagged for Dave.
  5. Insolvency overrides everything INCLUDING written_off: Dave keeps a
     segregated insolvency portfolio that is written off the main book but
     still maintained, so an insolvent charged-off loan reports ``insolvency``.
  6. ``bureau_reportable`` is a FLAG only (pot_60 and deeper, per Dave: "at 60
     days delinquent it is reported to the credit bureau"). Actual Equifax
     metro2 reporting is a later workstream.
  7. Suspended schedule items (WS-F may add a ``suspended`` installment status
     in parallel) are excluded from both the past-due derivation and the
     payment replay. The check is a plain string comparison on the status
     value, so this module works whether or not WS-F has merged.

MONEY: integer cents everywhere. Snapshot outstanding principal comes from the
actuals ledger's balance view (``loan_ledger.loan_balances`` at the month-end
date) so re-running a month's snapshot is deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from sqlalchemy.orm import Session

from app.models.platform.event import PlatformEvent
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanDelinquencySnapshot,
)
from app.services import loan_ledger


# ---------------------------------------------------------------------------
# Bucket vocabulary
# ---------------------------------------------------------------------------

BUCKET_CURRENT = "current"
BUCKET_CURRENT_MONTH_LATE = "current_month_late"
BUCKET_POT_30 = "pot_30"
BUCKET_POT_60 = "pot_60"
BUCKET_POT_90 = "pot_90"
BUCKET_DEFAULT = "default"
BUCKET_INSOLVENCY = "insolvency"
BUCKET_WRITTEN_OFF = "written_off"

ALL_BUCKETS = (
    BUCKET_CURRENT,
    BUCKET_CURRENT_MONTH_LATE,
    BUCKET_POT_30,
    BUCKET_POT_60,
    BUCKET_POT_90,
    BUCKET_DEFAULT,
    BUCKET_INSOLVENCY,
    BUCKET_WRITTEN_OFF,
)

# The delinquency ladder in roll order (for the debt-roll report: Dave's
# "debt roll = % of current-month-late rolling into POT 30s", generalized to
# every adjacent pair).
ROLL_LADDER = (
    (BUCKET_CURRENT_MONTH_LATE, BUCKET_POT_30),
    (BUCKET_POT_30, BUCKET_POT_60),
    (BUCKET_POT_60, BUCKET_POT_90),
    (BUCKET_POT_90, BUCKET_DEFAULT),
)

INSOLVENCY_STATUSES = ("consumer_proposal", "bankruptcy", "credit_counseling")

# Installment statuses that mean "nothing is owed on this row" for bucketing.
# ``suspended`` is WS-F's (possibly not-yet-merged) scheduled-transaction
# surgery status — excluded by string value so both merge orders work.
NOT_OWED_STATUSES = ("waived", "suspended")

LOAN_INSOLVENCY_MARKED_EVENT = "loan_insolvency_marked"
LOAN_INSOLVENCY_CLEARED_EVENT = "loan_insolvency_cleared"


@dataclass(frozen=True)
class BucketPolicy:
    """EVERY policy choice of the bucket machine, in ONE place (see module
    docstring for the numbered assumptions — each maps to a field here).

    Flagged for Dave: cure semantics, month-end-only assignment, the DPD-vs-
    months-unpaid derivation, and the >120 default threshold.
    """

    pot_30_min_dpd: int = 30
    pot_60_min_dpd: int = 60
    pot_90_min_dpd: int = 90
    # ">120 → default": 120 DPD is still pot_90; 121 is default.
    default_min_dpd: int = 121
    # Buckets at/after this one raise the bureau_reportable snapshot flag.
    bureau_reportable_buckets: tuple = (BUCKET_POT_60, BUCKET_POT_90, BUCKET_DEFAULT)
    # Assumption 1: buckets move only at month-end (intra-month reads keep the
    # last snapshot bucket; live DPD is shown separately).
    month_end_assignment_only: bool = True
    # Assumption 2: full catch-up cures to current immediately.
    full_catchup_cures_immediately: bool = True
    # Assumption 3: partial catch-up never cures (pending Dave's
    # amount-to-move tolerance Excel).
    partial_catchup_cures: bool = False


DEFAULT_POLICY = BucketPolicy()


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallmentState:
    """One installment's owed/paid state at an evaluation date (DB-free)."""

    due_date: date
    total_cents: int
    paid_cents: int
    status: str = "scheduled"


@dataclass(frozen=True)
class LoanBucketInputs:
    """Everything ``bucket_for`` needs about one loan (DB-free)."""

    loan_status: str
    insolvency_status: Optional[str]
    installments: tuple[InstallmentState, ...]


@dataclass(frozen=True)
class BucketAssignment:
    """The derived classification for one loan at one month-end (DB-free)."""

    bucket: str
    days_past_due: int
    amount_past_due_cents: int
    bureau_reportable: bool


def _past_due_installments(
    installments: Iterable[InstallmentState], as_of: date
) -> list[InstallmentState]:
    """Installments with an unpaid balance strictly past due at ``as_of``.

    Mirrors the nightly aging job's overdue test (``due_date < as_of``): an
    installment due exactly on the evaluation date is not yet late. Waived and
    suspended rows owe nothing (assumption 7).
    """
    return [
        i
        for i in installments
        if i.status not in NOT_OWED_STATUSES
        and i.paid_cents < i.total_cents
        and i.due_date < as_of
    ]


def _dpd_bucket(dpd: int, policy: BucketPolicy) -> str:
    if dpd <= 0:
        return BUCKET_CURRENT
    if dpd < policy.pot_30_min_dpd:
        return BUCKET_CURRENT_MONTH_LATE
    if dpd < policy.pot_60_min_dpd:
        return BUCKET_POT_30
    if dpd < policy.pot_90_min_dpd:
        return BUCKET_POT_60
    if dpd < policy.default_min_dpd:
        return BUCKET_POT_90
    return BUCKET_DEFAULT


def bucket_for(
    inputs: LoanBucketInputs,
    month_end: date,
    policy: BucketPolicy = DEFAULT_POLICY,
) -> BucketAssignment:
    """Derive the month-end bucket for one loan. PURE — deterministic, no I/O.

    Precedence (assumptions 5, then terminal status, then DPD):
      1. insolvency_status set → ``insolvency`` (overrides everything,
         including written_off — the segregated insolvency portfolio).
      2. loan charged_off → ``written_off``.
      3. Otherwise from days past due of the OLDEST unpaid installment at
         ``month_end``: 0 → current, 1-29 → current_month_late, 30-59 →
         pot_30, 60-89 → pot_60, 90-120 → pot_90, >120 → default.

    ``days_past_due`` / ``amount_past_due_cents`` are computed for every
    classification (informational on insolvency/written_off rows).
    ``bureau_reportable`` is raised only for delinquency-ladder buckets
    (pot_60+) — never for insolvency/written_off, whose bureau treatment is a
    separate concern.
    """
    past_due = _past_due_installments(inputs.installments, month_end)
    if past_due:
        oldest = min(i.due_date for i in past_due)
        dpd = (month_end - oldest).days
        amount_past_due = sum(i.total_cents - i.paid_cents for i in past_due)
    else:
        dpd = 0
        amount_past_due = 0

    if inputs.insolvency_status in INSOLVENCY_STATUSES:
        bucket = BUCKET_INSOLVENCY
    elif inputs.loan_status == "charged_off":
        bucket = BUCKET_WRITTEN_OFF
    else:
        bucket = _dpd_bucket(dpd, policy)

    return BucketAssignment(
        bucket=bucket,
        days_past_due=dpd,
        amount_past_due_cents=amount_past_due,
        bureau_reportable=bucket in policy.bureau_reportable_buckets,
    )


def installment_states_as_of(
    schedule_items: Sequence,
    payments: Sequence,
    as_of: date,
) -> tuple[InstallmentState, ...]:
    """Reconstruct each installment's paid state AS OF ``as_of`` (pure).

    Replays payments received on/before ``as_of`` against the schedule
    oldest-installment-first — the exact allocation ``record_payment`` applies
    (and migration 049's backfill replays) — so re-running a past month's
    snapshot after later payments landed still derives the same buckets.

    * ``schedule_items``: objects with installment_number, due_date,
      total_cents, status (ORM rows or fakes).
    * ``payments``: objects with amount_cents and received_at (datetime or
      date).

    LIMITATION (documented): waived/suspended statuses carry no timestamp, so
    the CURRENT status is used for all evaluation dates.
    """
    ordered = sorted(schedule_items, key=lambda s: s.installment_number)
    paid: dict[int, int] = {s.installment_number: 0 for s in ordered}

    def _payment_date(p) -> date:
        r = p.received_at
        return r.date() if isinstance(r, datetime) else r

    eligible = sorted(
        (p for p in payments if _payment_date(p) <= as_of),
        key=lambda p: (_payment_date(p), getattr(p, "created_at", None) or datetime.min),
    )
    for payment in eligible:
        remaining = payment.amount_cents
        for item in ordered:
            if remaining <= 0:
                break
            if item.status in NOT_OWED_STATUSES:
                continue
            outstanding = item.total_cents - paid[item.installment_number]
            if outstanding <= 0:
                continue
            applied = min(remaining, outstanding)
            paid[item.installment_number] += applied
            remaining -= applied

    return tuple(
        InstallmentState(
            due_date=s.due_date,
            total_cents=s.total_cents,
            paid_cents=paid[s.installment_number],
            status=s.status,
        )
        for s in ordered
    )


def effective_bucket(
    loan_status: str,
    insolvency_status: Optional[str],
    current_bucket: Optional[str],
    has_past_due_unpaid: bool,
    policy: BucketPolicy = DEFAULT_POLICY,
) -> str:
    """The bucket to DISPLAY between snapshots (pure). Assumptions 1 + 2:

    * insolvency / written_off override immediately (they are event-driven,
      not month-end-driven);
    * FULL catch-up (no past-due unpaid installment) cures to ``current``
      immediately without waiting for month-end;
    * otherwise the loan keeps its last month-end snapshot bucket.
    """
    if insolvency_status in INSOLVENCY_STATUSES:
        return BUCKET_INSOLVENCY
    if loan_status == "charged_off":
        return BUCKET_WRITTEN_OFF
    if policy.full_catchup_cures_immediately and not has_past_due_unpaid:
        return BUCKET_CURRENT
    return current_bucket or BUCKET_CURRENT


def compute_roll_rates(
    prior: dict, current: dict
) -> list[dict]:
    """Debt-roll rates between two months' snapshots (pure).

    ``prior`` / ``current`` map loan_id → bucket for the prior and current
    snapshot month. For every adjacent ladder pair (CML→pot_30, pot_30→pot_60,
    …) returns base_count (loans in the FROM bucket last month), rolled_count
    (how many of those are in the TO bucket this month), and roll_rate_bps
    (integer basis points; 10_000 = 100%). This is Dave's "debt roll is the
    percentage of current month late that roll into a new month and become
    POT 30s", generalized down the ladder.
    """
    out: list[dict] = []
    for from_bucket, to_bucket in ROLL_LADDER:
        base_ids = [lid for lid, b in prior.items() if b == from_bucket]
        rolled = sum(1 for lid in base_ids if current.get(lid) == to_bucket)
        base = len(base_ids)
        out.append(
            {
                "from_bucket": from_bucket,
                "to_bucket": to_bucket,
                "base_count": base,
                "rolled_count": rolled,
                "roll_rate_bps": (rolled * 10_000) // base if base else 0,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Month arithmetic (pure)
# ---------------------------------------------------------------------------


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end_of(snapshot_month: date) -> date:
    """Last day of the month containing ``snapshot_month``."""
    first = snapshot_month.replace(day=1)
    if first.month == 12:
        return date(first.year, 12, 31)
    return date(first.year, first.month + 1, 1) - timedelta(days=1)


def default_snapshot_month(today: date) -> date:
    """The month the external-cron job should snapshot when run on ``today``:

    * on a month's LAST day → that month (the cron's intended slot);
    * any other day → the previous month (idempotent re-runs / catch-up after
      a missed slot update that month's existing rows).
    """
    if (today + timedelta(days=1)).month != today.month:
        return month_start(today)
    return month_start(month_start(today) - timedelta(days=1))


# ---------------------------------------------------------------------------
# Orchestration (DB)
# ---------------------------------------------------------------------------

# Loan statuses that belong in the month-end snapshot. pending_disbursement
# (no money out), paid_off and cancelled are excluded — nothing to collect.
_SNAPSHOT_LOAN_STATUSES = ("active", "delinquent", "charged_off")


@dataclass(frozen=True)
class SnapshotResult:
    """Summary of one ``run_bucket_snapshot`` pass (DB-free value object)."""

    snapshot_month: date
    month_end: date
    loans_snapshotted: int
    bucket_counts: dict


def run_bucket_snapshot(
    db: Session,
    snapshot_month: Optional[date] = None,
    *,
    today: Optional[date] = None,
    policy: BucketPolicy = DEFAULT_POLICY,
) -> SnapshotResult:
    """Assign month-end buckets to every servicing loan and persist snapshots.

    IDEMPOTENT PER MONTH: one row per (loan, month) — re-runs UPDATE the same
    snapshot rows (derivation is deterministic at the month-end date because
    installment paid state is replayed as-of that date and outstanding
    principal comes from the ledger balance view at that date).

    ``platform_loans.current_bucket`` is updated only when snapshotting the
    LATEST completed month (re-running an old month must not clobber the
    loan's present classification with a stale one).

    Commits once. Returns a :class:`SnapshotResult`.
    """
    today = today or date.today()
    snapshot_month = month_start(snapshot_month or default_snapshot_month(today))
    m_end = month_end_of(snapshot_month)
    if m_end > today:
        raise ValueError(
            f"Refusing to snapshot {snapshot_month:%Y-%m}: its month-end "
            f"({m_end}) has not been reached (today={today})."
        )
    is_latest_month = snapshot_month == default_snapshot_month(today)
    now = datetime.now(timezone.utc)

    loans = (
        db.query(PlatformLoan)
        .filter(PlatformLoan.status.in_(_SNAPSHOT_LOAN_STATUSES))
        .all()
    )

    bucket_counts: dict[str, int] = {}
    snapshotted = 0
    for loan in loans:
        states = installment_states_as_of(
            list(loan.schedule or []), list(loan.payments or []), m_end
        )
        assignment = bucket_for(
            LoanBucketInputs(
                loan_status=loan.status,
                insolvency_status=loan.insolvency_status,
                installments=states,
            ),
            m_end,
            policy,
        )
        # Deterministic as-of outstanding principal from the actuals ledger.
        outstanding = loan_ledger.loan_balances(loan, as_of=m_end).outstanding_principal_cents

        existing = (
            db.query(PlatformLoanDelinquencySnapshot)
            .filter(
                PlatformLoanDelinquencySnapshot.loan_id == loan.id,
                PlatformLoanDelinquencySnapshot.snapshot_month == snapshot_month,
            )
            .first()
        )
        if existing is not None:
            existing.bucket = assignment.bucket
            existing.days_past_due = assignment.days_past_due
            existing.amount_past_due_cents = assignment.amount_past_due_cents
            existing.outstanding_principal_cents = outstanding
            existing.bureau_reportable = assignment.bureau_reportable
            existing.snapshotted_at = now
        else:
            db.add(
                PlatformLoanDelinquencySnapshot(
                    loan_id=loan.id,
                    snapshot_month=snapshot_month,
                    bucket=assignment.bucket,
                    days_past_due=assignment.days_past_due,
                    amount_past_due_cents=assignment.amount_past_due_cents,
                    outstanding_principal_cents=outstanding,
                    bureau_reportable=assignment.bureau_reportable,
                    snapshotted_at=now,
                )
            )

        if is_latest_month:
            loan.current_bucket = assignment.bucket

        bucket_counts[assignment.bucket] = bucket_counts.get(assignment.bucket, 0) + 1
        snapshotted += 1

    db.commit()
    return SnapshotResult(
        snapshot_month=snapshot_month,
        month_end=m_end,
        loans_snapshotted=snapshotted,
        bucket_counts=bucket_counts,
    )


# ---------------------------------------------------------------------------
# Insolvency marking (admin write path)
# ---------------------------------------------------------------------------


def set_insolvency(
    db: Session,
    loan: PlatformLoan,
    status: str,
    comment: str,
    actor_id: str,
    *,
    today: Optional[date] = None,
    policy: BucketPolicy = DEFAULT_POLICY,
) -> PlatformLoan:
    """Mark (or clear, with ``status='none'``) a loan's insolvency classification.

    MANUAL + AUDITED: insolvency is a human determination (consumer proposal /
    bankruptcy / credit counseling paperwork in hand), never derived. The
    mandatory ``comment`` and the acting staff id land in a ``platform_events``
    row (ids + statuses only — no PII).

    Marking flips ``current_bucket`` to ``insolvency`` immediately (it is
    event-driven, not month-end-driven). Clearing re-derives the bucket from
    the LIVE schedule state at ``today`` so the loan re-enters the ladder
    where it belongs rather than resurrecting a pre-insolvency label.

    Adds to the session; the CALLER owns the commit (endpoint idiom).
    """
    if status not in INSOLVENCY_STATUSES and status != "none":
        raise ValueError(f"Unknown insolvency status '{status}'")
    if not (comment or "").strip():
        raise ValueError("A comment is mandatory when changing insolvency status")

    today = today or date.today()
    before = loan.insolvency_status

    if status == "none":
        loan.insolvency_status = None
        loan.insolvency_marked_at = None
        loan.insolvency_marked_by = None
        # Re-derive from live state (assumption 2's cure rule applies via the
        # DPD derivation: no past-due unpaid → current).
        states = tuple(
            InstallmentState(
                due_date=s.due_date,
                total_cents=s.total_cents,
                paid_cents=s.paid_cents,
                status=s.status,
            )
            for s in (loan.schedule or [])
        )
        assignment = bucket_for(
            LoanBucketInputs(
                loan_status=loan.status,
                insolvency_status=None,
                installments=states,
            ),
            today,
            policy,
        )
        loan.current_bucket = assignment.bucket
        event_type = LOAN_INSOLVENCY_CLEARED_EVENT
    else:
        loan.insolvency_status = status
        loan.insolvency_marked_at = datetime.now(timezone.utc)
        loan.insolvency_marked_by = actor_id
        loan.current_bucket = BUCKET_INSOLVENCY
        event_type = LOAN_INSOLVENCY_MARKED_EVENT

    db.add(
        PlatformEvent(
            event_type=event_type,
            actor=actor_id,
            application_id=getattr(loan, "application_id", None),
            payload={
                "v": 1,
                "actor": {"type": "staff", "id": actor_id},
                "loan_id": str(loan.id),
                "before": {"insolvency_status": before},
                "after": {
                    "insolvency_status": loan.insolvency_status,
                    "current_bucket": loan.current_bucket,
                },
                "comment": comment.strip(),
            },
        )
    )
    return loan
