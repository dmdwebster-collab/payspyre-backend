"""Borrower-payload gaps B1 / B2 / B5 / B6 (video 11 re-watch, §4 P0 list).

DB-FREE by construction: the money shaping lives in ``borrower_portal`` as pure
functions over row-like objects, so these tests use lightweight stand-ins for
``PlatformLoanTransaction`` / ``PlatformLoanPayment`` / ``PlatformLoanScheduleItem``
instead of touching Postgres.

Run:
    source .venv/bin/activate && python -m pytest tests/test_borrower_payload_gaps.py -q
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.api.applicant.v1.endpoints.loans import PaymentRow, ScheduleRow
from app.api.applicant.v1.endpoints.new_loan import NewLoanBody
from app.services import borrower_portal

_TODAY = date(2026, 7, 1)
_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Row stand-ins (field-for-field with the real models' read surface)
# ---------------------------------------------------------------------------


@dataclass
class FakeTxn:
    seq: int
    txn_type: str
    amount_cents: int
    effective_date: date
    id: UUID = field(default_factory=uuid4)
    principal_cents: int = 0
    interest_cents: int = 0
    fees_cents: int = 0
    add_on_cents: int = 0
    payment_type: Optional[str] = "eft"
    repayment_mode: Optional[str] = "regular"
    created_by: str = "borrower"
    created_at: datetime = _NOW
    reference: str = "vend-loan-1"
    reverses_transaction_id: Optional[UUID] = None


@dataclass
class FakePayment:
    amount_cents: int
    received_at: datetime
    method: str = "zumrails"
    id: UUID = field(default_factory=uuid4)
    external_ref: Optional[str] = None


@dataclass
class FakeItem:
    installment_number: int
    due_date: date
    principal_cents: int
    interest_cents: int
    total_cents: int
    status: str = "scheduled"
    paid_cents: int = 0


# ---------------------------------------------------------------------------
# B1 — transactions tab: the eight columns off the LEDGER
# ---------------------------------------------------------------------------


def test_ledger_row_carries_posted_allocation_not_a_recomputation():
    txn = FakeTxn(
        seq=1,
        txn_type="payment",
        amount_cents=29_784,
        effective_date=date(2026, 6, 26),
        principal_cents=19_254,
        interest_cents=6_930,
        fees_cents=3_600,
    )
    (row,) = borrower_portal.shape_transaction_rows([txn], [])

    assert row["status"] == "posted"
    assert row["effective_date"] == date(2026, 6, 26)
    assert row["amount_cents"] == 29_784          # Total
    assert row["principal_cents"] == 19_254       # Principal
    assert row["interest_cents"] == 6_930         # Interest
    assert row["fee_total_cents"] == 3_600        # Fee
    assert row["repayment_mode_label"] == "Regular payment"
    assert row["repayment_method_label"] == "Direct bank transfer"
    assert row["allocation_source"] == "ledger"
    # The allocation is READ, never re-derived: the buckets need not sum to the
    # total (an overpayment's residue is not allocated), and we don't "fix" it.
    assert row["amount_cents"] >= (
        row["principal_cents"] + row["interest_cents"] + row["fee_total_cents"]
    )


def test_fee_column_includes_the_non_accruing_add_on_bucket():
    txn = FakeTxn(
        seq=1,
        txn_type="payment",
        amount_cents=5_000,
        effective_date=_TODAY,
        fees_cents=1_500,
        add_on_cents=3_500,  # e.g. an NSF fee repaid
        repayment_mode="add_on",
    )
    (row,) = borrower_portal.shape_transaction_rows([txn], [])
    assert row["fees_cents"] == 1_500
    assert row["add_on_cents"] == 3_500
    assert row["fee_total_cents"] == 5_000
    assert row["repayment_mode_label"] == "Add-on payment"


def test_automatic_payment_is_distinguished_from_a_borrower_made_one():
    auto = FakeTxn(
        seq=1,
        txn_type="payment",
        amount_cents=26_284,
        effective_date=_TODAY,
        created_by=borrower_portal.AUTOMATIC_ACTOR,
    )
    manual = FakeTxn(
        seq=2,
        txn_type="payment",
        amount_cents=26_284,
        effective_date=_TODAY,
        created_by=borrower_portal.BORROWER_ACTOR,
    )
    rows = borrower_portal.shape_transaction_rows([auto, manual], [])
    newest, oldest = rows  # most-recent first: seq 2 then seq 1
    assert oldest["is_automatic"] is True
    assert oldest["repayment_mode_label"] == "Automatic payment"
    assert newest["is_automatic"] is False
    assert newest["repayment_mode_label"] == "Regular payment"
    # Same repayment MODE on both — automatic-ness is provenance, not a mode.
    assert oldest["repayment_mode"] == newest["repayment_mode"] == "regular"


def test_reversed_payment_is_flagged_not_dropped_and_the_reversal_is_its_own_row():
    original = FakeTxn(
        seq=1,
        txn_type="payment",
        amount_cents=29_784,
        effective_date=date(2026, 6, 1),
        principal_cents=29_784,
        reference="vend-loan-1",
    )
    reversal = FakeTxn(
        seq=2,
        txn_type="reversal",
        amount_cents=29_784,
        effective_date=date(2026, 6, 10),
        payment_type=None,
        repayment_mode=None,
        reference="vend-loan-2",
        reverses_transaction_id=original.id,
    )
    rows = borrower_portal.shape_transaction_rows([original, reversal], [])
    assert len(rows) == 2  # nothing silently omitted

    rev, orig = rows  # most-recent first
    assert orig["status"] == "reversed"
    assert orig["reversed_on"] == date(2026, 6, 10)
    assert orig["amount_cents"] == 29_784

    assert rev["status"] == "reversal"
    assert rev["reverses_reference"] == "vend-loan-1"
    # The reversal shows the money it undoes — same reading loan_ledger and the
    # Reversed-payments report take.
    assert rev["amount_cents"] == 29_784
    assert rev["principal_cents"] == 29_784
    assert rev["effective_date"] == date(2026, 6, 10)


def test_charges_are_not_listed_as_repayments():
    fee = FakeTxn(
        seq=1, txn_type="fee", amount_cents=4_800, effective_date=_TODAY,
        add_on_cents=4_800, payment_type=None, repayment_mode=None,
    )
    disbursement = FakeTxn(
        seq=2, txn_type="disbursement", amount_cents=1_000_000,
        effective_date=_TODAY, payment_type=None, repayment_mode=None,
    )
    payment = FakeTxn(
        seq=3, txn_type="payment", amount_cents=10_000, effective_date=_TODAY
    )
    rows = borrower_portal.shape_transaction_rows([fee, disbursement, payment], [])
    assert [r["txn_type"] for r in rows] == ["payment"]


def test_rows_are_most_recent_first_by_effective_date_then_seq():
    old = FakeTxn(seq=1, txn_type="payment", amount_cents=1, effective_date=date(2026, 5, 1))
    mid = FakeTxn(seq=2, txn_type="payment", amount_cents=2, effective_date=date(2026, 6, 1))
    new = FakeTxn(seq=3, txn_type="payment", amount_cents=3, effective_date=date(2026, 6, 1))
    rows = borrower_portal.shape_transaction_rows([old, new, mid], [])
    assert [r["amount_cents"] for r in rows] == [3, 2, 1]


def test_pre_ledger_receipts_report_no_allocation_rather_than_a_guess():
    p1 = FakePayment(100_000, _NOW - timedelta(days=55))
    p2 = FakePayment(100_000, _NOW - timedelta(days=25), external_ref="zr-9")
    rows = borrower_portal.shape_transaction_rows([], [p1, p2], {p2.id})

    assert [r["received_at"] for r in rows] == [p2.received_at, p1.received_at]
    assert all(r["allocation_source"] == "unallocated" for r in rows)
    assert all(r["principal_cents"] is None for r in rows)
    assert all(r["fee_total_cents"] is None for r in rows)
    assert all(r["status"] == "posted" for r in rows)
    assert rows[0]["is_automatic"] is True   # settled an auto-collection attempt
    assert rows[1]["is_automatic"] is False
    assert rows[0]["method"] == "zumrails"   # pre-existing contract preserved


def test_payment_row_schema_accepts_both_shapes():
    ledger = FakeTxn(
        seq=1, txn_type="payment", amount_cents=1_000, effective_date=_TODAY,
        principal_cents=900, interest_cents=100,
    )
    for shaped in (
        borrower_portal.shape_transaction_rows([ledger], [])
        + borrower_portal.shape_transaction_rows([], [FakePayment(500, _NOW)])
    ):
        PaymentRow(**shaped)  # must validate against the response model


# ---------------------------------------------------------------------------
# B5 — per-installment fee
# ---------------------------------------------------------------------------


def test_schedule_fee_is_the_arithmetic_remainder_and_never_negative():
    item = FakeItem(1, _TODAY, principal_cents=9_658, interest_cents=552, total_cents=10_810)
    assert borrower_portal.schedule_fee_cents(item) == 600

    rounding_artefact = FakeItem(2, _TODAY, principal_cents=10_000, interest_cents=100, total_cents=10_099)
    assert borrower_portal.schedule_fee_cents(rounding_artefact) == 0


def test_schedule_views_expose_fee_in_both_presentations():
    items = [
        FakeItem(1, _TODAY, 9_658, 552, 10_810, status="paid", paid_cents=10_810),
        FakeItem(2, _TODAY + timedelta(days=14), 10_210, 100, 10_310),
    ]
    initial = borrower_portal.shape_schedule_rows(items, "initial", True)
    current = borrower_portal.shape_schedule_rows(items, "current", True)
    assert [r["fee_cents"] for r in initial] == [600, 0]
    assert [r["fee_cents"] for r in current] == [600, 0]
    ScheduleRow(
        installment_number=1, due_date=_TODAY, principal_cents=9_658,
        interest_cents=552, fee_cents=600, total_cents=10_810, paid_cents=0,
        status="scheduled",
    )


# ---------------------------------------------------------------------------
# B6 — new-loan calculator parameters
# ---------------------------------------------------------------------------


def test_new_loan_body_accepts_the_calculator_output():
    body = NewLoanBody(
        credit_product_id=uuid4(),
        requested_amount_cents=100_000,
        term_months=24,
        payment_frequency="biweekly",
        province="bc",
        promo_code="SPRING26",
    )
    assert body.term_months == 24
    assert body.payment_frequency == "biweekly"
    assert body.promo_code == "SPRING26"


def test_new_loan_body_stays_backwards_compatible():
    body = NewLoanBody(credit_product_id=uuid4(), requested_amount_cents=100_000)
    assert body.term_months is None
    assert body.payment_frequency is None
    assert body.province is None
    assert body.promo_code is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"term_months": 0},
        {"term_months": -12},
        {"province": "BRITISH COLUMBIA"},
        {"province": "B"},
        {"promo_code": "x" * 65},
    ],
)
def test_new_loan_body_rejects_malformed_terms(kwargs):
    with pytest.raises(ValidationError):
        NewLoanBody(
            credit_product_id=uuid4(), requested_amount_cents=100_000, **kwargs
        )


# ---------------------------------------------------------------------------
# Security mandates: adding fields must not add capability
# ---------------------------------------------------------------------------


def test_borrower_routes_add_no_bank_or_id_download_capability():
    from app.api.applicant.v1.endpoints import bank_accounts, loans, new_loan, profile

    for module in (bank_accounts, loans, new_loan, profile):
        for route in module.router.routes:
            methods = set(getattr(route, "methods", ()) or ())
            path = getattr(route, "path", "")
            # S1: no borrower route creates or deletes a bank account.
            assert "DELETE" not in methods, f"{path} exposes DELETE"
            if "bank" in path:
                assert methods <= {"GET", "HEAD", "OPTIONS", "POST"}
                assert path.endswith("/default") or methods <= {"GET", "HEAD", "OPTIONS"}
            # S4: no download/bytes route for stored ID images.
            assert "download" not in path
