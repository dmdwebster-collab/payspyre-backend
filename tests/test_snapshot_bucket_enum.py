"""Regression guard for the delinquency-snapshot bucket ENUM.

`PlatformLoanDelinquencySnapshot.bucket` was declared as
`ENUM(name="platform_delinquency_bucket", create_type=False)` with NO value
list. A SQLAlchemy ENUM with an empty `.enums` cannot map DB rows back on read,
so serializing any loan that HAS delinquency snapshots raised on read — a 500
on `GET /admin/loans/{loan_id}`. It must carry the same value list as
`PlatformLoan.current_bucket`. This test asserts the property whose absence
caused the bug, so it cannot silently return.
"""

from app.models.platform.loan import PlatformLoan, PlatformLoanDelinquencySnapshot

EXPECTED_BUCKETS = {
    "current",
    "current_month_late",
    "pot_30",
    "pot_60",
    "pot_90",
    "default",
    "insolvency",
    "written_off",
}


def test_snapshot_bucket_enum_declares_its_values():
    enum_type = PlatformLoanDelinquencySnapshot.__table__.c.bucket.type
    assert set(enum_type.enums) == EXPECTED_BUCKETS


def test_snapshot_bucket_matches_loan_current_bucket():
    # Both columns are the same DB type; their Python declarations must agree,
    # or one of them will fail to round-trip.
    snap = set(PlatformLoanDelinquencySnapshot.__table__.c.bucket.type.enums)
    loan = set(PlatformLoan.__table__.c.current_bucket.type.enums)
    assert snap == loan
