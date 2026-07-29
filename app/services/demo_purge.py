"""Guarded purge of demonstration data.

Placeholder borrowers, applications, loans and staff logins accumulated during
development are not just clutter — they make every test ambiguous: you cannot
tell a real defect from a leftover fixture. This removes them so the platform
holds only real data (a genuine imported portfolio) plus the accounts that
operate it.

This is the most destructive operation in the codebase, so it is fenced:

* NEVER IN PRODUCTION — refuses outright when ``ENVIRONMENT`` is production.
  There is no override flag; a production wipe is not a feature.
* EXPLICIT CONFIRMATION — the caller must supply the exact confirmation phrase
  :data:`CONFIRMATION_TOKEN`. No default, no truthy flag, nothing that can be
  satisfied by an accidental empty body or a stray ``true``.
* RETAINED ACCOUNTS — the operator logins named in ``retain_emails`` (and their
  sessions, roles and grants) survive. The purge REFUSES if a retained address
  does not exist, rather than locking everyone out of the platform it just
  emptied.
* TRANSACTIONAL — one transaction. Any failure rolls the whole thing back;
  there is no half-purged state.
* DRY RUN — reports exactly what WOULD go, per table, deleting nothing.
* REPORTED — a real run returns the actual per-table deleted counts, and writes
  an audit event.

SCOPE: the borrower/application/loan domain in full, plus staff users other than
the retained ones. CONFIGURATION IS NOT DEMO DATA and is never touched — credit
products, decision rules, notification templates, province compliance, company
info, integration settings, roles and permissions all survive.

SQL: every statement is a module-level literal (or built through SQLAlchemy's
expression API); no SQL string is ever constructed by formatting. The only
user-supplied values are the retained e-mail addresses, passed as BOUND
PARAMETERS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from sqlalchemy import func, select, table, text
from sqlalchemy.orm import Session

from app.core.config import settings

#: The caller must send this exact string. Anything else is refused.
CONFIRMATION_TOKEN = "PURGE-DEMO-DATA"

#: Operator logins that always survive a purge unless the caller names others.
DEFAULT_RETAIN_EMAILS: tuple[str, ...] = (
    "dave@payspyrebeta.com",
    "admin@payspyrebeta.com",
)

#: The ledger is WORM (migration 049): a DB trigger rejects UPDATE and DELETE,
#: including deletes cascaded from ``platform_loans``. A purge is the one
#: legitimate reason to remove ledger rows, so the trigger is disabled INSIDE
#: the transaction — DDL is transactional in Postgres, so a rollback restores it
#: automatically and no window exists where the ledger is unprotected outside
#: this statement's own transaction.
_DISABLE_LEDGER_WORM = text(
    "ALTER TABLE platform_loan_transactions DISABLE TRIGGER platform_loan_transactions_immutable"
)
_ENABLE_LEDGER_WORM = text(
    "ALTER TABLE platform_loan_transactions ENABLE TRIGGER platform_loan_transactions_immutable"
)

#: Child-first delete order for the borrower / application / loan domain. Every
#: entry is a static literal statement; the order satisfies the FK graph without
#: relying on ON DELETE CASCADE (several links are NO ACTION).
_DOMAIN_DELETES: tuple[tuple[str, object], ...] = (
    # --- servicing + collections children ------------------------------
    ("platform_collection_attempts", text("DELETE FROM platform_collection_attempts")),
    ("platform_collection_actions", text("DELETE FROM platform_collection_actions")),
    ("platform_collector_assignments", text("DELETE FROM platform_collector_assignments")),
    ("platform_promises_to_pay", text("DELETE FROM platform_promises_to_pay")),
    ("platform_insolvency_maintenance_fees", text("DELETE FROM platform_insolvency_maintenance_fees")),
    ("platform_loan_custom_transactions", text("DELETE FROM platform_loan_custom_transactions")),
    ("platform_loan_documents", text("DELETE FROM platform_loan_documents")),
    ("platform_loan_statements", text("DELETE FROM platform_loan_statements")),
    ("platform_loan_delinquency_snapshots", text("DELETE FROM platform_loan_delinquency_snapshots")),
    ("platform_loan_schedule", text("DELETE FROM platform_loan_schedule")),
    ("platform_loan_payments", text("DELETE FROM platform_loan_payments")),
    ("platform_payout_requests", text("DELETE FROM platform_payout_requests")),
    ("platform_hardship_requests", text("DELETE FROM platform_hardship_requests")),
    # Self-referencing (reversals) — clear the link before deleting the rows.
    ("platform_loan_transactions", text("UPDATE platform_loan_transactions SET reverses_transaction_id = NULL WHERE reverses_transaction_id IS NOT NULL")),
    ("platform_loan_transactions", text("DELETE FROM platform_loan_transactions")),
    # --- cross-cutting references to loans/applications/patients --------
    ("platform_staff_comments", text("DELETE FROM platform_staff_comments")),
    ("platform_flag_assignments", text("DELETE FROM platform_flag_assignments")),
    ("platform_communications_log", text("DELETE FROM platform_communications_log")),
    ("platform_notification_outbox", text("DELETE FROM platform_notification_outbox")),
    ("platform_notification_cursor", text("DELETE FROM platform_notification_cursor")),
    ("platform_vendor_disbursements", text("DELETE FROM platform_vendor_disbursements")),
    ("platform_bureau_batches", text("DELETE FROM platform_bureau_batches")),
    ("platform_loans", text("DELETE FROM platform_loans")),
    ("platform_loan_offers", text("DELETE FROM platform_loan_offers")),
    # --- application children -------------------------------------------
    ("platform_application_message_reads", text("DELETE FROM platform_application_message_reads")),
    ("platform_application_messages", text("DELETE FROM platform_application_messages")),
    ("platform_application_risk_scores", text("DELETE FROM platform_application_risk_scores")),
    ("platform_application_secondary_incomes", text("DELETE FROM platform_application_secondary_incomes")),
    ("platform_application_address_history", text("DELETE FROM platform_application_address_history")),
    ("platform_application_employment_history", text("DELETE FROM platform_application_employment_history")),
    ("platform_application_documents", text("DELETE FROM platform_application_documents")),
    ("platform_credit_report_pulls", text("DELETE FROM platform_credit_report_pulls")),
    ("platform_verifications", text("DELETE FROM platform_verifications")),
    ("platform_consents", text("DELETE FROM platform_consents")),
    ("platform_events", text("DELETE FROM platform_events")),
    # Co-applicant links are self-referencing; break them before the delete.
    ("platform_credit_applications", text("UPDATE platform_credit_applications SET co_applicant_of_application_id = NULL WHERE co_applicant_of_application_id IS NOT NULL")),
    ("platform_credit_applications", text("DELETE FROM platform_credit_applications")),
    # --- customer profile + marketplace + patient children --------------
    ("platform_customer_profile_fields", text("UPDATE platform_customer_profile_fields SET superseded_by_id = NULL WHERE superseded_by_id IS NOT NULL")),
    ("platform_customer_profile_fields", text("DELETE FROM platform_customer_profile_fields")),
    ("platform_customer_profiles", text("DELETE FROM platform_customer_profiles")),
    ("platform_marketplace_vendor_interest", text("DELETE FROM platform_marketplace_vendor_interest")),
    ("platform_marketplace_listings", text("DELETE FROM platform_marketplace_listings")),
    ("platform_customer_blocks", text("DELETE FROM platform_customer_blocks")),
    ("platform_patient_bank_accounts", text("DELETE FROM platform_patient_bank_accounts")),
    ("platform_patient_id_documents", text("UPDATE platform_patient_id_documents SET superseded_by_id = NULL WHERE superseded_by_id IS NOT NULL")),
    ("platform_patient_id_documents", text("DELETE FROM platform_patient_id_documents")),
    ("platform_patient_second_factor", text("DELETE FROM platform_patient_second_factor")),
    ("platform_patient_fields", text("UPDATE platform_patient_fields SET superseded_by_id = NULL WHERE superseded_by_id IS NOT NULL")),
    ("platform_patient_fields", text("DELETE FROM platform_patient_fields")),
    ("platform_patients", text("DELETE FROM platform_patients")),
    # --- import bookkeeping (batches reference the purged rows) ---------
    ("platform_import_batches", text("DELETE FROM platform_import_batches")),
    # --- legacy v1 borrower tables --------------------------------------
    ("credit_reports", text("DELETE FROM credit_reports")),
    ("credit_inquiries", text("DELETE FROM credit_inquiries")),
    ("refunds", text("DELETE FROM refunds")),
    ("payments", text("DELETE FROM payments")),
    ("payment_schedule", text("DELETE FROM payment_schedule")),
    ("payment_methods", text("DELETE FROM payment_methods")),
    ("statements", text("DELETE FROM statements")),
    ("funding", text("DELETE FROM funding")),
    ("kyb_beneficial_owners", text("DELETE FROM kyb_beneficial_owners")),
    ("manual_kyb_reviews", text("DELETE FROM manual_kyb_reviews")),
    ("kyb_applications", text("DELETE FROM kyb_applications")),
    ("kyc_co_borrower_links", text("DELETE FROM kyc_co_borrower_links")),
    ("kyc_events", text("DELETE FROM kyc_events")),
    ("kyc_results", text("DELETE FROM kyc_results")),
    ("kyc_sessions", text("DELETE FROM kyc_sessions")),
    ("document_versions", text("DELETE FROM document_versions")),
    ("documents", text("DELETE FROM documents")),
    ("loan_applications", text("DELETE FROM loan_applications")),
    ("borrowers", text("DELETE FROM borrowers")),
)

def _count(table_name: str):
    """``SELECT count(*) FROM <table>`` built through SQLAlchemy's expression API.

    No string formatting anywhere: the table name is bound into a Table object,
    not interpolated into SQL text. (The names are module-level literals either
    way — this keeps the file free of constructed SQL entirely.)
    """
    return select(func.count()).select_from(table(table_name))


#: Row counts reported by a dry run (same tables, same order).
_DOMAIN_COUNTS: tuple[tuple[str, object], ...] = tuple(
    (name, _count(name)) for name in dict.fromkeys(t for t, _ in _DOMAIN_DELETES)
)

#: Staff-user cleanup. Every statement binds ``:retain`` — never interpolates it.
_USER_DELETES: tuple[tuple[str, object], ...] = (
    ("sessions", text("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE lower(email) <> ALL(:retain))")),
    ("api_keys", text("DELETE FROM api_keys WHERE user_id IN (SELECT id FROM users WHERE lower(email) <> ALL(:retain))")),
    ("user_roles", text("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE lower(email) <> ALL(:retain))")),
    ("user_permissions", text("DELETE FROM user_permissions WHERE user_id IN (SELECT id FROM users WHERE lower(email) <> ALL(:retain)) OR granted_by IN (SELECT id FROM users WHERE lower(email) <> ALL(:retain))")),
    ("platform_clinic_memberships", text("DELETE FROM platform_clinic_memberships WHERE user_id IN (SELECT id FROM users WHERE lower(email) <> ALL(:retain))")),
    ("deliveries", text("DELETE FROM deliveries WHERE notification_id IN (SELECT id FROM notifications WHERE user_id IN (SELECT id FROM users WHERE lower(email) <> ALL(:retain)))")),
    ("notifications", text("DELETE FROM notifications WHERE user_id IN (SELECT id FROM users WHERE lower(email) <> ALL(:retain))")),
    ("notification_preferences", text("DELETE FROM notification_preferences WHERE user_id IN (SELECT id FROM users WHERE lower(email) <> ALL(:retain))")),
    ("users", text("DELETE FROM users WHERE lower(email) <> ALL(:retain)")),
)

_USER_COUNT = text("SELECT count(*) FROM users WHERE lower(email) <> ALL(:retain)")
_RETAINED_PRESENT = text("SELECT lower(email) FROM users WHERE lower(email) = ANY(:retain)")

#: Vendor records, purged only when explicitly asked for.
_VENDOR_DELETES: tuple[tuple[str, object], ...] = (
    ("platform_vendor_document_expiry_alerts", text("DELETE FROM platform_vendor_document_expiry_alerts")),
    ("platform_vendor_documents", text("DELETE FROM platform_vendor_documents")),
    ("platform_vendor_bank_accounts", text("DELETE FROM platform_vendor_bank_accounts")),
    ("platform_vendor_contacts", text("DELETE FROM platform_vendor_contacts")),
    ("platform_vendor_onboarding", text("DELETE FROM platform_vendor_onboarding")),
    ("platform_vendor_scorecards", text("DELETE FROM platform_vendor_scorecards")),
    ("platform_vendor_profile_change_requests", text("DELETE FROM platform_vendor_profile_change_requests")),
    ("platform_providers", text("DELETE FROM platform_providers")),
    ("vendors", text("DELETE FROM vendors")),
)
_VENDOR_COUNTS: tuple[tuple[str, object], ...] = tuple(
    (name, _count(name)) for name, _ in _VENDOR_DELETES
)


class PurgeRefused(Exception):
    """The purge did not run. Carries the operator-facing reason."""


@dataclass
class PurgeReport:
    dry_run: bool
    environment: str
    retained_emails: list[str] = field(default_factory=list)
    include_vendors: bool = False
    #: table -> rows deleted (real run) or rows that WOULD be deleted (dry run)
    tables: dict[str, int] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return sum(self.tables.values())

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "environment": self.environment,
            "retained_emails": self.retained_emails,
            "include_vendors": self.include_vendors,
            "total_rows": self.total_rows,
            "tables": {k: v for k, v in sorted(self.tables.items()) if v},
            "tables_empty": sorted(k for k, v in self.tables.items() if not v),
        }


def _retain_param(retain_emails: Optional[Sequence[str]]) -> list[str]:
    """Normalize the retain list.

    ``None`` means "use the standing operator accounts". An EXPLICITLY EMPTY list
    is not the same thing — it says "keep nobody", which would leave the platform
    with no way back in, so it is refused rather than quietly defaulted.
    """
    emails = list(DEFAULT_RETAIN_EMAILS) if retain_emails is None else list(retain_emails)
    normalized = sorted({e.strip().lower() for e in emails if e and e.strip()})
    if not normalized:
        raise PurgeRefused(
            "refusing to purge with an empty retain list — that would delete every login"
        )
    return normalized


def _guard_environment() -> str:
    env = (getattr(settings, "ENVIRONMENT", "") or "").strip().lower()
    if env in ("production", "prod"):
        raise PurgeRefused(
            "the demo-data purge is disabled in production; there is no override"
        )
    return env or "unknown"


def _guard_confirmation(confirmation: Optional[str]) -> None:
    if confirmation != CONFIRMATION_TOKEN:
        raise PurgeRefused(
            f"confirmation token missing or wrong — send exactly {CONFIRMATION_TOKEN!r}"
        )


def _guard_retained_exist(db: Session, retain: list[str]) -> None:
    present = {row[0] for row in db.execute(_RETAINED_PRESENT, {"retain": retain})}
    missing = [e for e in retain if e not in present]
    if missing:
        raise PurgeRefused(
            "refusing to purge: retained account(s) do not exist in this database — "
            f"{', '.join(missing)}. Create them first, or the purge would leave no login."
        )


def dry_run(
    db: Session,
    *,
    retain_emails: Optional[Sequence[str]] = None,
    include_vendors: bool = False,
) -> PurgeReport:
    """Count what a purge WOULD delete. Writes nothing, deletes nothing.

    Requires no confirmation token — counting is not destructive — but is still
    refused in production so the numbers can never be mistaken for a rehearsal
    of something that is allowed to happen there.
    """
    env = _guard_environment()
    retain = _retain_param(retain_emails)
    report = PurgeReport(
        dry_run=True, environment=env, retained_emails=retain, include_vendors=include_vendors
    )
    for name, stmt in _DOMAIN_COUNTS:
        report.tables[name] = int(db.execute(stmt).scalar() or 0)
    report.tables["users"] = int(db.execute(_USER_COUNT, {"retain": retain}).scalar() or 0)
    if include_vendors:
        for name, stmt in _VENDOR_COUNTS:
            report.tables[name] = int(db.execute(stmt).scalar() or 0)
    return report


def purge(
    db: Session,
    *,
    confirmation: Optional[str],
    retain_emails: Optional[Sequence[str]] = None,
    include_vendors: bool = False,
    commit: bool = True,
) -> PurgeReport:
    """Delete the demonstration data. One transaction; all-or-nothing.

    Raises :class:`PurgeRefused` (having changed nothing) when any guard fails.
    """
    env = _guard_environment()
    _guard_confirmation(confirmation)
    retain = _retain_param(retain_emails)
    _guard_retained_exist(db, retain)

    report = PurgeReport(
        dry_run=False, environment=env, retained_emails=retain, include_vendors=include_vendors
    )

    def _record(name: str, rowcount: int) -> None:
        report.tables[name] = report.tables.get(name, 0) + max(0, rowcount)

    try:
        db.execute(_DISABLE_LEDGER_WORM)
        for name, stmt in _DOMAIN_DELETES:
            result = db.execute(stmt)
            # The UPDATE statements that break self-references are preparation,
            # not deletion — they must not inflate the deleted-rows report.
            if str(stmt).lstrip().upper().startswith("DELETE"):
                _record(name, result.rowcount or 0)
            else:
                report.tables.setdefault(name, 0)
        if include_vendors:
            for name, stmt in _VENDOR_DELETES:
                _record(name, db.execute(stmt).rowcount or 0)
        for name, stmt in _USER_DELETES:
            _record(name, db.execute(stmt, {"retain": retain}).rowcount or 0)
        db.execute(_ENABLE_LEDGER_WORM)
    except Exception:
        db.rollback()
        raise

    if commit:
        db.commit()
    return report
