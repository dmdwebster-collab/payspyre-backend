"""Shared identifiers for the portfolio import.

PaySpyre imports loan portfolios REGARDLESS OF SOURCE SYSTEM. The importer is
source-neutral; only these persisted identifiers still carry the name of the
first legacy system the platform migrated off, because they exist in live rows
and rewriting live data is not a rename — it is a data migration with no upside.

The rule is therefore:

  * WRITE the neutral value (``portfolio_import``, ``portfolio:``, ``portfolio_*``
    field keys) for everything created from now on;
  * READ both the neutral value and its legacy alias, so rows written before the
    rename keep resolving, dedupe stays idempotent across the boundary, and a
    re-import of an already-migrated book does not duplicate anything.

Nothing outside this module should hardcode either spelling.
"""
from __future__ import annotations

# --- platform_loans.source -------------------------------------------------
# Loans that came from a legacy servicing system rather than a PaySpyre
# application. ``application_id`` is NULL for these (migration 035's CHECK
# constraint, widened by migration 083 to accept the neutral value too).
PORTFOLIO_SOURCE = "portfolio_import"
LEGACY_PORTFOLIO_SOURCE = "turnkey_migration"  # retained: exists in live rows
#: Every value that means "this loan was imported, not originated here".
IMPORTED_LOAN_SOURCES: tuple[str, ...] = (PORTFOLIO_SOURCE, LEGACY_PORTFOLIO_SOURCE)

# --- platform_loan_payments.external_ref namespaces ------------------------
# A historical reference is namespaced so a legacy transaction id can never
# collide with a live rail reference (e.g. a Zumrails transaction id).
REF_PREFIX_SUPPLIED = "portfolio:"          # the source file carried a stable id
REF_PREFIX_DERIVED = "import:"              # derived from (acct, date, amount)
LEGACY_REF_PREFIX_SUPPLIED = "turnkey:"     # retained: exists in live rows
#: Prefixes to try when checking whether a supplied id was already imported.
SUPPLIED_REF_PREFIXES: tuple[str, ...] = (
    REF_PREFIX_SUPPLIED,
    LEGACY_REF_PREFIX_SUPPLIED,
)

# --- platform_patient_fields keys + source tag -----------------------------
LEGACY_CUSTOMER_FIELD_KEY = "portfolio_legacy_customer_id"
IMPORT_ADDRESS_FIELD_KEY = "portfolio_import_address"
IMPORT_FIELD_SOURCE = "portfolio_import"
#: Legacy spellings, still read so pre-rename rows resolve.
LEGACY_CUSTOMER_FIELD_KEYS: tuple[str, ...] = (
    LEGACY_CUSTOMER_FIELD_KEY,
    "turnkey_legacy_customer_id",
)
IMPORT_ADDRESS_FIELD_KEYS: tuple[str, ...] = (
    IMPORT_ADDRESS_FIELD_KEY,
    "turnkey_import_address",
)
IMPORT_FIELD_SOURCES: tuple[str, ...] = (IMPORT_FIELD_SOURCE, "turnkey_import")

#: ``platform_loan_payments.method`` / ledger ``created_by`` provenance tag.
IMPORT_METHOD = "portfolio_import"
IMPORT_METHODS: tuple[str, ...] = (IMPORT_METHOD, "turnkey_migration")


def supplied_ref_variants(txn_id: str) -> tuple[str, ...]:
    """Every external_ref spelling a supplied transaction id could already have."""
    return tuple(f"{p}{txn_id}" for p in SUPPLIED_REF_PREFIXES)
