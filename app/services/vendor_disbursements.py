"""Vendor self-serve disbursements engine (W2-DISB, Turnkey parity video 10).

Dave's ask (10__Vendor_Access §2/§4/§5 "Disbursements — the big new ask"):
give each vendor a place to see, on their portfolio,

    • month-to-date collections,
    • the amount **due to the vendor** (assuming collections clear), and
    • the amount **available to request** as a disbursement **right now**,

then let the vendor **control when they get paid** — a free automated monthly
payout of the undispersed balance, plus optional intra-month **extra** payouts
that carry a transaction fee — with the payout fired straight through the
payment-processor integration (Zumrails) so the vendor is paid **directly by the
processor** (keeping PaySpyre out of "intermediary" territory).

    monthly cron (app/jobs/vendor_disbursements.py)  ── free sweep
        │  run_monthly_auto_disbursement()
    clinic endpoint (POST .../disbursements/request) ── extra, fee-charged
        │  request_extra_payout()
        └── execute_disbursement(): CLAIM a pending row (committed) BEFORE the
            Zumrails push (idempotency spine, mirrors the auto-collection
            attempt), push via create_disbursement, settle on the terminal ack
            / webhook.

================================ SAFETY ========================================
MONEY-OUT MASTER FLAG — ``settings.VENDOR_DISBURSEMENTS_ENABLED`` (default
**False**): both the monthly sweep and the extra-payout path are STRICT no-ops
until it is flipped. Additionally the push no-ops when no Zumrails adapter can be
built from ``integration_settings`` (real/sandbox creds) AND when the vendor has
no resolved disbursement recipient — so "flag on AND adapters on AND recipient
mapped" are ALL required before a single cent moves. Wallet READS never move
money and are always live (the dashboard renders with the flag off). Amounts are
integer cents everywhere. This module only READS the money ledger — it never
writes ``platform_loan_transactions`` and never touches interest/ledger math.

WALLET DERIVATION (DB-free-testable pure core below): the wallet is NOT stored;
it is computed from the money ledger minus this engine's own settled + in-flight
payouts:

    cleared_collected = net payments (payment − reversal) on the vendor's loans
                        with effective_date <= holdback_cutoff
    due_to_vendor     = cleared_collected * VENDOR_DISBURSEMENT_SHARE_BPS/10000
    available         = max(0, due_to_vendor − disbursed_settled − in_flight)

CLEARING HOLDBACK (Dave: "take away any payments made within the last four
business days"): ``holdback_cutoff`` = ``VENDOR_DISBURSEMENT_HOLDBACK_BUSINESS_DAYS``
business days before ``as_of``, using the WS-F business calendar
(``app.services.business_calendar`` — weekends + statutory holidays + admin
closures). ``next_business_day`` there is forward-only, so the backward walk
below leans on its sibling ``is_business_day`` from the same module.

FLAGGED FOR DAVE (must be confirmed before the flag is flipped):
* ``VENDOR_DISBURSEMENT_SHARE_BPS`` — the vendor's share of cleared collections.
  Default 10000 (100%) is a PLACEHOLDER; the real funding/revenue split is
  Dave's call.
* ``VENDOR_DISBURSEMENT_EXTRA_FEE_CENTS`` — the per-extra-payout fee. Default 0
  is a PLACEHOLDER.
* Vendor→Zumrails recipient mapping — there is no ``recipients`` wiring yet, so
  ``_recipient_id_for_vendor`` returns None (every push safely skips) until a
  mapping is supplied. Tests / staging inject a resolver.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Optional
from uuid import uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.platform.event import PlatformEvent
from app.models.platform.vendor_disbursement import PlatformVendorDisbursement
from app.services import business_calendar
from app.services.payments.zumrails_adapter import (
    PermanentZumrailsError,
    TransactionStatus,
    TransientZumrailsError,
)

logger = get_logger(__name__)


# Event types (platform_events vocabulary).
INITIATED_EVENT = "vendor_disbursement_initiated"
COMPLETED_EVENT = "vendor_disbursement_completed"
FAILED_EVENT = "vendor_disbursement_failed"

KIND_AUTO = "auto_monthly"
KIND_EXTRA = "extra"

_SETTLED_STATUSES = ("completed",)
_IN_FLIGHT_STATUSES = ("pending", "processing")

BPS_DENOMINATOR = 10_000


class DisbursementError(Exception):
    """Domain error surfaced to the clinic endpoint (mapped to 4xx)."""


# ---------------------------------------------------------------------------
# Pure core (DB-free, unit-tested against plain values)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalletSnapshot:
    """Derived vendor wallet (all integer cents). Not stored — recomputed on
    every read from the money ledger + this engine's payouts."""

    vendor_id: str
    as_of: date
    holdback_cutoff: date
    share_bps: int
    holdback_business_days: int
    # Ledger-derived collections (net of reversals).
    mtd_collected_cents: int
    total_collected_cents: int
    cleared_collected_cents: int
    held_back_cents: int
    # Vendor entitlement + payout accounting.
    due_to_vendor_cents: int
    disbursed_settled_cents: int
    disbursed_in_flight_cents: int
    available_cents: int

    def as_dict(self) -> dict:
        return {
            "vendor_id": str(self.vendor_id),
            "as_of": self.as_of.isoformat(),
            "holdback_cutoff": self.holdback_cutoff.isoformat(),
            "holdback_business_days": self.holdback_business_days,
            "share_bps": self.share_bps,
            "mtd_collected_cents": self.mtd_collected_cents,
            "total_collected_cents": self.total_collected_cents,
            "cleared_collected_cents": self.cleared_collected_cents,
            "held_back_cents": self.held_back_cents,
            "due_to_vendor_cents": self.due_to_vendor_cents,
            "disbursed_settled_cents": self.disbursed_settled_cents,
            "disbursed_in_flight_cents": self.disbursed_in_flight_cents,
            "available_cents": self.available_cents,
        }


def vendor_share_cents(collected_cents: int, share_bps: int) -> int:
    """Vendor's share of ``collected_cents`` at ``share_bps`` (10000 = 100%).

    Floored (integer cents; never round UP a money-out entitlement). Negative
    collected (net reversals exceed payments) floors at 0."""
    if collected_cents <= 0:
        return 0
    bps = max(0, share_bps)
    return (collected_cents * bps) // BPS_DENOMINATOR


def compute_available(
    cleared_collected_cents: int,
    disbursed_settled_cents: int,
    disbursed_in_flight_cents: int,
    share_bps: int,
) -> int:
    """PURE availability: entitlement on cleared collections minus everything
    already paid or in flight. Floored at 0."""
    due = vendor_share_cents(cleared_collected_cents, share_bps)
    return max(0, due - disbursed_settled_cents - disbursed_in_flight_cents)


def plan_extra_payout(available_cents: int, fee_cents: int) -> int:
    """Net amount to push for an EXTRA payout: the whole available balance less
    the fee. Raises ``DisbursementError`` when nothing is releasable or the fee
    would swallow the balance (net must be strictly positive — a payout that
    only pays the fee is never issued)."""
    fee = max(0, fee_cents)
    if available_cents <= 0:
        raise DisbursementError("No funds available to disburse.")
    net = available_cents - fee
    if net <= 0:
        raise DisbursementError(
            "Available balance does not cover the disbursement fee."
        )
    return net


def _business_days_before(
    d: date, n: int, db: Optional[Session] = None, province: Optional[str] = None
) -> date:
    """The date ``n`` business days before ``d`` (WS-F calendar). ``next_business_day``
    in business_calendar is forward-only, so this backward walk reuses that
    module's ``is_business_day`` (weekends + statutory holidays + admin
    closures). ``n<=0`` returns ``d``."""
    from datetime import timedelta

    if n <= 0:
        return d
    cur = d
    remaining = n
    # Bounded scan — cannot loop forever with a sane calendar.
    for _ in range(n * 5 + 60):
        cur -= timedelta(days=1)
        if business_calendar.is_business_day(cur, province, db):
            remaining -= 1
            if remaining == 0:
                return cur
    raise RuntimeError(  # pragma: no cover - impossible with a sane calendar
        f"could not find {n} business days before {d}"
    )


# ---------------------------------------------------------------------------
# Ledger reads (static SQL — B608 safe; parameterized, no interpolation)
# ---------------------------------------------------------------------------


def _collected_buckets(
    db: Session, vendor_id, as_of: date, cutoff: date
) -> tuple[int, int, int]:
    """(mtd, cleared, total) net collections (payment − reversal) on the
    vendor's portfolio. ``mtd`` = current calendar month of ``as_of``;
    ``cleared`` = effective_date <= ``cutoff`` (releasable past the holdback).
    Reversals net conservatively against collected (never over-pays a vendor)."""
    month_start = date(as_of.year, as_of.month, 1)
    stmt = text(
        """
        SELECT
          COALESCE(SUM(CASE
              WHEN t.txn_type = 'payment'  AND t.effective_date >= :month_start THEN t.amount_cents
              WHEN t.txn_type = 'reversal' AND t.effective_date >= :month_start THEN -t.amount_cents
              ELSE 0 END), 0) AS mtd_cents,
          COALESCE(SUM(CASE
              WHEN t.txn_type = 'payment'  AND t.effective_date <= :cutoff THEN t.amount_cents
              WHEN t.txn_type = 'reversal' AND t.effective_date <= :cutoff THEN -t.amount_cents
              ELSE 0 END), 0) AS cleared_cents,
          COALESCE(SUM(CASE
              WHEN t.txn_type = 'payment'  THEN t.amount_cents
              WHEN t.txn_type = 'reversal' THEN -t.amount_cents
              ELSE 0 END), 0) AS total_cents
        FROM platform_loan_transactions t
        JOIN platform_loans l ON l.id = t.loan_id
        JOIN platform_credit_applications a ON a.id = l.application_id
        WHERE a.vendor_id = :vendor_id
        """
    )
    row = db.execute(
        stmt,
        {"vendor_id": vendor_id, "month_start": month_start, "cutoff": cutoff},
    ).mappings().first()
    if row is None:  # pragma: no cover - COALESCE guarantees a row
        return 0, 0, 0
    return int(row["mtd_cents"]), int(row["cleared_cents"]), int(row["total_cents"])


def _disbursed_totals(db: Session, vendor_id) -> tuple[int, int]:
    """(settled, in_flight) totals of ``amount_cents + fee_cents`` for the
    vendor's payouts. Failed/cancelled payouts are excluded (they released no
    money)."""
    stmt = text(
        """
        SELECT
          COALESCE(SUM(CASE WHEN status IN :settled
                            THEN amount_cents + fee_cents ELSE 0 END), 0) AS settled,
          COALESCE(SUM(CASE WHEN status IN :in_flight
                            THEN amount_cents + fee_cents ELSE 0 END), 0) AS in_flight
        FROM platform_vendor_disbursements
        WHERE vendor_id = :vendor_id
        """
    ).bindparams(
        bindparam("settled", expanding=True),
        bindparam("in_flight", expanding=True),
    )
    row = db.execute(
        stmt,
        {
            "vendor_id": vendor_id,
            "settled": list(_SETTLED_STATUSES),
            "in_flight": list(_IN_FLIGHT_STATUSES),
        },
    ).mappings().first()
    if row is None:  # pragma: no cover
        return 0, 0
    return int(row["settled"]), int(row["in_flight"])


def compute_wallet(db: Session, vendor_id, as_of: Optional[date] = None) -> WalletSnapshot:
    """Derive the vendor wallet as of ``as_of`` (default: today, UTC). READ ONLY
    — moves no money, needs no flag; always safe to call for the dashboard."""
    as_of = as_of or datetime.now(timezone.utc).date()
    holdback_days = settings.VENDOR_DISBURSEMENT_HOLDBACK_BUSINESS_DAYS
    share_bps = settings.VENDOR_DISBURSEMENT_SHARE_BPS
    cutoff = _business_days_before(as_of, holdback_days, db)

    mtd, cleared, total = _collected_buckets(db, vendor_id, as_of, cutoff)
    settled, in_flight = _disbursed_totals(db, vendor_id)
    due = vendor_share_cents(cleared, share_bps)
    available = max(0, due - settled - in_flight)

    return WalletSnapshot(
        vendor_id=str(vendor_id),
        as_of=as_of,
        holdback_cutoff=cutoff,
        share_bps=share_bps,
        holdback_business_days=holdback_days,
        mtd_collected_cents=mtd,
        total_collected_cents=total,
        cleared_collected_cents=cleared,
        held_back_cents=max(0, total - cleared),
        due_to_vendor_cents=due,
        disbursed_settled_cents=settled,
        disbursed_in_flight_cents=in_flight,
        available_cents=available,
    )


# ---------------------------------------------------------------------------
# Recipient + adapter resolution (both must succeed before any money moves)
# ---------------------------------------------------------------------------


def _recipient_id_for_vendor(db: Session, vendor_id) -> Optional[str]:
    """Resolve the vendor's Zumrails recipient id (the payee "UserId").

    PLACEHOLDER: there is no vendor→recipient mapping wired yet, so this returns
    None (every push safely SKIPS) until the mapping is supplied. Optionally the
    ``vendor_disbursements`` integration_settings row may carry a
    ``{"recipients": {"<vendor_id>": "<zum_user_id>"}}`` map — read here so
    staging can wire recipients without a schema change. Tests inject a resolver
    directly. Never returns a raw bank account number (no PII reaches the
    adapter)."""
    try:
        from app.services import integration_settings as isvc

        row = isvc.get(db, "vendor_disbursements")
    except Exception:  # pragma: no cover - integration_settings optional
        row = None
    if row is None or not getattr(row, "enabled", False):
        return None
    recipients = (row.config or {}).get("recipients") or {}
    rid = recipients.get(str(vendor_id))
    return str(rid) if rid else None


def _build_adapter(db: Session):
    from app.services.loan_lifecycle import _build_zumrails_adapter

    return _build_zumrails_adapter(db)


# ---------------------------------------------------------------------------
# Execution (claim-before-push idempotency spine, mirrors execute_charge)
# ---------------------------------------------------------------------------


@dataclass
class DisbursementResult:
    """Outcome of one payout attempt (job logging / tests)."""

    status: str  # initiated | settled | failed | skipped | duplicate | errored
    disbursement_id: Optional[str] = None
    external_ref: Optional[str] = None
    amount_cents: int = 0
    fee_cents: int = 0


def execute_disbursement(
    db: Session,
    *,
    vendor_id,
    kind: str,
    net_cents: int,
    fee_cents: int,
    holdback_cutoff: date,
    client_transaction_id: str,
    requested_by: str,
    recipient_id: str,
    zumrails,
    period: Optional[tuple[int, int]] = None,
    now: Optional[datetime] = None,
) -> DisbursementResult:
    """Push one payout. Crash-safety contract (identical to auto-collection):

    1. CLAIM the ``pending`` row and COMMIT — the UNIQUE client_transaction_id
       makes a duplicate claim impossible (the monthly sweep's deterministic id
       gives free per-vendor-per-month idempotency);
    2. call Zumrails ``create_disbursement`` with that id;
    3. record the external ref, emit ``vendor_disbursement_initiated``, COMMIT.
    A crash between 2 and 3 leaves a ``pending`` row with no external ref, which
    a duplicate run cannot re-push (unique id) — conservative by design."""
    now = now or datetime.now(timezone.utc)
    row = PlatformVendorDisbursement(
        id=uuid4(),
        vendor_id=vendor_id,
        kind=kind,
        status="pending",
        amount_cents=net_cents,
        fee_cents=max(0, fee_cents),
        holdback_cutoff=holdback_cutoff,
        period_year=period[0] if period else None,
        period_month=period[1] if period else None,
        client_transaction_id=client_transaction_id,
        requested_by=requested_by,
    )
    db.add(row)
    try:
        db.commit()  # claim BEFORE the vendor call (idempotency spine)
    except IntegrityError:
        db.rollback()  # duplicate id already claimed (e.g. monthly re-run)
        return DisbursementResult(status="duplicate")

    try:
        result = zumrails.create_disbursement(
            recipient_id=recipient_id,
            amount_cents=net_cents,
            client_transaction_id=client_transaction_id,
            memo=f"PaySpyre vendor payout ({kind})",
        )
    except TransientZumrailsError as exc:
        # Vendor state UNKNOWN — leave the row 'pending' with no external ref;
        # the unique id blocks any re-push. Staff-resolvable.
        row.error = f"transient: {exc}"[:500]
        db.commit()
        logger.error(
            "vendor_disbursement_initiate_unknown_state",
            vendor_id=str(vendor_id),
            disbursement_id=str(row.id),
        )
        return DisbursementResult(
            status="errored", disbursement_id=str(row.id),
            amount_cents=net_cents, fee_cents=row.fee_cents,
        )
    except PermanentZumrailsError as exc:
        row.status = "failed"
        row.return_code = "adapter_permanent_error"
        row.error = str(exc)[:500]
        row.completed_at = now
        db.commit()
        _emit(db, FAILED_EVENT, vendor_id, requested_by, {
            "disbursement_id": str(row.id), "kind": kind,
            "amount_cents": net_cents, "fee_cents": row.fee_cents,
            "return_code": row.return_code,
        })
        db.commit()
        logger.error(
            "vendor_disbursement_initiate_rejected",
            vendor_id=str(vendor_id),
            disbursement_id=str(row.id),
        )
        return DisbursementResult(
            status="failed", disbursement_id=str(row.id),
            amount_cents=net_cents, fee_cents=row.fee_cents,
        )

    row.external_ref = result.transaction_id
    row.status = "processing"
    _emit(db, INITIATED_EVENT, vendor_id, requested_by, {
        "disbursement_id": str(row.id),
        "kind": kind,
        "amount_cents": net_cents,
        "fee_cents": row.fee_cents,
        "transaction_id": result.transaction_id,
        "client_transaction_id": client_transaction_id,
        "status": result.status.value,
    })
    db.commit()
    logger.info(
        "vendor_disbursement_initiated",
        vendor_id=str(vendor_id),
        disbursement_id=str(row.id),
        kind=kind,
        amount_cents=net_cents,
        fee_cents=row.fee_cents,
        transaction_id=result.transaction_id,
    )

    # Simulator / sync rails ack terminally in the create response — settle now.
    if result.status == TransactionStatus.COMPLETED:
        _settle(db, row, now=now, requested_by=requested_by)
        return DisbursementResult(
            status="settled", disbursement_id=str(row.id),
            external_ref=result.transaction_id,
            amount_cents=net_cents, fee_cents=row.fee_cents,
        )
    if result.status == TransactionStatus.FAILED:
        _fail(db, row, return_code=result.raw_status, now=now, requested_by=requested_by)
        return DisbursementResult(
            status="failed", disbursement_id=str(row.id),
            external_ref=result.transaction_id,
            amount_cents=net_cents, fee_cents=row.fee_cents,
        )
    return DisbursementResult(
        status="initiated", disbursement_id=str(row.id),
        external_ref=result.transaction_id,
        amount_cents=net_cents, fee_cents=row.fee_cents,
    )


def _settle(
    db: Session, row: PlatformVendorDisbursement, *, now: datetime, requested_by: str
) -> None:
    if row.status == "completed":
        return
    row.status = "completed"
    row.completed_at = row.completed_at or now
    _emit(db, COMPLETED_EVENT, row.vendor_id, requested_by, {
        "disbursement_id": str(row.id), "kind": row.kind,
        "amount_cents": row.amount_cents, "fee_cents": row.fee_cents,
        "external_ref": row.external_ref,
    })
    db.commit()


def _fail(
    db: Session,
    row: PlatformVendorDisbursement,
    *,
    return_code: Optional[str],
    now: datetime,
    requested_by: str,
) -> None:
    if row.status == "completed":
        return  # never un-settle a completed payout
    row.status = "failed"
    if return_code:
        row.return_code = str(return_code)[:200]
    row.completed_at = row.completed_at or now
    _emit(db, FAILED_EVENT, row.vendor_id, requested_by, {
        "disbursement_id": str(row.id), "kind": row.kind,
        "amount_cents": row.amount_cents, "fee_cents": row.fee_cents,
        "return_code": row.return_code, "external_ref": row.external_ref,
    })
    db.commit()


# ---------------------------------------------------------------------------
# Webhook hooks (settlement lands async in production)
# ---------------------------------------------------------------------------


def _by_external_ref(db: Session, transaction_id: str) -> Optional[PlatformVendorDisbursement]:
    return (
        db.query(PlatformVendorDisbursement)
        .filter(PlatformVendorDisbursement.external_ref == transaction_id)
        .first()
    )


def on_disbursement_settled(db: Session, transaction_id: str) -> bool:
    """Webhook hook (COMPLETED): mark the matching payout completed. Returns
    False when the txn is not a vendor disbursement (e.g. a loan collection)."""
    row = _by_external_ref(db, transaction_id)
    if row is None:
        return False
    _settle(db, row, now=datetime.now(timezone.utc), requested_by=row.requested_by)
    return True


def on_disbursement_failed(
    db: Session, transaction_id: str, return_code: Optional[str] = None
) -> bool:
    """Webhook hook (FAILED): mark the matching payout failed — releasing its
    held ``amount+fee`` back into the vendor's available balance (a failed
    payout counts toward neither settled nor in-flight). Returns False when not
    a vendor disbursement. Idempotent."""
    row = _by_external_ref(db, transaction_id)
    if row is None:
        return False
    _fail(
        db, row, return_code=return_code,
        now=datetime.now(timezone.utc), requested_by=row.requested_by,
    )
    return True


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def request_extra_payout(
    db: Session,
    vendor_id,
    *,
    requested_by: str,
    zumrails=None,
    recipient_resolver: Optional[Callable] = None,
    now: Optional[datetime] = None,
    as_of: Optional[date] = None,
) -> DisbursementResult:
    """On-demand EXTRA payout of the vendor's whole available balance, less the
    configured fee. Raises ``DisbursementError`` (→ 4xx) when disabled, when no
    adapter/recipient is available, or when nothing is releasable.

    STRICT NO-OP GATE: raises unless ``VENDOR_DISBURSEMENTS_ENABLED`` is True."""
    if not settings.VENDOR_DISBURSEMENTS_ENABLED:
        raise DisbursementError("Vendor disbursements are not enabled.")

    now = now or datetime.now(timezone.utc)
    wallet = compute_wallet(db, vendor_id, as_of)
    net = plan_extra_payout(wallet.available_cents, settings.VENDOR_DISBURSEMENT_EXTRA_FEE_CENTS)
    fee = settings.VENDOR_DISBURSEMENT_EXTRA_FEE_CENTS

    zumrails = zumrails if zumrails is not None else _build_adapter(db)
    if zumrails is None:
        raise DisbursementError("Payment processor is not configured.")

    resolve = recipient_resolver or _recipient_id_for_vendor
    recipient_id = resolve(db, vendor_id)
    if not recipient_id:
        raise DisbursementError("No disbursement recipient is configured for this vendor.")

    return execute_disbursement(
        db,
        vendor_id=vendor_id,
        kind=KIND_EXTRA,
        net_cents=net,
        fee_cents=fee,
        holdback_cutoff=wallet.holdback_cutoff,
        client_transaction_id=f"vdisb-extra-{uuid4().hex}",
        requested_by=requested_by,
        recipient_id=recipient_id,
        zumrails=zumrails,
        now=now,
    )


@dataclass
class MonthlyRunResult:
    as_of: date
    enabled: bool = True
    adapter_available: bool = True
    vendors_scanned: int = 0
    payouts_initiated: int = 0
    payouts_settled: int = 0
    skipped: int = 0
    duplicates: int = 0
    errors: int = 0
    initiated_ids: list[str] = field(default_factory=list)


def _vendor_ids_with_activity(db: Session) -> list:
    """Vendors that have any collected payment on their portfolio (payout
    candidates). Static SQL."""
    stmt = text(
        """
        SELECT DISTINCT a.vendor_id
        FROM platform_loan_transactions t
        JOIN platform_loans l ON l.id = t.loan_id
        JOIN platform_credit_applications a ON a.id = l.application_id
        WHERE t.txn_type = 'payment' AND a.vendor_id IS NOT NULL
        """
    )
    return [r[0] for r in db.execute(stmt).all()]


def run_monthly_auto_disbursement(
    db: Session,
    as_of: Optional[date] = None,
    *,
    zumrails=None,
    recipient_resolver: Optional[Callable] = None,
    now: Optional[datetime] = None,
) -> MonthlyRunResult:
    """Free monthly sweep: for every vendor with a positive available balance,
    push the whole balance (fee 0). Idempotent per vendor+month via the
    deterministic ``vdisb-auto-{vendor}-{YYYYMM}`` id — a re-run in the same
    month is a no-op (duplicate claim rolled back). Vendors with no resolved
    recipient are skipped (no money moves).

    STRICT NO-OP unless BOTH gates pass:
      * ``VENDOR_DISBURSEMENTS_ENABLED`` is True, and
      * a Zumrails adapter is available (injected or built from the enabled
        ``zumrails`` integration_settings row)."""
    as_of = as_of or datetime.now(timezone.utc).date()
    now = now or datetime.now(timezone.utc)
    result = MonthlyRunResult(as_of=as_of)

    if not settings.VENDOR_DISBURSEMENTS_ENABLED:
        result.enabled = False
        return result

    zumrails = zumrails if zumrails is not None else _build_adapter(db)
    if zumrails is None:
        result.adapter_available = False
        logger.warning("vendor_disbursement_no_adapter")
        return result

    resolve = recipient_resolver or _recipient_id_for_vendor
    period = (as_of.year, as_of.month)

    for vendor_id in _vendor_ids_with_activity(db):
        result.vendors_scanned += 1
        wallet = compute_wallet(db, vendor_id, as_of)
        if wallet.available_cents <= 0:
            result.skipped += 1
            continue
        recipient_id = resolve(db, vendor_id)
        if not recipient_id:
            result.skipped += 1
            logger.info(
                "vendor_disbursement_no_recipient", vendor_id=str(vendor_id)
            )
            continue
        ctid = f"vdisb-auto-{vendor_id}-{as_of.year}{as_of.month:02d}"
        outcome = execute_disbursement(
            db,
            vendor_id=vendor_id,
            kind=KIND_AUTO,
            net_cents=wallet.available_cents,
            fee_cents=0,
            holdback_cutoff=wallet.holdback_cutoff,
            client_transaction_id=ctid,
            requested_by="system:vendor_disbursement",
            recipient_id=recipient_id,
            zumrails=zumrails,
            period=period,
            now=now,
        )
        if outcome.status in ("initiated", "settled"):
            result.payouts_initiated += 1
            if outcome.status == "settled":
                result.payouts_settled += 1
            if outcome.disbursement_id:
                result.initiated_ids.append(outcome.disbursement_id)
        elif outcome.status == "duplicate":
            result.duplicates += 1
        elif outcome.status == "failed":
            result.errors += 1
        else:
            result.errors += 1

    logger.info(
        "vendor_disbursement_monthly_complete",
        as_of=as_of.isoformat(),
        scanned=result.vendors_scanned,
        initiated=result.payouts_initiated,
        settled=result.payouts_settled,
        skipped=result.skipped,
        duplicates=result.duplicates,
        errors=result.errors,
    )
    return result


# ---------------------------------------------------------------------------
# History read (endpoint helper)
# ---------------------------------------------------------------------------


def list_disbursements(db: Session, vendor_id, *, limit: int = 100) -> list[dict]:
    """The vendor's payout history, newest first (dashboard 'Transactions')."""
    rows = (
        db.query(PlatformVendorDisbursement)
        .filter(PlatformVendorDisbursement.vendor_id == vendor_id)
        .order_by(PlatformVendorDisbursement.created_at.desc())
        .limit(min(max(1, limit), 500))
        .all()
    )
    return [_serialize(r) for r in rows]


def _serialize(row: PlatformVendorDisbursement) -> dict:
    return {
        "id": str(row.id),
        "vendor_id": str(row.vendor_id),
        "kind": row.kind,
        "status": row.status,
        "amount_cents": row.amount_cents,
        "fee_cents": row.fee_cents,
        "holdback_cutoff": row.holdback_cutoff.isoformat() if row.holdback_cutoff else None,
        "period_year": row.period_year,
        "period_month": row.period_month,
        "external_ref": row.external_ref,
        "return_code": row.return_code,
        "requested_by": row.requested_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


# ---------------------------------------------------------------------------
# shared event emit (platform_events audit)
# ---------------------------------------------------------------------------


def _emit(db: Session, event_type: str, vendor_id, actor: str, payload: dict) -> None:
    body = {
        "v": 1,
        "actor": {"type": "system" if actor.startswith("system:") else "clinic", "id": actor},
        "vendor_id": str(vendor_id),
        **payload,
    }
    db.add(
        PlatformEvent(
            event_type=event_type,
            actor=actor,
            payload=body,
        )
    )
    db.flush()
