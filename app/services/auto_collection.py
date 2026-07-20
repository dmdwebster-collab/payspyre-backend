"""Auto-collection engine (WS-G, Turnkey parity P0) — scheduled PAD pulls.

Turnkey Lender auto-charges each scheduled installment on its due date over the
PAD/EFT rail; PaySpyre previously had only borrower-initiated Pay Now. This
module is the whole engine:

    nightly cron (app/jobs/auto_collection.py)
        │  run_auto_collection()
        ├── PAD pre-notifications: N business days before each auto-charge,
        │   emit a ``pad_pre_notification`` event → notification processor
        │   renders + sends it (same outbox path as dunning).
        └── charges: for every open installment DUE TODAY on an active loan
            with auto-charges enabled (plus past-due installments eligible for
            a retry under the policy below), claim an idempotent attempt row,
            then fire a Zumrails collection (pull).
                │  Zumrails webhook (app/api/webhooks/v1/endpoints/payments.py)
                ├── completed → loan_payments.on_collection_complete (existing
                │   settle path: record_payment, ledger allocation) +
                │   on_collection_settled here (attempt bookkeeping).
                └── failed → loan_payments.on_collection_failed (event) +
                    on_collection_failed_txn here: NSF fee → add-on bucket,
                    retry scheduling, dead-account auto-disable.

MASTER FLAG — ``settings.AUTO_COLLECTION_ENABLED`` (default **False**): the
engine is a strict no-op until it is flipped. Additionally the job no-ops when
no Zumrails adapter can be built from ``integration_settings`` — so "enabled
AND adapters on" are both required before a single cent moves. In tests /
staging the adapter is injected (simulation mode), mirroring
``loan_payments.initiate_payment``.

IDEMPOTENCY (the double-charge safety spine — MONEY PATH):

* One ``platform_collection_attempts`` row per (schedule_item, attempt #),
  enforced by a DB UNIQUE constraint. The row is claimed (COMMITTED) *before*
  the Zumrails call, so a crash-restart or a duplicate cron run finds the
  claim and never re-initiates.
* The Zumrails ``ClientTransactionId`` is deterministic —
  ``autocol-{schedule_item_id}-{attempt_number}`` — giving the rail a stable
  per-attempt dedupe handle.
* An attempt whose vendor outcome is UNKNOWN (network error mid-call) stays
  ``pending`` with no external ref and BLOCKS further auto attempts on that
  installment: we prefer a stalled installment (staff-resolvable, surfaced in
  logs) over any chance of a double pull.
* Settlement double-apply is separately impossible: ``record_payment`` dedupes
  on (loan_id, external_ref) — migration 033.

RETRY POLICY (PLACEHOLDER — flagged for Dave): defaults below retry the FULL
outstanding installment once, ``retry_delay_days=3`` days after a failure, to
a maximum of ``max_attempts=2`` attempts per installment; beyond that the
installment is left to dunning / the collections queue. Dave's "amount to
move" Excel (the 50%-tolerance partial-recovery algorithm from
04__WP_Collections) will replace the amount computation — the strategy seam
(``RETRY_AMOUNT_STRATEGIES``: policy + outstanding + prior attempts →
amount_cents) is where it drops in.

NSF (03__WP_Servicing / 04__WP_Collections): when the loan's product
PricingConfig carries an ENABLED ``nsf`` fee (Turnkey demo: $45, on-event,
add-on), a failed pull charges it as an immutable ledger ``fee`` row into the
NON-ACCRUING ADD-ON bucket (``add_on_cents``) — never loan fees, never
interest-bearing. At most one NSF fee per failed attempt
(``nsf_fee_transaction_id`` marker).

DEAD-ACCOUNT RETURN CODES (PLACEHOLDER LIST — flagged for Dave/Zumrails docs):
codes meaning the account can never pay (closed / not found / frozen / payor
deceased / stop payment) auto-disable the loan's auto-charges with a reason,
emit an ``auto_charge_disabled`` platform event (staff dashboard/audit
surface), and suppress all further retries — continuing to pull only racks up
processor dishonour fees (Dave, 03__WP_Servicing §f0026). Zumrails' concrete
return-code vocabulary is not pinned in their public docs, so matching keys on
the configurable ``dead_account_return_codes`` list (normalized substring
match); correct the list against real webhook captures.

PAD PRE-NOTIFICATION — LEGAL PLACEHOLDER, COUNSEL MUST CONFIRM COPY + TIMING:
Payments Canada Rule H1 requires the payee to give the payor advance notice of
the amount and date of each PAD. The H1 *default* notice period is longer than
the 3-business-day default used here (which matches the Turnkey demo cadence:
"Payment Reminder: Autopay in 7 Days" + 48-hour reminders); H1 permits
REDUCING or WAIVING the notice only when the payor expressly agrees in the PAD
agreement. Rule H1 also treats recurring fixed-amount PADs differently, and —
per the Turnkey settings video — WEEKLY-frequency PADs commonly rely on an
explicit pre-notification WAIVER in the PAD agreement because per-charge
notices at weekly cadence are impractical. ACTION FOR DAVE/COUNSEL before the
flag is flipped: (a) confirm the PaySpyre PAD agreement contains the
reduced-notice/waiver clause, (b) supply the exact regulatory notice copy for
the ``pad_pre_notification`` template, (c) confirm whether a policy-retry pull
needs its own notice. Statutory holidays are NOT excluded from the
business-day arithmetic here (weekends only) — same flag.

Everything money is integer cents. No new dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional, Sequence
from uuid import uuid4

from sqlalchemy import bindparam, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import (
    PlatformCollectionAttempt,
    PlatformLoan,
    PlatformLoanScheduleItem,
    PlatformLoanTransaction,
)
from app.schemas.pricing_config import (
    FeeCalc,
    FeeType,
    PricingConfig,
    PricingConfigError,
    parse_pricing_config,
)
from app.services import loan_ledger
from app.services.payments.zumrails_adapter import (
    PermanentZumrailsError,
    TransactionStatus,
    TransientZumrailsError,
)

logger = get_logger(__name__)


# Event types (platform_events vocabulary).
PRE_NOTIFICATION_EVENT = "pad_pre_notification"       # → notification processor
INITIATED_EVENT = "loan_payment_initiated"            # shared with Pay Now (webhook resolution)
AUTO_CHARGE_DISABLED_EVENT = "auto_charge_disabled"   # staff dashboard/audit trigger
AUTO_CHARGE_ENABLED_EVENT = "auto_charge_enabled"
NSF_FEE_CHARGED_EVENT = "nsf_fee_charged"
SKIPPED_EVENT = "auto_collection_skipped"

# Loan statuses whose installments are auto-charged (disbursed, still owing).
_CHASEABLE_LOAN_STATUSES = ("active", "delinquent")
# Installment statuses that still owe money (mirrors dunning / loan_payments).
_OPEN_ITEM_STATUSES = ("scheduled", "partial", "late")

# Attempt outcomes that count as terminal-and-retryable.
_RETRYABLE_OUTCOMES = ("failed", "cancelled")


# ---------------------------------------------------------------------------
# Policy — every business knob in one dataclass (mirrors DunningPolicy), with
# an optional DB override via the ``auto_collection`` integration_settings row.
# ALL DEFAULTS FLAGGED FOR DAVE (see module docstring).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoCollectionPolicy:
    """Auto-collection business policy. Values are data, not logic.

    * ``pre_notification_business_days`` — PAD pre-notification lead time
      (weekends excluded, statutory holidays NOT — flagged for counsel).
    * ``retry_delay_days`` / ``max_attempts`` — PLACEHOLDER retry ladder until
      Dave's "amount to move" Excel arrives: one retry of the full installment
      after 3 days, max 2 attempts per installment, then dunning/collections.
    * ``retry_amount_strategy`` — key into :data:`RETRY_AMOUNT_STRATEGIES`;
      Dave's partial-recovery algorithm drops in as a new strategy returning
      the amount_cents to attempt.
    * ``dead_account_return_codes`` — normalized-substring-matched codes that
      auto-disable auto-charges (PLACEHOLDER list pending the real Zumrails
      return-code vocabulary).
    """

    pre_notification_business_days: int = 3
    retry_delay_days: int = 3
    max_attempts: int = 2
    retry_amount_strategy: str = "full_installment"
    dead_account_return_codes: tuple[str, ...] = (
        "account_closed",
        "account_not_found",
        "cannot_locate_account",
        "no_account",
        "invalid_account",
        "payor_deceased",
        "account_frozen",
        "funds_frozen",
        "stop_payment",
    )


DEFAULT_POLICY = AutoCollectionPolicy()

# Integration-settings provider key for the optional DB override.
_POLICY_PROVIDER = "auto_collection"


def get_policy(db: Session) -> AutoCollectionPolicy:
    """Resolve the effective policy: the ``auto_collection`` integration
    settings row's config overlays the dataclass defaults (same DB-override
    seam as the notification rules over DunningPolicy). Missing / disabled row
    → pure defaults."""
    from app.services import integration_settings

    row = integration_settings.get(db, _POLICY_PROVIDER)
    if row is None or not row.enabled:
        return DEFAULT_POLICY
    cfg = row.config or {}
    kwargs: dict = {}
    for key in ("pre_notification_business_days", "retry_delay_days", "max_attempts"):
        if cfg.get(key) is not None:
            kwargs[key] = int(cfg[key])
    if cfg.get("retry_amount_strategy"):
        kwargs["retry_amount_strategy"] = str(cfg["retry_amount_strategy"])
    if cfg.get("dead_account_return_codes"):
        kwargs["dead_account_return_codes"] = tuple(
            str(c) for c in cfg["dead_account_return_codes"]
        )
    return AutoCollectionPolicy(**kwargs) if kwargs else DEFAULT_POLICY


# ---------------------------------------------------------------------------
# Retry-amount strategy seam (Dave's "amount to move" Excel drops in here).
# ---------------------------------------------------------------------------

#: (policy, outstanding_cents, prior_attempts) -> amount_cents to attempt.
RetryAmountStrategy = Callable[
    [AutoCollectionPolicy, int, Sequence["AttemptView"]], int
]


def _full_installment(
    policy: AutoCollectionPolicy,
    outstanding_cents: int,
    prior_attempts: Sequence["AttemptView"],
) -> int:
    """PLACEHOLDER default: retry the full outstanding installment amount.

    Dave's "amount to move" algorithm (50%-tolerance partial recovery,
    04__WP_Collections) replaces this: register it under its own key and point
    ``AutoCollectionPolicy.retry_amount_strategy`` at it."""
    return outstanding_cents


RETRY_AMOUNT_STRATEGIES: dict[str, RetryAmountStrategy] = {
    "full_installment": _full_installment,
}


def retry_amount_cents(
    policy: AutoCollectionPolicy,
    outstanding_cents: int,
    prior_attempts: Sequence["AttemptView"],
) -> int:
    strategy = RETRY_AMOUNT_STRATEGIES.get(
        policy.retry_amount_strategy, _full_installment
    )
    return max(0, min(outstanding_cents, strategy(policy, outstanding_cents, prior_attempts)))


# ---------------------------------------------------------------------------
# Pure planning core (DB-free, unit-tested against plain values)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChargeCandidate:
    """One open installment on an auto-charge-eligible loan (planner input)."""

    item_id: str
    loan_id: str
    due_date: date
    total_cents: int
    paid_cents: int = 0

    @property
    def outstanding_cents(self) -> int:
        return max(0, self.total_cents - self.paid_cents)


@dataclass(frozen=True)
class AttemptView:
    """Planner-facing view of one prior collection attempt."""

    attempt_number: int
    outcome: str  # pending | completed | failed | cancelled
    initiated_on: date


@dataclass(frozen=True)
class PlannedCharge:
    item_id: str
    loan_id: str
    attempt_number: int
    amount_cents: int
    kind: str  # "initial" | "retry"


def plan_charges(
    as_of: date,
    candidates: Sequence[ChargeCandidate],
    attempts_by_item: dict,
    policy: AutoCollectionPolicy = DEFAULT_POLICY,
) -> list[PlannedCharge]:
    """Decide which installments to pull today. PURE — the idempotency /
    retry policy brain, re-runnable any number of times with the same inputs.

    Rules:
      * nothing to charge (outstanding <= 0) → skip;
      * any PENDING attempt (in flight / vendor-unknown) → skip (never risk a
        parallel pull);
      * any COMPLETED attempt → skip (settled; schedule status catches up via
        record_payment — partial-recovery strategies revisit later, flagged);
      * no attempts → INITIAL charge only when the installment is due TODAY
        (installments already past due with no attempt history are dunning's
        business — the engine only auto-charges from its own due dates on);
      * all attempts terminal failed/cancelled → RETRY when attempts <
        ``max_attempts`` and ``as_of`` >= last attempt + ``retry_delay_days``
        (>= so a missed cron day still fires later); amount from the retry
        strategy. Beyond max attempts → left to dunning/collections.
    """
    planned: list[PlannedCharge] = []
    for cand in candidates:
        outstanding = cand.outstanding_cents
        if outstanding <= 0:
            continue
        attempts = sorted(
            attempts_by_item.get(cand.item_id, ()), key=lambda a: a.attempt_number
        )
        if any(a.outcome == "pending" for a in attempts):
            continue
        if any(a.outcome == "completed" for a in attempts):
            continue
        if not attempts:
            if cand.due_date == as_of:
                planned.append(
                    PlannedCharge(cand.item_id, cand.loan_id, 1, outstanding, "initial")
                )
            continue
        # Terminal, retryable history only from here.
        if not all(a.outcome in _RETRYABLE_OUTCOMES for a in attempts):
            continue  # defensive: unknown outcome value → do nothing
        if len(attempts) >= policy.max_attempts:
            continue
        last = attempts[-1]
        if as_of < last.initiated_on + timedelta(days=policy.retry_delay_days):
            continue
        amount = retry_amount_cents(policy, outstanding, attempts)
        if amount <= 0:
            continue
        planned.append(
            PlannedCharge(
                cand.item_id, cand.loan_id, last.attempt_number + 1, amount, "retry"
            )
        )
    return planned


def add_business_days(d: date, n: int) -> date:
    """``d`` advanced by ``n`` business days (weekends skipped; statutory
    holidays NOT — flagged for counsel alongside the H1 notice copy)."""
    cur = d
    remaining = n
    while remaining > 0:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            remaining -= 1
    return cur


def _normalize_code(code: Optional[str]) -> str:
    if not code:
        return ""
    out = []
    for ch in str(code).strip().lower():
        out.append(ch if ch.isalnum() else "_")
    normalized = "".join(out)
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized.strip("_")


def is_dead_account_code(
    return_code: Optional[str], policy: AutoCollectionPolicy = DEFAULT_POLICY
) -> bool:
    """True when a failure return code means the account can never pay
    (closed / not found / frozen / deceased / stop payment). Normalized
    substring match against the configurable code list, so both bare codes
    ("R02") and phrases ("Account Closed - R02") match once the real Zumrails
    vocabulary is configured."""
    ncode = _normalize_code(return_code)
    if not ncode:
        return False
    for entry in policy.dead_account_return_codes:
        nentry = _normalize_code(entry)
        if nentry and (nentry == ncode or nentry in ncode):
            return True
    return False


# ---------------------------------------------------------------------------
# NSF fee → add-on bucket (ledger primitives; idempotent per failed attempt)
# ---------------------------------------------------------------------------


def nsf_fee_cents(cfg: Optional[PricingConfig], principal_cents: int) -> Optional[int]:
    """The enabled NSF fee amount for a product config, in cents (or None).

    Fixed fees return their amount; rate fees are basis points of the ORIGINAL
    principal. Disabled / absent NSF fee → None (no charge)."""
    if cfg is None:
        return None
    for fee in cfg.fees:
        if fee.fee_type is FeeType.NSF and fee.enabled:
            if fee.calc is FeeCalc.RATE_BPS:
                amount = round(principal_cents * fee.amount / 10_000)
            else:
                amount = fee.amount
            return amount if amount > 0 else None
    return None


def charge_nsf_fee(
    db: Session,
    loan: PlatformLoan,
    attempt: PlatformCollectionAttempt,
    *,
    fee_cents: int,
    vendor_id=None,
    now: Optional[datetime] = None,
) -> Optional[PlatformLoanTransaction]:
    """Append the NSF fee as an immutable ledger ``fee`` row in the
    NON-ACCRUING ADD-ON bucket (Dave: NSF lives under add-on balance, never
    loan fees). Idempotent per failed attempt via
    ``attempt.nsf_fee_transaction_id``. Caller commits."""
    if attempt.nsf_fee_transaction_id is not None:
        return None  # already charged for this attempt
    now = now or datetime.now(timezone.utc)
    effective = now.date()
    seq = loan_ledger.next_seq(loan)
    txn = PlatformLoanTransaction(
        id=uuid4(),
        loan_id=loan.id,
        seq=seq,
        reference=loan_ledger.build_reference(vendor_id, loan.id, seq),
        txn_type="fee",
        payment_type=None,
        repayment_mode=None,
        amount_cents=fee_cents,
        principal_cents=0,
        interest_cents=0,
        fees_cents=0,
        add_on_cents=fee_cents,  # non-accruing add-on bucket
        effective_date=effective,
        processing_date=effective,
        created_by="auto_collection",
        comment=(
            f"NSF fee — failed auto-collection attempt {attempt.attempt_number} "
            f"(return code: {attempt.return_code or 'unknown'})"
        ),
    )
    loan.transactions.append(txn)  # keep the in-session ledger view consistent
    db.add(txn)
    attempt.nsf_fee_transaction_id = txn.id
    return txn


def _pricing_config_for_loan(db: Session, loan: PlatformLoan) -> Optional[PricingConfig]:
    """The loan's product PricingConfig (None for migrated loans without an
    application, or on a parse failure — fail SAFE: no config → no NSF fee)."""
    if loan.application_id is None:
        return None
    from app.models.platform.credit_application import PlatformCreditApplication
    from app.models.platform.credit_product import PlatformCreditProduct

    row = (
        db.query(PlatformCreditProduct.pricing_config)
        .join(
            PlatformCreditApplication,
            PlatformCreditApplication.credit_product_id == PlatformCreditProduct.id,
        )
        .filter(PlatformCreditApplication.id == loan.application_id)
        .first()
    )
    if row is None:
        return None
    try:
        return parse_pricing_config(row[0], context="auto-collection NSF fee")
    except PricingConfigError:
        logger.warning("auto_collection_pricing_parse_failed", loan_id=str(loan.id))
        return None


def _vendor_id_for_loan(db: Session, loan: PlatformLoan):
    if loan.application_id is None:
        return None
    from app.models.platform.credit_application import PlatformCreditApplication

    row = (
        db.query(PlatformCreditApplication.vendor_id)
        .filter(PlatformCreditApplication.id == loan.application_id)
        .first()
    )
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Failure / settlement handling (webhook entry points)
# ---------------------------------------------------------------------------


def effective_auto_charge(loan: PlatformLoan) -> bool:
    """Per-loan switch with platform default: NULL inherits enabled (the
    ENGINE gate is the separate AUTO_COLLECTION_ENABLED flag)."""
    enabled = getattr(loan, "auto_charge_enabled", None)
    return True if enabled is None else bool(enabled)


def handle_failed_attempt(
    db: Session,
    *,
    attempt: PlatformCollectionAttempt,
    loan: PlatformLoan,
    return_code: Optional[str],
    policy: AutoCollectionPolicy,
    pricing_cfg: Optional[PricingConfig],
    vendor_id=None,
    now: Optional[datetime] = None,
) -> None:
    """Core failure handling (DB-free-testable; caller commits):

    1. mark the attempt failed (+ return code);
    2. charge the product's enabled NSF fee into the add-on bucket —
       idempotent per failed attempt;
    3. dead-account return code → auto-disable auto-charges with reason +
       ``auto_charge_disabled`` platform event (staff dashboard trigger);
       the planner then never schedules another attempt for this loan.
    """
    if attempt.outcome == "completed":
        return  # never un-settle a completed attempt
    now = now or datetime.now(timezone.utc)
    attempt.outcome = "failed"
    if return_code:
        attempt.return_code = str(return_code)[:200]
    if attempt.completed_at is None:
        attempt.completed_at = now

    # NSF fee (only when the product defines an enabled NSF fee).
    fee_cents = nsf_fee_cents(pricing_cfg, loan.principal_cents)
    if fee_cents and attempt.nsf_fee_transaction_id is None:
        txn = charge_nsf_fee(
            db, loan, attempt, fee_cents=fee_cents, vendor_id=vendor_id, now=now
        )
        if txn is not None:
            _emit_event(
                db,
                NSF_FEE_CHARGED_EVENT,
                application_id=loan.application_id,
                payload={
                    "loan_id": str(loan.id),
                    "schedule_item_id": str(attempt.schedule_item_id),
                    "attempt_number": attempt.attempt_number,
                    "fee_cents": fee_cents,
                    "ledger_reference": txn.reference,
                    "return_code": attempt.return_code,
                },
            )

    # Dead-account return code → kill the auto-charge switch, notify staff.
    if is_dead_account_code(return_code, policy) and loan.auto_charge_enabled is not False:
        reason = f"dead-account return code: {str(return_code)[:200]}"
        loan.auto_charge_enabled = False
        loan.auto_charge_disabled_reason = reason
        _emit_event(
            db,
            AUTO_CHARGE_DISABLED_EVENT,
            application_id=loan.application_id,
            payload={
                "loan_id": str(loan.id),
                "reason": reason,
                "return_code": str(return_code)[:200],
                "disabled_by": "system:auto_collection",
                "schedule_item_id": str(attempt.schedule_item_id),
                "attempt_number": attempt.attempt_number,
            },
        )
        logger.warning(
            "auto_charge_disabled_dead_account",
            loan_id=str(loan.id),
            return_code=return_code,
        )


def _attempt_by_external_ref(
    db: Session, transaction_id: str
) -> Optional[PlatformCollectionAttempt]:
    return (
        db.query(PlatformCollectionAttempt)
        .filter(PlatformCollectionAttempt.external_ref == transaction_id)
        .first()
    )


def on_collection_settled(db: Session, transaction_id: str) -> bool:
    """Webhook hook (COMPLETED): mark the matching attempt completed. The money
    itself was already applied by ``loan_payments.on_collection_complete`` /
    ``record_payment`` (which dedupes on external_ref). Returns False when the
    txn is not an auto-collection attempt (e.g. borrower Pay Now)."""
    attempt = _attempt_by_external_ref(db, transaction_id)
    if attempt is None:
        return False
    if attempt.outcome != "completed":
        attempt.outcome = "completed"
        attempt.completed_at = attempt.completed_at or datetime.now(timezone.utc)
        db.commit()
    return True


def on_collection_failed_txn(
    db: Session,
    transaction_id: str,
    return_code: Optional[str] = None,
    *,
    policy: Optional[AutoCollectionPolicy] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Webhook hook (FAILED): full failure handling for an auto-collection
    attempt (NSF fee, retry bookkeeping, dead-account auto-disable). Commits.
    Returns False when the txn is not an auto-collection attempt. Idempotent:
    a webhook replay finds the attempt already failed and the NSF marker set."""
    attempt = _attempt_by_external_ref(db, transaction_id)
    if attempt is None:
        return False
    loan = (
        db.query(PlatformLoan).filter(PlatformLoan.id == attempt.loan_id).first()
    )
    if loan is None:  # pragma: no cover - FK guarantees this
        return False
    handle_failed_attempt(
        db,
        attempt=attempt,
        loan=loan,
        return_code=return_code,
        policy=policy or get_policy(db),
        pricing_cfg=_pricing_config_for_loan(db, loan),
        vendor_id=_vendor_id_for_loan(db, loan),
        now=now,
    )
    db.commit()
    logger.warning(
        "auto_collection_attempt_failed",
        loan_id=str(attempt.loan_id),
        schedule_item_id=str(attempt.schedule_item_id),
        attempt_number=attempt.attempt_number,
        return_code=return_code,
    )
    return True


def on_collection_cancelled(db: Session, transaction_id: str) -> bool:
    """Webhook hook (CANCELLED): terminal but NOT a payment-rail dishonour —
    no NSF fee, no dead-account logic. The attempt becomes retry-eligible
    under the normal policy. Commits."""
    attempt = _attempt_by_external_ref(db, transaction_id)
    if attempt is None:
        return False
    if attempt.outcome == "pending":
        attempt.outcome = "cancelled"
        attempt.completed_at = attempt.completed_at or datetime.now(timezone.utc)
        db.commit()
    return True


# ---------------------------------------------------------------------------
# PAD pre-notifications (Rule H1 mechanism — see module docstring LEGAL note)
# ---------------------------------------------------------------------------


def _fmt_cents(cents: Optional[int]) -> str:
    return "${:,.2f}".format((cents or 0) / 100)


def _fmt_date(d: date) -> str:
    return d.strftime("%B %d, %Y")


def _prenotify_rows(db: Session, target: date) -> list[dict]:
    """Open installments due on ``target`` on auto-charge-eligible loans,
    joined to borrower context (mirrors dunning's row shape)."""
    stmt = text(
        """
        SELECT s.id            AS item_id,
               s.total_cents   AS total_cents,
               s.paid_cents    AS paid_cents,
               s.due_date      AS due_date,
               l.id            AS loan_id,
               l.application_id AS application_id,
               a.patient_id    AS patient_id,
               p.legal_first_name AS first_name,
               p.legal_last_name  AS last_name
        FROM platform_loan_schedule s
        JOIN platform_loans l ON l.id = s.loan_id
        JOIN platform_credit_applications a ON a.id = l.application_id
        JOIN platform_patients p ON p.id = a.patient_id
        WHERE s.due_date = :target
          AND s.status IN :open_statuses
          AND l.status IN :loan_statuses
          AND (l.auto_charge_enabled IS TRUE OR l.auto_charge_enabled IS NULL)
        """
    ).bindparams(
        bindparam("open_statuses", expanding=True),
        bindparam("loan_statuses", expanding=True),
    )
    rows = db.execute(
        stmt,
        {
            "target": target,
            "open_statuses": list(_OPEN_ITEM_STATUSES),
            "loan_statuses": list(_CHASEABLE_LOAN_STATUSES),
        },
    ).mappings().all()
    return [dict(r) for r in rows]


def _already_emitted(db: Session, pad_key: str) -> bool:
    row = db.execute(
        text(
            """
            SELECT 1 FROM platform_events
            WHERE event_type = :etype AND payload->>'pad_key' = :key
            LIMIT 1
            """
        ),
        {"etype": PRE_NOTIFICATION_EVENT, "key": pad_key},
    ).first()
    return row is not None


def emit_pre_notifications(
    db: Session,
    as_of: date,
    policy: AutoCollectionPolicy = DEFAULT_POLICY,
    *,
    rows: Optional[list[dict]] = None,
    already_emitted: Optional[Callable[[Session, str], bool]] = None,
) -> int:
    """Emit one ``pad_pre_notification`` event per installment whose auto-charge
    is ``pre_notification_business_days`` business days out. Idempotent per
    installment via ``pad_key`` (one notice per scheduled charge; whether a
    policy RETRY needs its own notice is a flagged counsel question — none is
    sent today). The notification processor renders + sends it (email; province
    send windows enforced there). Caller commits."""
    target = add_business_days(as_of, policy.pre_notification_business_days)
    if rows is None:
        rows = _prenotify_rows(db, target)
    check = already_emitted or _already_emitted
    emitted = 0
    for row in rows:
        pad_key = f"{row['item_id']}:pad-pre"
        if check(db, pad_key):
            continue
        name = " ".join(
            p for p in (row.get("first_name"), row.get("last_name")) if p
        ).strip() or "there"
        outstanding = max(0, (row["total_cents"] or 0) - (row.get("paid_cents") or 0))
        base = settings.BORROWER_PORTAL_BASE_URL.rstrip("/")
        context = {
            "borrower_name": name,
            "loan_id": str(row["loan_id"])[:8],
            "payment_amount": _fmt_cents(outstanding),
            "charge_date": _fmt_date(row["due_date"]),
            "pre_notification_days": policy.pre_notification_business_days,
            "account_url": f"{base}/account",
        }
        _emit_event(
            db,
            PRE_NOTIFICATION_EVENT,
            patient_id=row.get("patient_id"),
            application_id=row.get("application_id"),
            payload={
                "loan_id": str(row["loan_id"]),
                "pad_key": pad_key,
                "channels": ["email"],
                "context": context,
            },
        )
        emitted += 1
    return emitted


# ---------------------------------------------------------------------------
# Charge execution + orchestration
# ---------------------------------------------------------------------------


@dataclass
class AutoCollectionResult:
    """Summary of one auto-collection run (job logging / tests)."""

    as_of: date
    enabled: bool = True
    adapter_available: bool = True
    pre_notifications_emitted: int = 0
    charges_planned: int = 0
    charges_initiated: int = 0
    charges_skipped: int = 0
    settled_synchronously: int = 0
    errors: int = 0
    initiated_refs: list[str] = field(default_factory=list)


def _payer_id_for_loan(db: Session, loan: PlatformLoan) -> Optional[str]:
    from app.services.loan_payments import _payer_id_for_loan as resolve

    return resolve(db, loan)


def _load_candidates(
    db: Session, as_of: date
) -> tuple[list[ChargeCandidate], dict, dict, dict]:
    """Load (candidates, attempts_by_item, loans_by_id, items_by_id) for the
    planner: open installments on chaseable, auto-charge-eligible loans that
    are either due today or carry attempt history (retry candidates)."""
    attempted_items = db.query(PlatformCollectionAttempt.schedule_item_id)
    rows = (
        db.query(PlatformLoanScheduleItem, PlatformLoan)
        .join(PlatformLoan, PlatformLoanScheduleItem.loan_id == PlatformLoan.id)
        .filter(
            PlatformLoan.status.in_(_CHASEABLE_LOAN_STATUSES),
            or_(
                PlatformLoan.auto_charge_enabled.is_(True),
                PlatformLoan.auto_charge_enabled.is_(None),
            ),
            PlatformLoanScheduleItem.status.in_(_OPEN_ITEM_STATUSES),
            or_(
                PlatformLoanScheduleItem.due_date == as_of,
                PlatformLoanScheduleItem.id.in_(attempted_items),
            ),
        )
        .all()
    )
    candidates: list[ChargeCandidate] = []
    loans_by_id: dict = {}
    items_by_id: dict = {}
    for item, loan in rows:
        candidates.append(
            ChargeCandidate(
                item_id=item.id,
                loan_id=loan.id,
                due_date=item.due_date,
                total_cents=item.total_cents or 0,
                paid_cents=item.paid_cents or 0,
            )
        )
        loans_by_id[loan.id] = loan
        items_by_id[item.id] = item

    attempts_by_item: dict = {}
    if items_by_id:
        attempt_rows = (
            db.query(PlatformCollectionAttempt)
            .filter(
                PlatformCollectionAttempt.schedule_item_id.in_(list(items_by_id))
            )
            .all()
        )
        for a in attempt_rows:
            attempts_by_item.setdefault(a.schedule_item_id, []).append(
                AttemptView(
                    attempt_number=a.attempt_number,
                    outcome=a.outcome,
                    initiated_on=(
                        a.initiated_at.date()
                        if isinstance(a.initiated_at, datetime)
                        else a.initiated_at
                    ),
                )
            )
    return candidates, attempts_by_item, loans_by_id, items_by_id


def execute_charge(
    db: Session,
    planned: PlannedCharge,
    loan: PlatformLoan,
    zumrails,
    *,
    payer_resolver: Optional[Callable] = None,
    now: Optional[datetime] = None,
    policy: AutoCollectionPolicy = DEFAULT_POLICY,
) -> str:
    """Execute one planned charge. Returns one of ``"initiated"``,
    ``"settled"``, ``"skipped"``, ``"errored"``.

    Order of operations is the crash-safety contract:
      1. CLAIM the attempt row and COMMIT — the unique (item, attempt #)
         constraint makes duplicate claims impossible;
      2. call Zumrails with the deterministic ClientTransactionId;
      3. record the external ref + emit ``loan_payment_initiated`` (the event
         the webhook resolves collections by) + COMMIT.
    A crash between 2 and 3 leaves a ``pending`` attempt with no external ref,
    which BLOCKS further auto attempts on the installment — conservative by
    design (staff-resolvable; never a double pull)."""
    resolve_payer = payer_resolver or _payer_id_for_loan
    now = now or datetime.now(timezone.utc)

    payer_id = resolve_payer(db, loan)
    if not payer_id:
        logger.warning(
            "auto_collection_no_funding_profile",
            loan_id=str(loan.id),
            schedule_item_id=str(planned.item_id),
        )
        return "skipped"

    attempt = PlatformCollectionAttempt(
        id=uuid4(),
        loan_id=loan.id,
        schedule_item_id=planned.item_id,
        attempt_number=planned.attempt_number,
        amount_cents=planned.amount_cents,
        client_transaction_id=f"autocol-{planned.item_id}-{planned.attempt_number}",
        outcome="pending",
        initiated_at=now,
        created_by="auto_collection",
    )
    db.add(attempt)
    try:
        db.commit()  # claim BEFORE the vendor call (idempotency spine)
    except IntegrityError:
        db.rollback()  # concurrent duplicate run already claimed it
        return "skipped"

    try:
        result = zumrails.create_collection(
            payer_id=payer_id,
            amount_cents=planned.amount_cents,
            client_transaction_id=attempt.client_transaction_id,
            memo=f"Scheduled loan payment {loan.id}",
        )
    except TransientZumrailsError as exc:
        # Vendor state UNKNOWN (network/5xx mid-call): leave the attempt
        # 'pending' with no external ref — blocks further auto attempts on
        # this installment rather than risking a double pull.
        attempt.error = f"transient: {exc}"[:500]
        db.commit()
        logger.error(
            "auto_collection_initiate_unknown_state",
            loan_id=str(loan.id),
            schedule_item_id=str(planned.item_id),
            attempt_number=planned.attempt_number,
        )
        return "errored"
    except PermanentZumrailsError as exc:
        # Vendor rejected the request outright — nothing is in flight. Counts
        # toward max_attempts and retries per policy. NOT an NSF event (the
        # rail never attempted the pull), so no fee.
        attempt.outcome = "failed"
        attempt.return_code = "adapter_permanent_error"
        attempt.error = str(exc)[:500]
        attempt.completed_at = now
        db.commit()
        logger.error(
            "auto_collection_initiate_rejected",
            loan_id=str(loan.id),
            schedule_item_id=str(planned.item_id),
            attempt_number=planned.attempt_number,
        )
        return "errored"

    attempt.external_ref = result.transaction_id
    _emit_event(
        db,
        INITIATED_EVENT,
        application_id=loan.application_id,
        payload={
            # Fields loan_payments._find_initiation resolves settlements by:
            "transaction_id": result.transaction_id,
            "client_transaction_id": attempt.client_transaction_id,
            "amount_cents": planned.amount_cents,
            "status": result.status.value,
            "loan_id": str(loan.id),
            # Auto-collection provenance:
            "source": "auto_collection",
            "schedule_item_id": str(planned.item_id),
            "attempt_number": planned.attempt_number,
            "kind": planned.kind,
        },
    )
    db.commit()
    logger.info(
        "auto_collection_initiated",
        loan_id=str(loan.id),
        schedule_item_id=str(planned.item_id),
        attempt_number=planned.attempt_number,
        amount_cents=planned.amount_cents,
        transaction_id=result.transaction_id,
    )

    # Some rails ack terminally in the create response (mock adapter / sync
    # rails) — settle or fail right away, mirroring loan_payments.
    if result.status == TransactionStatus.COMPLETED:
        from app.services import loan_payments

        loan_payments.on_collection_complete(db, result.transaction_id)
        on_collection_settled(db, result.transaction_id)
        return "settled"
    if result.status == TransactionStatus.FAILED:
        on_collection_failed_txn(
            db, result.transaction_id, return_code=result.raw_status, policy=policy
        )
        return "initiated"
    return "initiated"


def run_auto_collection(
    db: Session,
    as_of: date,
    *,
    policy: Optional[AutoCollectionPolicy] = None,
    zumrails=None,
    payer_resolver: Optional[Callable] = None,
    now: Optional[datetime] = None,
) -> AutoCollectionResult:
    """One full engine pass for ``as_of``: PAD pre-notifications, then charges.

    STRICT NO-OP unless BOTH gates pass:
      * ``settings.AUTO_COLLECTION_ENABLED`` is True (master money flag), and
      * a Zumrails adapter is available (injected, or built from the enabled
        ``zumrails`` integration_settings row — "real adapters on").
    Safe to re-run any number of times per day (attempt claims + pad_key
    dedupe make the second run a no-op)."""
    result = AutoCollectionResult(as_of=as_of)

    if not settings.AUTO_COLLECTION_ENABLED:
        result.enabled = False
        return result

    if zumrails is None:
        from app.services.loan_lifecycle import _build_zumrails_adapter

        zumrails = _build_zumrails_adapter(db)
    if zumrails is None:
        result.adapter_available = False
        logger.warning("auto_collection_no_adapter")
        return result

    policy = policy or get_policy(db)

    # 1) PAD pre-notifications for charges N business days out.
    result.pre_notifications_emitted = emit_pre_notifications(db, as_of, policy)
    db.commit()

    # 2) Today's charges + policy retries.
    candidates, attempts_by_item, loans_by_id, _items = _load_candidates(db, as_of)
    planned = plan_charges(as_of, candidates, attempts_by_item, policy)
    result.charges_planned = len(planned)
    for charge in planned:
        loan = loans_by_id.get(charge.loan_id)
        if loan is None:  # pragma: no cover - loader invariant
            continue
        outcome = execute_charge(
            db,
            charge,
            loan,
            zumrails,
            payer_resolver=payer_resolver,
            now=now,
            policy=policy,
        )
        if outcome == "initiated":
            result.charges_initiated += 1
        elif outcome == "settled":
            result.charges_initiated += 1
            result.settled_synchronously += 1
        elif outcome == "skipped":
            result.charges_skipped += 1
        else:
            result.errors += 1

    logger.info(
        "auto_collection_run_complete",
        as_of=as_of.isoformat(),
        pre_notifications=result.pre_notifications_emitted,
        planned=result.charges_planned,
        initiated=result.charges_initiated,
        settled_sync=result.settled_synchronously,
        skipped=result.charges_skipped,
        errors=result.errors,
    )
    return result


# ---------------------------------------------------------------------------
# shared event emit
# ---------------------------------------------------------------------------


def _emit_event(
    db: Session,
    event_type: str,
    *,
    actor: str = "system",
    patient_id=None,
    application_id=None,
    payload: dict,
) -> None:
    body = {
        "v": 1,
        "actor": {"type": "system", "id": "system"},
        "application_id": str(application_id) if application_id else None,
        "patient_id": str(patient_id) if patient_id else None,
        **payload,
    }
    db.add(
        PlatformEvent(
            event_type=event_type,
            actor=actor,
            patient_id=patient_id,
            application_id=application_id,
            payload=body,
        )
    )
    db.flush()
