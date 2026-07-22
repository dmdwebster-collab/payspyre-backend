"""Tests for the AI/ML Phase 0 training-data export (scripts/ml/export_training_data.py).

Two layers:

* **Pure-helper tests** — no DB; exercise the label/feature derivation and the
  row builder against plain dataclasses. These are the core correctness checks.
* **DB-seeded tests** — seed a handful of loans (active on-time, delinquent,
  charged-off, paid-off) on the Postgres test DB and run the full
  ``export()`` -> CSV path, asserting labels, features and the no-PII guarantee.

NOTE: per repo policy these are written but NOT executed here (shared test DB).
"""
import csv
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanPayment,
    PlatformLoanScheduleItem,
)
from app.models.platform.patient import PlatformPatient

import scripts.ml.export_training_data as m

AS_OF = date(2026, 6, 22)
SALT = "test-salt"


# ===========================================================================
# Pure helper tests (no database)
# ===========================================================================
class TestNoPii:
    def test_column_contract_has_no_pii(self):
        # Must not raise.
        m.assert_no_pii_columns()

    def test_no_pii_tokens_in_columns(self):
        for col in m.COLUMNS:
            low = col.lower()
            for tok in m._PII_TOKENS:
                assert tok not in low, f"{col} leaks PII token {tok}"

    def test_injected_pii_column_is_rejected(self):
        with pytest.raises(AssertionError):
            m.assert_no_pii_columns(["loan_id_hash", "legal_first_name"])


class TestHashId:
    def test_stable_and_opaque(self):
        h1 = m.hash_id("loan-123", SALT)
        h2 = m.hash_id("loan-123", SALT)
        assert h1 == h2 and len(h1) == 32
        assert "loan-123" not in h1  # not reversible at a glance

    def test_salt_changes_output(self):
        assert m.hash_id("x", "a") != m.hash_id("x", "b")

    def test_none_is_empty(self):
        assert m.hash_id(None, SALT) == ""


class TestMaxDaysPastDue:
    def test_earliest_unpaid_drives_dpd(self):
        sched = [
            m.ScheduleItemView(1, date(2026, 2, 1), 1000, 0, "late"),
            m.ScheduleItemView(2, date(2026, 3, 1), 1000, 0, "late"),
        ]
        assert m.max_days_past_due(sched, AS_OF) == (AS_OF - date(2026, 2, 1)).days

    def test_fully_paid_and_waived_and_future_excluded(self):
        sched = [
            m.ScheduleItemView(1, date(2026, 1, 1), 1000, 1000, "paid"),
            m.ScheduleItemView(2, date(2026, 1, 1), 1000, 0, "waived"),
            m.ScheduleItemView(3, date(2026, 12, 1), 1000, 0, "scheduled"),
        ]
        assert m.max_days_past_due(sched, AS_OF) == 0

    def test_partial_is_past_due(self):
        sched = [m.ScheduleItemView(1, date(2026, 6, 1), 1000, 400, "partial")]
        assert m.max_days_past_due(sched, AS_OF) == 21


class TestLabels:
    def test_charged_off_is_default(self):
        lab = m.derive_labels(status="charged_off", max_dpd=0)
        assert lab["outcome_default"] == 1
        # charged_off with 0 DPD does not, by itself, set the delinquent label
        # (delinquent label fires on status=='delinquent' OR max_dpd>=30).
        assert lab["outcome_delinquent"] == 0

    def test_past_default_threshold_is_default(self):
        # Dave's rule: default at >90 DPD (BucketPolicy.default_min_dpd = 91).
        lab = m.derive_labels(status="active", max_dpd=120)
        assert lab["outcome_default"] == 1 and lab["outcome_delinquent"] == 1
        assert m.derive_labels(status="active", max_dpd=91)["outcome_default"] == 1
        # 90 DPD is the last non-default day.
        assert m.derive_labels(status="active", max_dpd=90)["outcome_default"] == 0

    def test_dpd_30_is_delinquent_not_default(self):
        lab = m.derive_labels(status="active", max_dpd=45)
        assert lab["outcome_delinquent"] == 1 and lab["outcome_default"] == 0

    def test_paid_off(self):
        lab = m.derive_labels(status="paid_off", max_dpd=0)
        assert lab["outcome_paid_off"] == 1 and lab["outcome_default"] == 0

    def test_buckets(self):
        # Single shared vocabulary with the reports (P0/T4).
        assert m.days_past_due_bucket(0) == "good"
        assert m.days_past_due_bucket(15) == "1-30"
        assert m.days_past_due_bucket(30) == "1-30"
        assert m.days_past_due_bucket(45) == "31-60"
        assert m.days_past_due_bucket(75) == "61-90"
        assert m.days_past_due_bucket(90) == "61-90"
        assert m.days_past_due_bucket(200) == "91plus"


class TestDecisionFeatures:
    def test_reasons_joined_and_score_absent(self):
        feats = m.origination_decision_features(
            {"decision": "approved", "decision_reasons": ["manual_review_band", "x"]}
        )
        assert feats["decision"] == "approved"
        assert feats["decision_reason_codes"] == "manual_review_band|x"
        # Audit gap: no raw bureau score persisted -> null.
        assert feats["decision_score"] is None
        assert feats["decision_score_band_min"] is None

    def test_none_decision_is_safe(self):
        feats = m.origination_decision_features(None)
        assert feats["decision"] is None and feats["decision_reason_codes"] == ""

    def test_future_band_snapshot_is_picked_up(self):
        feats = m.origination_decision_features(
            {"decision": "approved", "score": 712, "band": {"min": 600, "max": 679}}
        )
        assert feats["decision_score"] == 712
        assert feats["decision_score_band_min"] == 600


class TestSelfReported:
    def test_cents_keys_used_directly(self):
        feats = m.self_reported_features(
            {"income_cents": 6_000_000, "liabilities_cents": 1_200_000}
        )
        assert feats["self_reported_income_cents"] == 6_000_000
        assert feats["self_reported_liabilities_cents"] == 1_200_000

    def test_missing_is_none(self):
        feats = m.self_reported_features({})
        assert feats["self_reported_income_cents"] is None


class TestPerformanceFeatures:
    def test_counts_ratios_and_nsf(self):
        sched = [
            m.ScheduleItemView(1, date(2026, 1, 1), 1000, 1000, "paid"),
            m.ScheduleItemView(2, date(2026, 2, 1), 1000, 0, "late"),
            m.ScheduleItemView(3, date(2026, 7, 1), 1000, 0, "scheduled"),  # future
        ]
        payments = [
            m.PaymentView(1000, None, "zumrails"),
            m.PaymentView(1000, None, "nsf_returned"),  # excluded from total
        ]
        feats = m.performance_features(
            status="delinquent",
            principal_balance_cents=2000,
            schedule=sched,
            payments=payments,
            as_of=AS_OF,
        )
        assert feats["installments_total"] == 3
        assert feats["installments_due_count"] == 2  # future item excluded
        assert feats["installments_paid"] == 1
        assert feats["installments_late"] == 1
        assert feats["nsf_count"] == 1
        assert feats["total_paid_cents"] == 1000  # nsf payment excluded
        assert feats["on_time_payment_ratio"] == 0.5  # 1 of 2 due paid on time


class TestBuildRow:
    def _record(self, **over):
        base = dict(
            loan_id="L1",
            application_id="A1",
            source="application",
            principal_cents=2_500_000,
            annual_rate_bps=1299,
            term_months=24,
            principal_balance_cents=2_000_000,
            status="delinquent",
            requested_amount_cents=2_500_000,
            decision={"decision": "approved", "decision_reasons": ["manual_review_band"]},
            self_reported={"income_cents": 6_000_000},
            applicant_role="primary",
            co_applicant_of_application_id=None,
            verification_depth="id_bank_cb_verified",
            schedule=[
                m.ScheduleItemView(1, date(2026, 2, 1), 1000, 0, "late"),
            ],
            payments=[],
        )
        base.update(over)
        return m.LoanRecord(**base)

    def test_keys_match_columns_exactly(self):
        row = m.build_row(self._record(), as_of=AS_OF, salt=SALT)
        assert set(row.keys()) == set(m.COLUMNS)

    def test_no_pii_keys_in_row(self):
        row = m.build_row(self._record(), as_of=AS_OF, salt=SALT)
        for key in row:
            for tok in m._PII_TOKENS:
                assert tok not in key.lower()

    def test_migrated_loan_origination_features_null(self):
        rec = self._record(
            application_id=None,
            source="turnkey_migration",
            requested_amount_cents=None,
            decision=None,
            self_reported=None,
            applicant_role=None,
            verification_depth=None,
        )
        row = m.build_row(rec, as_of=AS_OF, salt=SALT)
        assert row["application_id_hash"] == ""
        assert row["is_migrated"] == 1
        assert row["decision"] is None
        assert row["requested_amount_cents"] is None

    def test_co_applicant_flag(self):
        rec = self._record(
            applicant_role="co_applicant",
            co_applicant_of_application_id="primary-app-id",
        )
        row = m.build_row(rec, as_of=AS_OF, salt=SALT)
        assert row["is_co_applicant"] == 1
        assert row["applicant_role"] == "co_applicant"


class TestSerialize:
    def test_none_float_bool(self):
        assert m.serialize_cell(None) == ""
        assert m.serialize_cell(True) == "1"
        assert m.serialize_cell(False) == "0"
        assert m.serialize_cell(0.333333333) == "0.333333"
        assert m.serialize_cell(42) == "42"


# ===========================================================================
# DB-seeded integration tests (Postgres test DB)
# ===========================================================================
def _patient(db: Session, depth="id_bank_cb_verified") -> PlatformPatient:
    p = PlatformPatient(
        email=f"ml-{uuid.uuid4().hex[:8]}@example.com",
        legal_first_name="Test",
        legal_last_name="Borrower",
        verification_depth=depth,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _application(db: Session, patient: PlatformPatient, *, status="approved") -> PlatformCreditApplication:
    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )
    row = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=product.id,
        credit_product_version=product.version,
        requested_amount_cents=2_500_000,
        requested_amount_source="clinic",
        status=status,
        applicant_role="primary",
        decision={"decision": "approved", "decision_reasons": ["clean_above_band"]},
        self_reported={"income_cents": 7_200_000},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _loan(db: Session, app_row, *, status, balance=2_500_000) -> PlatformLoan:
    loan = PlatformLoan(
        application_id=app_row.id if app_row else None,
        source="application" if app_row else "turnkey_migration",
        principal_cents=2_500_000,
        annual_rate_bps=1299,
        term_months=24,
        status=status,
        principal_balance_cents=balance,
    )
    db.add(loan)
    db.commit()
    db.refresh(loan)
    return loan


def _item(db, loan, *, n, due, status="scheduled", total=111800, paid=0):
    db.add(
        PlatformLoanScheduleItem(
            loan_id=loan.id,
            installment_number=n,
            due_date=due,
            principal_cents=100000,
            interest_cents=11800,
            total_cents=total,
            paid_cents=paid,
            status=status,
        )
    )
    db.commit()


def _payment(db, loan, *, amount, when, method="zumrails"):
    db.add(
        PlatformLoanPayment(
            loan_id=loan.id, amount_cents=amount, received_at=when, method=method
        )
    )
    db.commit()


class TestExportEndToEnd:
    def test_four_loan_archetypes_export_correctly(self, db_session: Session, tmp_path):
        now = datetime.now(timezone.utc)

        # 1. active, on-time
        p1 = _patient(db_session)
        a1 = _application(db_session, p1)
        l1 = _loan(db_session, a1, status="active", balance=2_300_000)
        _item(db_session, l1, n=1, due=AS_OF - timedelta(days=60), status="paid", paid=111800)
        _item(db_session, l1, n=2, due=AS_OF + timedelta(days=30))
        _payment(db_session, l1, amount=111800, when=now)

        # 2. delinquent (31-60 dpd)
        p2 = _patient(db_session)
        a2 = _application(db_session, p2)
        l2 = _loan(db_session, a2, status="delinquent")
        _item(db_session, l2, n=1, due=AS_OF - timedelta(days=45), status="late")

        # 3. charged-off
        p3 = _patient(db_session)
        a3 = _application(db_session, p3)
        l3 = _loan(db_session, a3, status="charged_off")
        _item(db_session, l3, n=1, due=AS_OF - timedelta(days=200), status="late")
        _payment(db_session, l3, amount=50000, when=now, method="nsf_returned")

        # 4. paid-off
        p4 = _patient(db_session)
        a4 = _application(db_session, p4)
        l4 = _loan(db_session, a4, status="paid_off", balance=0)
        _item(db_session, l4, n=1, due=AS_OF - timedelta(days=90), status="paid", paid=111800)

        out = tmp_path / "training_data.csv"
        count = m.export(db_session, out_path=str(out), as_of=AS_OF, salt=SALT)
        assert count >= 4

        rows = {r["loan_id_hash"]: r for r in csv.DictReader(out.open())}

        r1 = rows[m.hash_id(l1.id, SALT)]
        assert r1["status"] == "active"
        assert r1["outcome_default"] == "0" and r1["outcome_delinquent"] == "0"
        assert r1["days_past_due_bucket"] == "good"
        assert r1["decision"] == "approved"
        assert r1["decision_reason_codes"] == "clean_above_band"
        assert r1["self_reported_income_cents"] == "7200000"
        assert r1["verification_depth"] == "id_bank_cb_verified"
        # Bureau gap: no raw score persisted.
        assert r1["decision_score"] == ""

        r2 = rows[m.hash_id(l2.id, SALT)]
        assert r2["outcome_delinquent"] == "1" and r2["outcome_default"] == "0"
        assert r2["days_past_due_bucket"] == "31-60"

        r3 = rows[m.hash_id(l3.id, SALT)]
        assert r3["outcome_default"] == "1"
        assert r3["nsf_count"] == "1"
        assert r3["total_paid_cents"] == "0"  # only NSF payment, excluded

        r4 = rows[m.hash_id(l4.id, SALT)]
        assert r4["outcome_paid_off"] == "1"

    def test_migrated_loan_without_application(self, db_session: Session, tmp_path):
        loan = _loan(db_session, None, status="paid_off", balance=0)
        loan.legacy_account_number = f"LEGACY-{uuid.uuid4().hex[:8]}"
        db_session.commit()
        _item(db_session, loan, n=1, due=AS_OF - timedelta(days=120), status="paid", paid=111800)

        out = tmp_path / "migrated.csv"
        m.export(db_session, out_path=str(out), as_of=AS_OF, salt=SALT)
        rows = {r["loan_id_hash"]: r for r in csv.DictReader(out.open())}
        r = rows[m.hash_id(loan.id, SALT)]
        assert r["application_id_hash"] == ""
        assert r["is_migrated"] == "1"
        assert r["decision"] == ""  # no application -> null decision
        assert r["requested_amount_cents"] == ""
        assert r["outcome_paid_off"] == "1"

    def test_csv_header_is_column_contract(self, db_session: Session, tmp_path):
        p = _patient(db_session)
        a = _application(db_session, p)
        _loan(db_session, a, status="active")
        out = tmp_path / "header.csv"
        m.export(db_session, out_path=str(out), as_of=AS_OF, salt=SALT)
        header = next(csv.reader(out.open()))
        assert header == m.COLUMNS
        # And no PII columns in the actual written file.
        for col in header:
            for tok in m._PII_TOKENS:
                assert tok not in col.lower()
