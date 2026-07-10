"""Cutover CSV import (WS-D) — idempotency-key derivation + report shape. Pure/DB-free."""
from datetime import date

from app.services.migration import turnkey_payments
from app.services.migration.csv_import import (
    ImportContext,
    derive_disbursement_ref,
    derive_payment_external_ref,
    validate_csv,
)


def test_payment_ref_derived_from_acct_date_amount():
    ref = derive_payment_external_ref("BC4906-0001", date(2025, 6, 1), 12345)
    assert ref == "import:BC4906-0001:2025-06-01:12345"
    # Deterministic: same inputs, same key (idempotent re-import).
    assert ref == derive_payment_external_ref("BC4906-0001", date(2025, 6, 1), 12345)


def test_payment_ref_distinct_when_any_component_differs():
    base = derive_payment_external_ref("A1", date(2025, 6, 1), 100)
    assert derive_payment_external_ref("A2", date(2025, 6, 1), 100) != base
    assert derive_payment_external_ref("A1", date(2025, 6, 2), 100) != base
    assert derive_payment_external_ref("A1", date(2025, 6, 1), 101) != base


def test_supplied_reference_uses_turnkey_namespace_matching_existing_importer():
    # Must share the namespace of the existing Turnkey payment importer so the
    # two paths dedupe against EACH OTHER on re-import.
    ref = derive_payment_external_ref("A1", date(2025, 6, 1), 100, reference=" TXN-9 ")
    assert ref == "turnkey:TXN-9"
    assert ref.startswith(turnkey_payments.EXTERNAL_REF_PREFIX)


def test_disbursement_ref_namespaces():
    assert derive_disbursement_ref("A1", date(2024, 1, 10)) == "import:disb:A1:2024-01-10"
    assert derive_disbursement_ref("A1", date(2024, 1, 10), "DSB-4") == "turnkey:DSB-4"


def test_derived_and_supplied_refs_cannot_collide():
    derived = derive_payment_external_ref("A1", date(2025, 6, 1), 100)
    supplied = derive_payment_external_ref("A1", date(2025, 6, 1), 100, reference="A1:2025-06-01:100")
    assert derived != supplied  # distinct namespaces


def test_preview_report_is_json_shaped_and_counts_consistent():
    ctx = ImportContext(existing_loan_accounts={"A1"})
    r = validate_csv(
        "payments",
        "legacy_account_number,payment_date,amount,type,reference\n"
        "A1,2025-06-01,10.00,PAD,\n"
        "A1,not-a-date,10.00,PAD,\n",
        ctx,
    )
    report = r.report()
    assert report["row_count"] == 2
    assert report["valid_count"] == 1
    assert report["error_count"] == 1
    assert report["errors"][0] == {
        "row": 2,
        "field": "payment_date",
        "message": "invalid date — expected YYYY-MM-DD (got 'not-a-date')",
    }
    # JSONB-serializable: only plain types.
    import json

    json.dumps(report)
