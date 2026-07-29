"""Import a loan portfolio from ANY servicing system into PaySpyre.

This is the DB-write half of the portfolio import. The shape of the source file
is declared in a :mod:`portfolio_profile`; :mod:`portfolio_workbook` turns it
into normalized records; this module lands those records as vendors, providers,
borrowers, loans and ledger history, and then RECONCILES what it wrote against
what the source stated.

WHAT GETS WRITTEN
-----------------
vendors      resolved by the source's own vendor code (``vendors.external_code``);
             created only when ``create_missing_vendors`` is on.
providers    the vendor's roster (``platform_providers``) — from the vendor
             sheet's provider block plus every provider named on an account.
borrowers    ``platform_patients``, keyed by the source's customer id when it has
             one and otherwise by (vendor, borrower name). Missing contact
             details are synthesized ONLY under an explicit
             :class:`PlaceholderPolicy`.
loans        ``platform_loans`` with ``source='portfolio_import'``,
             ``application_id`` NULL, ``legacy_account_number`` = the source
             account number, and the source's STATED current balance.
history      every transaction becomes an immutable ledger row
             (``platform_loan_transactions``), plus a cash receipt
             (``platform_loan_payments``) for rows the profile marks as cash.

THE FIDELITY RULE
-----------------
Each transaction's fees / interest / principal allocation is written EXACTLY as
the source recorded it, sign included. Nothing here re-derives an allocation, and
nothing re-applies history to a balance: the loan's outstanding principal is the
figure the source states, full stop. Where the derived history and the stated
balances disagree, the run REPORTS the exception — it does not adjust it. That is
the whole point of a migration: preserve the claim, surface the contradiction.

IDEMPOTENCY
-----------
Every write is keyed on a stable identifier — vendor code, (vendor, provider
name), borrower key, ``legacy_account_number``, and per-transaction external
ref. Re-running an import creates nothing it already created.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.loan import Vendor
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanPayment,
    PlatformLoanScheduleItem,
    PlatformLoanTransaction,
)
from app.models.platform.patient import PlatformPatient
from app.models.platform.patient_field import PlatformPatientField
from app.models.platform.provider import PROVIDER_SOURCE_IMPORT
from app.services import providers as providers_service
from app.services.loan_servicing import generate_amortization_schedule
from app.services.migration import constants
from app.services.migration.borrower_completion import (
    NO_PLACEHOLDERS,
    PlaceholderPolicy,
    generate_contact,
)
from app.services.migration.portfolio_profile import (
    PortfolioProfile,
    TransactionTypeRule,
    get_profile,
)
from app.services.migration.portfolio_reconcile import (
    DEFAULT_TOLERANCE_CENTS,
    PersistedLoanTotals,
    ReconciliationReport,
    reconcile_persisted,
    reconcile_source,
)
from app.services.migration.portfolio_workbook import (
    PortfolioAccount,
    PortfolioTransaction,
    PortfolioVendor,
    WorkbookReadResult,
    iter_by_account,
    split_person_name,
)

#: Ledger ``created_by`` / payment ``method`` provenance tag.
ACTOR = constants.IMPORT_METHOD


@dataclass
class ImportOptions:
    """Everything an operator decides about ONE import run."""

    profile_name: Optional[str] = None
    #: Synthesize missing borrower contact details? Off unless asked.
    placeholders: PlaceholderPolicy = NO_PLACEHOLDERS
    #: Create vendors the book references but PaySpyre does not have yet.
    create_missing_vendors: bool = True
    #: Build a forward amortization schedule for still-active loans.
    build_forward_schedule: bool = True
    #: Cent tolerance when tying derived figures to the source's stated ones.
    tolerance_cents: int = DEFAULT_TOLERANCE_CENTS
    #: Interest convention for a generated forward schedule. ``actual/360`` is
    #: what the first migrated book reconciled to; ``generate_amortization_schedule``
    #: accepts '30/360' or 'actual/360'.
    day_count: str = "actual/360"

    def describe(self) -> dict:
        return {
            "profile": self.profile_name,
            "placeholders": self.placeholders.describe(),
            "create_missing_vendors": self.create_missing_vendors,
            "build_forward_schedule": self.build_forward_schedule,
            "tolerance_cents": self.tolerance_cents,
            "day_count": self.day_count,
        }


@dataclass
class ImportResult:
    dry_run: bool = False
    options: dict = field(default_factory=dict)
    read_report: dict = field(default_factory=dict)

    vendors_created: int = 0
    vendors_matched: int = 0
    vendors_missing: list[str] = field(default_factory=list)
    providers_created: int = 0
    providers_existing: int = 0
    borrowers_created: int = 0
    borrowers_matched: int = 0
    borrowers_completed: int = 0
    placeholder_fields: dict = field(default_factory=dict)

    loans_created: int = 0
    loans_skipped_existing: int = 0
    loans_skipped_status: list[str] = field(default_factory=list)
    loans_unmapped_status: list[str] = field(default_factory=list)
    schedules_built: int = 0

    ledger_rows_created: int = 0
    payment_receipts_created: int = 0
    transactions_skipped_duplicate: int = 0
    transactions_unmapped_type: dict = field(default_factory=dict)
    transactions_orphan_account: int = 0
    reversals_linked: int = 0
    reversals_unlinked: int = 0

    warnings: list[str] = field(default_factory=list)
    source_reconciliation: dict = field(default_factory=dict)
    persisted_reconciliation: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "options": self.options,
            "source": self.read_report,
            "vendors": {
                "created": self.vendors_created,
                "matched": self.vendors_matched,
                "missing": self.vendors_missing[:100],
            },
            "providers": {
                "created": self.providers_created,
                "existing": self.providers_existing,
            },
            "borrowers": {
                "created": self.borrowers_created,
                "matched": self.borrowers_matched,
                "completed_with_placeholders": self.borrowers_completed,
                "placeholder_fields": self.placeholder_fields,
            },
            "loans": {
                "created": self.loans_created,
                "skipped_existing": self.loans_skipped_existing,
                "skipped_by_status": self.loans_skipped_status[:100],
                "unmapped_status": self.loans_unmapped_status[:100],
                "forward_schedules_built": self.schedules_built,
            },
            "transactions": {
                "ledger_rows_created": self.ledger_rows_created,
                "payment_receipts_created": self.payment_receipts_created,
                "skipped_duplicate": self.transactions_skipped_duplicate,
                "unmapped_types": self.transactions_unmapped_type,
                "orphan_account_rows": self.transactions_orphan_account,
                "reversals_linked": self.reversals_linked,
                "reversals_unlinked": self.reversals_unlinked,
            },
            "warnings": self.warnings[:200],
            "reconciliation": {
                "source": self.source_reconciliation,
                "persisted": self.persisted_reconciliation,
            },
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_dt(d: Optional[date]) -> Optional[datetime]:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) if d else None


def _parse_vendor_address(raw: Optional[str]) -> dict:
    """Split a one-line vendor address into the columns ``vendors`` requires.

    "2033 Gordon Dr., Kelowna, BC. V1Y 3J2" -> line1 / city / province / postal.
    Deliberately forgiving: a shape it cannot split lands entirely in line1 so
    nothing is lost.
    """
    out = {"address_line1": (raw or "").strip() or "Unknown",
           "city": "Unknown", "province": "BC", "postal_code": "000000"}
    if not raw:
        return out
    parts = [p.strip().rstrip(".") for p in str(raw).split(",") if p.strip()]
    if len(parts) >= 3:
        out["address_line1"] = parts[0]
        out["city"] = parts[1]
        tail = parts[2].split()
        if tail:
            out["province"] = tail[0].rstrip(".").upper()[:50]
            if len(tail) > 1:
                out["postal_code"] = " ".join(tail[1:])[:10]
    elif len(parts) == 2:
        out["address_line1"], out["city"] = parts[0], parts[1]
    return out


def _months_inclusive(start: date, end: date) -> int:
    return max(0, (end.year - start.year) * 12 + (end.month - start.month) + 1)


# ---------------------------------------------------------------------------
# Stage 1 — vendors
# ---------------------------------------------------------------------------


def _resolve_vendors(
    db: Session,
    read: WorkbookReadResult,
    options: ImportOptions,
    result: ImportResult,
) -> dict[str, Vendor]:
    """code -> Vendor. Matches on ``external_code``, then on business name."""
    by_code: dict[str, Vendor] = {}
    existing = db.query(Vendor).all()
    by_external = {
        (v.external_code or "").strip().upper(): v for v in existing if v.external_code
    }
    by_name = {(v.business_name or "").strip().casefold(): v for v in existing}

    # Codes the book actually references (accounts are the authority; the vendor
    # sheet may list vendors with no loans in this export).
    referenced = {
        (a.vendor_code or "").strip().upper() for a in read.accounts if a.vendor_code
    }
    sheet_by_code: dict[str, PortfolioVendor] = {
        (v.code or "").strip().upper(): v for v in read.vendors if v.code
    }

    for code in sorted(referenced):
        vendor = by_external.get(code)
        if vendor is None:
            info = sheet_by_code.get(code)
            if info and info.name:
                vendor = by_name.get(info.name.strip().casefold())
            if vendor is not None:
                # Adopt the code so the next run matches directly.
                vendor.external_code = code
        if vendor is not None:
            result.vendors_matched += 1
            by_code[code] = vendor
            continue

        info = sheet_by_code.get(code)
        if not options.create_missing_vendors or info is None or not info.name:
            result.vendors_missing.append(code)
            if info is None:
                result.warnings.append(
                    f"vendor {code}: referenced by accounts but absent from the vendor "
                    "sheet — loans keep their account data but have no vendor link"
                )
            continue

        addr = _parse_vendor_address(info.address)
        vendor = Vendor(
            external_code=code,
            business_name=info.name,
            business_type="corporation",
            contact_name=info.name,
            email=info.email or f"{code.lower()}@unknown.invalid",
            phone=info.phone or "000-000-0000",
            status="active",
            **addr,
        )
        db.add(vendor)
        db.flush()
        by_code[code] = vendor
        result.vendors_created += 1
    return by_code


# ---------------------------------------------------------------------------
# Stage 2 — providers
# ---------------------------------------------------------------------------


def _seed_providers(
    db: Session,
    read: WorkbookReadResult,
    vendors: dict[str, Vendor],
    result: ImportResult,
) -> dict[tuple[str, str], Any]:
    """(vendor_code, provider match-key) -> PlatformProvider."""
    rosters: dict[str, list[str]] = {}
    for v in read.vendors:
        code = (v.code or "").strip().upper()
        if code:
            rosters.setdefault(code, []).extend(v.providers)
    for a in read.accounts:
        code = (a.vendor_code or "").strip().upper()
        if code and a.provider_name:
            rosters.setdefault(code, []).append(a.provider_name)

    resolved: dict[tuple[str, str], Any] = {}
    for code, names in rosters.items():
        vendor = vendors.get(code)
        if vendor is None:
            continue
        created, existing = providers_service.seed_roster(
            db, vendor.id, names, source=PROVIDER_SOURCE_IMPORT
        )
        result.providers_created += created
        result.providers_existing += existing
        for p in providers_service.roster(db, vendor.id, active_only=False):
            key = providers_service.match_key(p.name)
            if key:
                resolved[(code, key)] = p
    return resolved


# ---------------------------------------------------------------------------
# Stage 3 — borrowers
# ---------------------------------------------------------------------------


def borrower_key(account: PortfolioAccount) -> str:
    """The stable identity an imported borrower is deduped on.

    A servicing export rarely carries a customer id — the loan book's unit is the
    ACCOUNT. So identity falls back to (vendor, borrower name): the same person
    with three loans at one clinic is one borrower, and two same-named people at
    the SAME clinic would merge. That risk is stated rather than hidden; a source
    that does carry a customer id maps it through the profile and this key is
    never used.
    """
    vendor = (account.vendor_code or "").strip().upper()
    name = " ".join((account.borrower_name or "").split()).casefold()
    return f"{vendor}:{name}" if name else f"{vendor}:acct-{account.account_number}"


def _existing_borrowers(db: Session) -> dict[str, PlatformPatient]:
    """Legacy-customer-key -> patient, across both the neutral and legacy keys."""
    rows = (
        db.query(PlatformPatientField, PlatformPatient)
        .join(PlatformPatient, PlatformPatient.id == PlatformPatientField.patient_id)
        .filter(
            PlatformPatientField.field_key.in_(constants.LEGACY_CUSTOMER_FIELD_KEYS),
            PlatformPatientField.is_current.is_(True),
        )
        .all()
    )
    out: dict[str, PlatformPatient] = {}
    for f, p in rows:
        value = f.field_value
        if isinstance(value, dict):
            value = value.get("value")
        if value is not None:
            out.setdefault(str(value), p)
    return out


def _import_borrowers(
    db: Session,
    read: WorkbookReadResult,
    profile: PortfolioProfile,
    options: ImportOptions,
    result: ImportResult,
) -> dict[str, PlatformPatient]:
    known = _existing_borrowers(db)
    out: dict[str, PlatformPatient] = {}
    field_counts: dict[str, int] = {}

    for account in read.accounts:
        key = borrower_key(account)
        if key in out:
            continue
        patient = known.get(key)
        if patient is not None:
            out[key] = patient
            result.borrowers_matched += 1
            continue

        first, last = split_person_name(account.borrower_name, profile.name_format)
        contact = generate_contact(
            key=key,
            first_name=first,
            last_name=last,
            policy=options.placeholders,
            vendor_code=account.vendor_code,
        )
        patient = PlatformPatient(
            legal_first_name=first,
            legal_last_name=last,
            email=contact.email,
            phone_e164=contact.phone_e164,
        )
        db.add(patient)
        db.flush()
        db.add(
            PlatformPatientField(
                patient_id=patient.id,
                field_key=constants.LEGACY_CUSTOMER_FIELD_KEY,
                field_value={"value": key, "account_number": account.account_number},
                source=constants.IMPORT_FIELD_SOURCE,
            )
        )
        address = contact.address_dict()
        if address:
            db.add(
                PlatformPatientField(
                    patient_id=patient.id,
                    field_key=constants.IMPORT_ADDRESS_FIELD_KEY,
                    field_value=address,
                    source=constants.IMPORT_FIELD_SOURCE,
                )
            )
        if contact.generated_fields:
            result.borrowers_completed += 1
            for f in contact.generated_fields:
                field_counts[f] = field_counts.get(f, 0) + 1
        out[key] = patient
        known[key] = patient
        result.borrowers_created += 1

    result.placeholder_fields = field_counts
    return out


# ---------------------------------------------------------------------------
# Stage 4 — loans
# ---------------------------------------------------------------------------


def _forward_schedule(account: PortfolioAccount, options: ImportOptions, result: ImportResult):
    balance = account.principal_balance_cents or 0
    if balance <= 0 or not account.next_due_date or not account.final_payment_date:
        return []
    if not account.annual_rate_bps:
        return []
    remaining = _months_inclusive(account.next_due_date, account.final_payment_date)
    if remaining <= 0:
        result.warnings.append(
            f"account {account.account_number}: non-positive remaining term — "
            "no forward schedule built"
        )
        return []
    return generate_amortization_schedule(
        balance,
        account.annual_rate_bps,
        remaining,
        account.next_due_date,
        day_count=options.day_count,
    )


def _import_loans(
    db: Session,
    read: WorkbookReadResult,
    profile: PortfolioProfile,
    options: ImportOptions,
    vendors: dict[str, Vendor],
    provider_rows: dict[tuple[str, str], Any],
    borrowers: dict[str, PlatformPatient],
    result: ImportResult,
) -> dict[str, PlatformLoan]:
    existing = {
        acct: loan
        for acct, loan in db.query(PlatformLoan.legacy_account_number, PlatformLoan)
        .filter(PlatformLoan.legacy_account_number.isnot(None))
        .all()
    }
    out: dict[str, PlatformLoan] = {}

    for account in read.accounts:
        acct = account.account_number
        if acct in existing:
            out[acct] = existing[acct]
            result.loans_skipped_existing += 1
            continue
        if profile.is_skipped_status(account.source_status, account.source_sub_status):
            result.loans_skipped_status.append(acct)
            continue
        status = profile.map_status(account.source_status, account.source_sub_status)
        if status is None:
            result.loans_unmapped_status.append(
                f"{acct} ({account.source_status}/{account.source_sub_status})"
            )
            continue

        code = (account.vendor_code or "").strip().upper()
        vendor = vendors.get(code)
        provider = provider_rows.get(
            (code, providers_service.match_key(account.provider_name) or "")
        )

        # CLOSED loans carry no outstanding balance; ACTIVE loans carry the
        # source's STATED balance verbatim.
        balance = account.principal_balance_cents or 0
        if status in ("paid_off", "cancelled") and balance:
            result.warnings.append(
                f"account {acct}: closed as {status} but states an outstanding "
                f"principal balance of {balance} cents — imported as stated"
            )

        frequency = profile.map_frequency(account.payment_frequency)
        if frequency is None and account.payment_frequency:
            result.warnings.append(
                f"account {acct}: unmapped payment frequency "
                f"{account.payment_frequency!r} — booked monthly"
            )

        loan = PlatformLoan(
            application_id=None,
            patient_id=borrowers.get(borrower_key(account)).id
            if borrowers.get(borrower_key(account))
            else None,
            vendor_id=vendor.id if vendor else None,
            provider_id=provider.id if provider is not None else None,
            source=constants.PORTFOLIO_SOURCE,
            legacy_account_number=acct,
            principal_cents=account.amount_financed_cents or 0,
            annual_rate_bps=account.annual_rate_bps or 0,
            term_months=account.term_months or 0,
            payment_frequency=frequency or "monthly",
            status=status,
            principal_balance_cents=balance,
            disbursed_at=_to_dt(account.origination_date),
            closed_at=_to_dt(account.close_date),
            closed_at_source="transition" if account.close_date else None,
            # An imported loan was already agreed and funded in its source system.
            agreement_status="signed",
            disbursement_status="completed",
            currency="CAD",
        )
        if options.build_forward_schedule and status in ("active", "delinquent"):
            rows = _forward_schedule(account, options, result)
            for r in rows:
                loan.schedule.append(
                    PlatformLoanScheduleItem(
                        installment_number=r.installment_number,
                        due_date=r.due_date,
                        principal_cents=r.principal_cents,
                        interest_cents=r.interest_cents,
                        total_cents=r.total_cents,
                        status="scheduled",
                        paid_cents=0,
                    )
                )
            if rows:
                result.schedules_built += 1
        db.add(loan)
        db.flush()
        out[acct] = loan
        existing[acct] = loan
        result.loans_created += 1
    return out


# ---------------------------------------------------------------------------
# Stage 5 — transaction history
# ---------------------------------------------------------------------------


def _external_ref(txn: PortfolioTransaction) -> str:
    """A stable per-transaction reference for idempotent re-import."""
    if txn.transaction_number:
        return f"{constants.REF_PREFIX_SUPPLIED}{txn.transaction_number}"
    stamp = txn.date.isoformat() if txn.date else "nodate"
    return (
        f"{constants.REF_PREFIX_DERIVED}{txn.account_number}:{stamp}:"
        f"{txn.payment_cents or 0}:{txn.source_type}"
    )


def _ledger_amount(txn: PortfolioTransaction, rule: TransactionTypeRule) -> int:
    """The ledger's non-negative ``amount_cents`` for one source row.

    Transcription, not derivation: a cash row's amount is its stated Payment; an
    ORIGINATION row states no payment (the advance IS the opening balance it
    leaves behind), so that balance is the disbursement amount. Sign lives in
    ``txn_type`` (a reversal is a reversal) and in the allocation columns, which
    keep whatever sign the source recorded.
    """
    if txn.payment_cents:
        return abs(txn.payment_cents)
    if rule.opens_loan and txn.principal_balance_cents:
        return abs(txn.principal_balance_cents)
    return 0


def _import_transactions(
    db: Session,
    read: WorkbookReadResult,
    profile: PortfolioProfile,
    loans: dict[str, PlatformLoan],
    result: ImportResult,
) -> None:
    grouped = iter_by_account(read.transactions)
    loan_ids = [loan.id for loan in loans.values()]

    # Existing refs, so a re-run inserts nothing twice. Both prefixes are
    # checked (a book imported before the rename carries the legacy one).
    seen_refs: set[tuple[Any, str]] = set()
    if loan_ids:
        for loan_id, ref in (
            db.query(PlatformLoanPayment.loan_id, PlatformLoanPayment.external_ref)
            .filter(
                PlatformLoanPayment.loan_id.in_(loan_ids),
                PlatformLoanPayment.external_ref.isnot(None),
            )
            .all()
        ):
            seen_refs.add((loan_id, ref))

    for acct, rows in grouped.items():
        loan = loans.get(acct)
        if loan is None:
            result.transactions_orphan_account += len(rows)
            continue

        # A LOAN'S HISTORY IS IMPORTED WHOLESALE, ONCE. The ledger is immutable
        # and carries no per-row external reference, so partial top-ups cannot be
        # deduped row-by-row for non-cash rows (an adjustment leaves no payment
        # receipt to key on). Re-running therefore skips any loan the importer has
        # already written history for — which is the safe direction: a migration
        # must never risk a second copy of a borrower's payment record.
        if any(t.created_by in constants.IMPORT_METHODS for t in (loan.transactions or [])):
            result.transactions_skipped_duplicate += len(rows)
            continue

        seq = max((t.seq for t in (loan.transactions or [])), default=0)
        # Cash rows already on the ledger, newest last — the reversal resolver
        # walks this backwards.
        cash_stack: list[PlatformLoanTransaction] = [
            t for t in (loan.transactions or []) if t.txn_type == "payment"
        ]
        reversed_ids: set[Any] = {
            t.reverses_transaction_id
            for t in (loan.transactions or [])
            if t.reverses_transaction_id is not None
        }

        for txn in rows:
            rule = profile.rule_for(txn.source_type)
            if rule is None:
                result.transactions_unmapped_type[txn.source_type] = (
                    result.transactions_unmapped_type.get(txn.source_type, 0) + 1
                )
                continue

            ref = _external_ref(txn)
            variants = (
                constants.supplied_ref_variants(txn.transaction_number)
                if txn.transaction_number
                else (ref,)
            )
            if any((loan.id, v) in seen_refs for v in variants):
                result.transactions_skipped_duplicate += 1
                continue

            amount = _ledger_amount(txn, rule)
            txn_type = rule.ledger_type
            reverses_id = None
            comment = " | ".join(
                p for p in (txn.source_type, txn.comment) if p
            )[:2000]

            if rule.is_reversal:
                target = None
                for candidate in reversed(cash_stack):
                    if candidate.id in reversed_ids:
                        continue
                    if candidate.amount_cents == amount:
                        target = candidate
                        break
                if target is not None:
                    reverses_id = target.id
                    reversed_ids.add(target.id)
                    result.reversals_linked += 1
                else:
                    # The ledger requires a reversal to name the row it undoes.
                    # Rather than invent a link, record the return as an
                    # adjustment carrying the source type verbatim in its comment.
                    txn_type = "adjustment"
                    result.reversals_unlinked += 1

            seq += 1
            ledger_row = PlatformLoanTransaction(
                loan_id=loan.id,
                seq=seq,
                reference=f"{loan.vendor_id or 'none'}-{loan.id}-{seq}",
                txn_type=txn_type,
                payment_type="eft" if rule.is_cash else None,
                repayment_mode=rule.repayment_mode,
                amount_cents=amount,
                # AS RECORDED — signs preserved, never re-derived.
                principal_cents=txn.principal_paid_cents or 0,
                interest_cents=txn.interest_paid_cents or 0,
                fees_cents=txn.fees_paid_cents or 0,
                add_on_cents=0,
                effective_date=txn.date,
                processing_date=txn.date,
                reverses_transaction_id=reverses_id,
                created_by=ACTOR,
                comment=comment or None,
            )
            # Appended through the relationship (cascade owns the insert), then
            # flushed so the row has an id the NEXT reversal can point at.
            loan.transactions.append(ledger_row)
            db.flush()
            result.ledger_rows_created += 1
            if txn_type == "payment":
                cash_stack.append(ledger_row)

            # A cash receipt mirrors real money in, so payment-history readers
            # (portal, statements, analytics) see the same events.
            if rule.is_cash and (txn.payment_cents or 0) > 0:
                db.add(
                    PlatformLoanPayment(
                        loan_id=loan.id,
                        amount_cents=txn.payment_cents,
                        received_at=_to_dt(txn.date),
                        method=f"{constants.IMPORT_METHOD}:{txn.source_type}",
                        external_ref=ref,
                    )
                )
                seen_refs.add((loan.id, ref))
                result.payment_receipts_created += 1


# ---------------------------------------------------------------------------
# Reconciliation of what actually landed
# ---------------------------------------------------------------------------


def _persisted_totals(
    db: Session, loans: dict[str, PlatformLoan]
) -> dict[str, PersistedLoanTotals]:
    out: dict[str, PersistedLoanTotals] = {}
    for acct, loan in loans.items():
        rows = list(loan.transactions or [])
        out[acct] = PersistedLoanTotals(
            account_number=acct,
            principal_balance_cents=loan.principal_balance_cents or 0,
            fees_paid_cents=sum(r.fees_cents or 0 for r in rows),
            interest_paid_cents=sum(r.interest_cents or 0 for r in rows),
            principal_paid_cents=sum(r.principal_cents or 0 for r in rows),
            # The total AMOUNT MOVED is the sum of the SIGNED allocations, not of
            # ``amount_cents``: the ledger's amount column is constrained
            # non-negative, so a return or a negative correction would count the
            # wrong way round. The allocations are stored exactly as the source
            # recorded them, signs included, so their sum is the source's own
            # "payment amount" figure by construction.
            payment_total_cents=sum(
                (r.principal_cents or 0) + (r.interest_cents or 0) + (r.fees_cents or 0)
                for r in rows
            ),
            ledger_rows=len(rows),
        )
    return out


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def preview(read: WorkbookReadResult, options: ImportOptions) -> ImportResult:
    """Everything that can be said about a file WITHOUT touching the database:
    what it contains, and whether it reconciles against itself."""
    result = ImportResult(dry_run=True, options=options.describe())
    result.read_report = read.as_report()
    source_report = reconcile_source(
        read.accounts, read.transactions, tolerance_cents=options.tolerance_cents
    )
    result.source_reconciliation = source_report.as_dict()
    profile = get_profile(options.profile_name)
    for account in read.accounts:
        if profile.is_skipped_status(account.source_status, account.source_sub_status):
            result.loans_skipped_status.append(account.account_number)
        elif profile.map_status(account.source_status, account.source_sub_status) is None:
            result.loans_unmapped_status.append(
                f"{account.account_number} "
                f"({account.source_status}/{account.source_sub_status})"
            )
    for txn in read.transactions:
        if profile.rule_for(txn.source_type) is None:
            result.transactions_unmapped_type[txn.source_type] = (
                result.transactions_unmapped_type.get(txn.source_type, 0) + 1
            )
    return result


def apply_import(
    db: Session,
    read: WorkbookReadResult,
    options: ImportOptions,
    *,
    commit: bool = True,
) -> ImportResult:
    """Land a read portfolio in PaySpyre and reconcile it.

    The caller owns the transaction when ``commit=False`` (that is how the dry
    run works: apply everything, reconcile the REAL result, then roll back).
    """
    profile = get_profile(options.profile_name)
    problems = options.placeholders.validate()
    if problems:
        raise ValueError("; ".join(problems))

    result = ImportResult(dry_run=not commit, options=options.describe())
    result.read_report = read.as_report()
    result.source_reconciliation = reconcile_source(
        read.accounts, read.transactions, tolerance_cents=options.tolerance_cents
    ).as_dict()

    vendors = _resolve_vendors(db, read, options, result)
    provider_rows = _seed_providers(db, read, vendors, result)
    borrowers = _import_borrowers(db, read, profile, options, result)
    loans = _import_loans(
        db, read, profile, options, vendors, provider_rows, borrowers, result
    )
    _import_transactions(db, read, profile, loans, result)
    db.flush()

    persisted: ReconciliationReport = reconcile_persisted(
        read.accounts,
        _persisted_totals(db, loans),
        tolerance_cents=options.tolerance_cents,
    )
    result.persisted_reconciliation = persisted.as_dict()

    if commit:
        db.commit()
    return result


def reconcile_existing(
    db: Session,
    read: WorkbookReadResult,
    *,
    tolerance_cents: int = DEFAULT_TOLERANCE_CENTS,
) -> ReconciliationReport:
    """Tie an ALREADY-IMPORTED book back to the source workbook. Read-only.

    Loans are matched on ``legacy_account_number``; an account with no loan in
    PaySpyre is listed rather than counted as reconciled.
    """
    accounts = {a.account_number for a in read.accounts}
    loans = {
        loan.legacy_account_number: loan
        for loan in db.query(PlatformLoan)
        .filter(PlatformLoan.legacy_account_number.in_(accounts))
        .all()
        if loan.legacy_account_number
    }
    return reconcile_persisted(
        read.accounts, _persisted_totals(db, loans), tolerance_cents=tolerance_cents
    )


def import_workbook(
    db: Session,
    path_or_bytes: Any,
    options: Optional[ImportOptions] = None,
    *,
    dry_run: bool = True,
) -> ImportResult:
    """Read a workbook and (unless ``dry_run``) land it.

    A DRY RUN still performs every write inside the transaction and then ROLLS
    BACK, so the reconciliation it reports is the reconciliation the real run
    would produce — not an estimate of it.
    """
    from app.services.migration.portfolio_workbook import read_file

    options = options or ImportOptions()
    profile = get_profile(options.profile_name)
    read = read_file(path_or_bytes, profile)
    if dry_run:
        try:
            return apply_import(db, read, options, commit=False)
        finally:
            db.rollback()
    return apply_import(db, read, options, commit=True)
