"""Export a labeled feature/outcome dataset for risk-model training (AI/ML Phase 0).

Run offline (NOT in the request path):

    python -m scripts.ml.export_training_data --out training_data.csv
    python -m scripts.ml.export_training_data --out td.csv --as-of 2026-06-01
    python -m scripts.ml.export_training_data --out td.csv \
        --database-url postgresql+psycopg2://user:pw@host:5432/db

One CSV row per ``PlatformLoan``. Each row carries three blocks of columns:

* **identifiers** — opaque, salted-hash surrogates of the loan/application UUIDs.
  No PII whatsoever (no name, email, phone, SIN, DOB, address). The hash lets
  you join back to the source row only if you hold the same salt; without it the
  ids are non-reversible.
* **origination features** — everything known when the loan was booked: amount,
  rate, term, requested amount, the credit decision + its stable reason codes,
  self-reported income/liabilities, KYC verification depth, applicant role and
  loan source.
* **performance / outcome features (as-of the given date)** — current status,
  outstanding principal, installment status counts, max days-past-due, on-time
  payment ratio, NSF/late counts and total paid.
* **labels** — ``outcome_default``, ``outcome_delinquent``, ``outcome_paid_off``
  and a ``days_past_due_bucket``, derived from the same as-of snapshot.

This single dataset serves all four planned model targets: early-warning,
collections prioritization, underwriting and pricing. The pure derivation
helpers (``*_features`` / ``derive_labels`` / ``build_row``) take plain Python
objects, so they are unit-testable without a database.

----------------------------------------------------------------------------
DATA GAPS — read before training a model on this output
----------------------------------------------------------------------------
1. **Raw bureau attributes are NOT stored.** Per the platform audit, the credit
   bureau pull is consumed at decision time and discarded; only the *decision*
   ("approved"/"declined"/"manual_review") and the stable ``decision_reasons``
   codes survive in ``platform_credit_applications.decision`` (JSONB). The raw
   FICO/ERS score, tradelines, utilization, inquiries, bankruptcies, etc. are
   unavailable here. The bureau-oriented columns
   (``decision_score``, ``decision_score_band_*``) are emitted as **empty/null**
   whenever the underlying value is absent, and will be null for essentially all
   current rows. Closing this gap (persisting a privacy-safe bureau-attribute
   snapshot at decision time) is a prerequisite for a real underwriting model
   and is owned outside this script.

2. **Low volume until the legacy book is imported.** Natively-originated loans
   only began at the July beta. Historical performance lives in the legacy
   Turnkey book and must be imported via ``scripts/migration/turnkey_import.py``
   (loans land with ``source='turnkey_migration'`` and ``application_id IS
   NULL``) BEFORE this export has enough labeled, matured loans to train on.
   Migrated loans have no PaySpyre application, so their origination-feature
   columns (requested amount, decision, self-reported, verification depth,
   applicant role) are null — expected, documented, and handled gracefully.

3. **Label maturity / survivorship.** A young active loan has not had time to
   default; treat ``outcome_*`` as point-in-time as of ``--as-of`` and filter on
   loan age / observation window in the training workspace. This script does not
   apply a maturity cutoff — it exports every loan and records the as-of date.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Column contract
# ---------------------------------------------------------------------------
# The exact, ordered set of columns written to the CSV. Kept as a module-level
# constant so tests can assert the schema and so the no-PII guarantee can be
# checked programmatically (see ``_PII_TOKENS`` / ``assert_no_pii_columns``).
COLUMNS: list[str] = [
    # --- identifiers (opaque, non-PII) ---
    "loan_id_hash",
    "application_id_hash",
    "as_of_date",
    # --- origination features ---
    "principal_cents",
    "annual_rate_bps",
    "term_months",
    "requested_amount_cents",
    "loan_source",
    "is_migrated",
    "decision",
    "decision_reason_codes",  # pipe-joined stable codes
    "decision_score",  # null today — see DATA GAPS #1
    "decision_score_band_min",  # null today — see DATA GAPS #1
    "decision_score_band_max",  # null today — see DATA GAPS #1
    "self_reported_income_cents",
    "self_reported_liabilities_cents",
    "verification_depth",
    "applicant_role",
    "is_co_applicant",
    # --- performance / outcome features (as-of) ---
    "status",
    "principal_balance_cents",
    "installments_total",
    "installments_paid",
    "installments_partial",
    "installments_late",
    "installments_waived",
    "installments_due_count",  # due on or before as_of
    "on_time_payment_ratio",
    "nsf_count",
    "late_payment_count",
    "payments_count",
    "total_paid_cents",
    "max_days_past_due",
    # --- labels ---
    "outcome_default",
    "outcome_delinquent",
    "outcome_paid_off",
    "days_past_due_bucket",
]

# Substrings that must NEVER appear in a column name. Defence-in-depth against a
# future edit accidentally leaking PII into the export.
_PII_TOKENS = (
    "name",
    "email",
    "phone",
    "sin",
    "dob",
    "birth",
    "address",
    "postal",
    "first",
    "last",
)


def assert_no_pii_columns(columns: Iterable[str] = COLUMNS) -> None:
    """Raise if any column name looks like PII. Cheap, runs at startup + in tests."""
    for col in columns:
        low = col.lower()
        for tok in _PII_TOKENS:
            if tok in low:
                raise AssertionError(
                    f"PII-looking column {col!r} (matched {tok!r}) is forbidden in the "
                    "ML training export"
                )


# ---------------------------------------------------------------------------
# Small, dependency-free record shapes used by the pure helpers.
# Tests construct these directly; the DB path adapts ORM rows into them.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScheduleItemView:
    installment_number: int
    due_date: date
    total_cents: int
    paid_cents: int
    status: str  # scheduled | paid | partial | late | waived


@dataclass(frozen=True)
class PaymentView:
    amount_cents: int
    received_at: Optional[datetime]
    method: str  # e.g. "zumrails", "nsf_reversal", "manual"


# ---------------------------------------------------------------------------
# Hashing (opaque identifiers)
# ---------------------------------------------------------------------------
def hash_id(value: Any, salt: str) -> str:
    """Return a stable, salted, non-reversible surrogate for an id.

    ``None`` maps to the empty string (e.g. a migrated loan with no application).
    """
    if value is None:
        return ""
    return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Decision-JSONB extraction
# ---------------------------------------------------------------------------
def origination_decision_features(decision: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Pull model features out of ``platform_credit_applications.decision`` JSONB.

    Robust to the real shape ``{decision, decision_reasons, ...}`` and to the
    audit finding that no raw bureau score is stored: ``decision_score`` and the
    band columns come out ``None`` unless a (future) snapshot includes them.
    """
    decision = decision or {}
    reasons = decision.get("decision_reasons") or decision.get("reasons") or []
    if not isinstance(reasons, list):
        reasons = []

    # Score / band are not persisted today; look in a few plausible shapes so the
    # script keeps working if a privacy-safe snapshot is added later.
    score = decision.get("score")
    if score is None:
        score = decision.get("effective_score")
    band = decision.get("band") if isinstance(decision.get("band"), dict) else {}

    return {
        "decision": decision.get("decision"),
        "decision_reason_codes": "|".join(str(r) for r in reasons),
        "decision_score": score,
        "decision_score_band_min": band.get("min"),
        "decision_score_band_max": band.get("max"),
    }


def _coerce_cents(value: Any) -> Optional[int]:
    """Best-effort cents from self-reported JSONB (int already, or dollars float)."""
    if value is None:
        return None
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    if isinstance(value, int):
        return value
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def self_reported_features(self_reported: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Extract income / liabilities from the ``self_reported`` JSONB if present.

    Accepts ``*_cents`` keys (used as-is) or bare ``income`` / ``liabilities``
    (coerced). Missing → ``None``."""
    sr = self_reported or {}
    income = sr.get("income_cents")
    if income is None:
        income = sr.get("annual_income_cents")
    if income is None and "income" in sr:
        income = _coerce_cents(sr.get("income"))
    else:
        income = _coerce_cents(income)

    liabilities = sr.get("liabilities_cents")
    if liabilities is None and "liabilities" in sr:
        liabilities = _coerce_cents(sr.get("liabilities"))
    else:
        liabilities = _coerce_cents(liabilities)

    return {
        "self_reported_income_cents": income,
        "self_reported_liabilities_cents": liabilities,
    }


# ---------------------------------------------------------------------------
# Performance / outcome features (as-of)
# ---------------------------------------------------------------------------
# Heuristic methods that indicate a failed/returned collection. Robust to the
# method string varying — we substring-match.
_NSF_METHOD_TOKENS = ("nsf", "returned", "reversal", "failed", "chargeback")


def max_days_past_due(
    schedule: Iterable[ScheduleItemView], as_of: date
) -> int:
    """Days between the EARLIEST unpaid/underpaid past-due installment and ``as_of``.

    An installment counts as past-due if its ``due_date <= as_of`` and it is not
    fully paid (``paid_cents < total_cents``) and not waived. The earliest such
    item drives the worst (largest) DPD. Returns 0 when nothing is past due.
    """
    worst = 0
    for item in schedule:
        if item.status == "waived":
            continue
        if item.due_date > as_of:
            continue
        if item.paid_cents >= item.total_cents:
            continue
        dpd = (as_of - item.due_date).days
        if dpd > worst:
            worst = dpd
    return worst


def performance_features(
    *,
    status: str,
    principal_balance_cents: int,
    schedule: Iterable[ScheduleItemView],
    payments: Iterable[PaymentView],
    as_of: date,
) -> dict[str, Any]:
    """Aggregate schedule + payment history into as-of performance features."""
    schedule = list(schedule)
    payments = list(payments)

    counts = {"paid": 0, "partial": 0, "late": 0, "waived": 0, "scheduled": 0}
    due_count = 0
    on_time_eligible = 0
    on_time_paid = 0
    for item in schedule:
        counts[item.status] = counts.get(item.status, 0) + 1
        if item.due_date <= as_of:
            due_count += 1
            if item.status != "waived":
                on_time_eligible += 1
                # "On time" = fully paid and not flagged late.
                if item.status == "paid" and item.paid_cents >= item.total_cents:
                    on_time_paid += 1

    on_time_ratio = (
        round(on_time_paid / on_time_eligible, 4) if on_time_eligible else None
    )

    nsf_count = 0
    total_paid = 0
    for pmt in payments:
        method = (pmt.method or "").lower()
        if any(tok in method for tok in _NSF_METHOD_TOKENS):
            nsf_count += 1
        else:
            # NSF/reversal entries are not "money received"; exclude from total.
            total_paid += pmt.amount_cents

    return {
        "status": status,
        "principal_balance_cents": principal_balance_cents,
        "installments_total": len(schedule),
        "installments_paid": counts["paid"],
        "installments_partial": counts["partial"],
        "installments_late": counts["late"],
        "installments_waived": counts["waived"],
        "installments_due_count": due_count,
        "on_time_payment_ratio": on_time_ratio,
        "nsf_count": nsf_count,
        "late_payment_count": counts["late"],
        "payments_count": len(payments),
        "total_paid_cents": total_paid,
        "max_days_past_due": max_days_past_due(schedule, as_of),
    }


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
def days_past_due_bucket(max_dpd: int) -> str:
    """Coarse DPD bucket used as a categorical label / stratification key."""
    if max_dpd <= 0:
        return "current"
    if max_dpd < 30:
        return "1-29"
    if max_dpd < 60:
        return "30-59"
    if max_dpd < 90:
        return "60-89"
    return "90+"


def derive_labels(*, status: str, max_dpd: int) -> dict[str, Any]:
    """Compute the supervised labels from terminal status + worst DPD.

    * ``outcome_default``    — charged off OR ever 90+ DPD.
    * ``outcome_delinquent`` — currently delinquent OR ever 30+ DPD.
    * ``outcome_paid_off``   — loan reached paid_off.
    """
    return {
        "outcome_default": int(status == "charged_off" or max_dpd >= 90),
        "outcome_delinquent": int(status == "delinquent" or max_dpd >= 30),
        "outcome_paid_off": int(status == "paid_off"),
        "days_past_due_bucket": days_past_due_bucket(max_dpd),
    }


# ---------------------------------------------------------------------------
# Row builder (pure)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LoanRecord:
    """Everything the row builder needs, decoupled from the ORM for testability."""

    loan_id: Any
    application_id: Any
    source: str
    principal_cents: int
    annual_rate_bps: int
    term_months: int
    principal_balance_cents: int
    status: str
    # origination context (None for migrated loans with no application)
    requested_amount_cents: Optional[int]
    decision: Optional[dict[str, Any]]
    self_reported: Optional[dict[str, Any]]
    applicant_role: Optional[str]
    co_applicant_of_application_id: Any
    verification_depth: Optional[str]
    schedule: list[ScheduleItemView]
    payments: list[PaymentView]


def build_row(record: LoanRecord, *, as_of: date, salt: str) -> dict[str, Any]:
    """Assemble one fully-derived CSV row dict (keys == ``COLUMNS``)."""
    is_migrated = record.source == "turnkey_migration"

    row: dict[str, Any] = {
        "loan_id_hash": hash_id(record.loan_id, salt),
        "application_id_hash": hash_id(record.application_id, salt),
        "as_of_date": as_of.isoformat(),
        "principal_cents": record.principal_cents,
        "annual_rate_bps": record.annual_rate_bps,
        "term_months": record.term_months,
        "requested_amount_cents": record.requested_amount_cents,
        "loan_source": record.source,
        "is_migrated": int(is_migrated),
        "self_reported_income_cents": None,
        "self_reported_liabilities_cents": None,
        "verification_depth": record.verification_depth,
        "applicant_role": record.applicant_role,
        "is_co_applicant": int(record.co_applicant_of_application_id is not None),
    }
    row.update(origination_decision_features(record.decision))
    row.update(self_reported_features(record.self_reported))
    row.update(
        performance_features(
            status=record.status,
            principal_balance_cents=record.principal_balance_cents,
            schedule=record.schedule,
            payments=record.payments,
            as_of=as_of,
        )
    )
    row.update(
        derive_labels(status=record.status, max_dpd=row["max_days_past_due"])
    )

    # Final safety net: the assembled row must carry exactly the declared columns
    # and no PII keys.
    assert set(row.keys()) == set(COLUMNS), (
        f"row keys {sorted(set(row) ^ set(COLUMNS))} diverge from COLUMNS"
    )
    return row


def serialize_cell(value: Any) -> str:
    """Stable CSV serialization: ``None`` -> ''; floats rounded; bools as 0/1."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return repr(round(value, 6))
    return str(value)


# ---------------------------------------------------------------------------
# DB adaptation (only used by the CLI; the helpers above need no DB)
# ---------------------------------------------------------------------------
def _record_from_loan(loan: Any, application: Any) -> LoanRecord:
    """Adapt an ORM ``PlatformLoan`` (+ optional application) into a LoanRecord."""
    schedule = [
        ScheduleItemView(
            installment_number=it.installment_number,
            due_date=it.due_date,
            total_cents=it.total_cents,
            paid_cents=it.paid_cents,
            status=it.status,
        )
        for it in (loan.schedule or [])
    ]
    payments = [
        PaymentView(
            amount_cents=p.amount_cents,
            received_at=p.received_at,
            method=p.method,
        )
        for p in (loan.payments or [])
    ]
    patient = getattr(application, "patient", None) if application is not None else None
    return LoanRecord(
        loan_id=loan.id,
        application_id=loan.application_id,
        source=loan.source,
        principal_cents=loan.principal_cents,
        annual_rate_bps=loan.annual_rate_bps,
        term_months=loan.term_months,
        principal_balance_cents=loan.principal_balance_cents,
        status=loan.status,
        requested_amount_cents=(
            application.requested_amount_cents if application is not None else None
        ),
        decision=(application.decision if application is not None else None),
        self_reported=(application.self_reported if application is not None else None),
        applicant_role=(application.applicant_role if application is not None else None),
        co_applicant_of_application_id=(
            application.co_applicant_of_application_id if application is not None else None
        ),
        verification_depth=(
            patient.verification_depth if patient is not None else None
        ),
        schedule=schedule,
        payments=payments,
    )


def export(db: Any, *, out_path: str, as_of: date, salt: str) -> int:
    """Stream every loan to ``out_path`` as a CSV. Returns the row count."""
    # Imported lazily so the pure helpers + tests don't require the app models.
    from app.models.platform.credit_application import PlatformCreditApplication
    from app.models.platform.loan import PlatformLoan

    assert_no_pii_columns()

    written = 0
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="raise")
        writer.writeheader()
        for loan in db.query(PlatformLoan).yield_per(500):
            application = None
            if loan.application_id is not None:
                application = (
                    db.query(PlatformCreditApplication)
                    .filter(PlatformCreditApplication.id == loan.application_id)
                    .first()
                )
            record = _record_from_loan(loan, application)
            row = build_row(record, as_of=as_of, salt=salt)
            writer.writerow({k: serialize_cell(v) for k, v in row.items()})
            written += 1
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_as_of(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export a labeled loan feature/outcome dataset for risk modelling."
    )
    parser.add_argument("--out", required=True, help="Output CSV path.")
    parser.add_argument(
        "--as-of",
        type=_parse_as_of,
        default=date.today(),
        help="As-of date (YYYY-MM-DD) for performance/outcome features. Default: today.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override DATABASE_URL (else uses the app's SessionLocal config).",
    )
    parser.add_argument(
        "--salt",
        default=os.environ.get("ML_EXPORT_SALT", "payspyre-ml"),
        help="Salt for opaque id hashing (or set ML_EXPORT_SALT).",
    )
    args = parser.parse_args(argv)

    assert_no_pii_columns()

    if args.database_url:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(args.database_url)
        session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        db = session_factory()
    else:
        from app.db.base import SessionLocal

        db = SessionLocal()

    try:
        count = export(db, out_path=args.out, as_of=args.as_of, salt=args.salt)
        print(
            f"Exported {count} loan rows to {args.out} (as-of {args.as_of.isoformat()})."
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
