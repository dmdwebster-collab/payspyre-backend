"""DB-free tests for WS-I (Archive + servicing misc).

All in-memory fakes — NO database (shared remote test DB; do not run the full
suite). Covers the pure cores and fake-session service paths:

  * archive          — close-reason derivation, polymorphic detail shape.
  * blacklists       — value normalization/masking, the never-auto-reject
                       screen policy (pinned), CRUD soft-delete + audit.
  * bureau_reporting — Metro2-style flat-file structure + bucket→status map,
                       reportable-account selection is exercised via the pure
                       renderer with fake accounts.
  * audit_diffs      — old→new field diffing + event rendering.
  * loan_ledger      — backdate validation bounds + payment-type set.
  * record_payment   — permission-bounded backdating end-to-end (fake session),
                       explicit payment_type surfaced on the ledger row.
  * migration 063 chain pin (merge-train convention).

Run just this file:

    source .venv/bin/activate && \
        python -m pytest tests/test_parity_i_archive_misc.py -p no:warnings -q
"""
import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.services import archive, audit_diffs, blacklists, bureau_reporting, loan_ledger
from app.services.bureau_reporting import ReportableAccount, generate_batch_content
from app.services.loan_servicing import record_payment

D = date


# ===========================================================================
# archive — close-reason derivation + polymorphic detail
# ===========================================================================


def test_close_reason_for_application():
    assert archive.close_reason_for_application("declined", None) == "rejected"
    assert archive.close_reason_for_application("withdrawn", {}) == "cancelled"
    assert archive.close_reason_for_application("expired", {}) == "expired"
    assert (
        archive.close_reason_for_application(
            "expired", {"expiry_reason": "bank_verification"}
        )
        == "bank_verification_expired"
    )
    # Non-terminal → None.
    assert archive.close_reason_for_application("approved", None) is None
    assert archive.close_reason_for_application("under_review", None) is None


def test_close_reason_for_loan():
    assert archive.close_reason_for_loan("paid_off") == "repaid"
    assert archive.close_reason_for_loan("charged_off") == "written_off"
    assert archive.close_reason_for_loan("cancelled") == "cancelled"
    assert archive.close_reason_for_loan("active") is None
    assert archive.close_reason_for_loan("delinquent") is None


def test_decision_snapshot_block_is_frozen_fields_only():
    class _App:
        decision = {"decision": "approved", "decision_reasons": ["clean"]}
        decision_at = datetime(2026, 5, 13, 17, 5, tzinfo=timezone.utc)
        decision_by = "auto"
        credit_product_version = 3
        product_config_snapshot = {"identity": {"required": True}}

    block = archive.decision_snapshot_block(_App())
    assert block["decision"]["decision"] == "approved"
    assert block["decision_by"] == "auto"
    assert block["credit_product_version"] == 3
    assert block["product_config_snapshot"] == {"identity": {"required": True}}
    assert archive.decision_snapshot_block(None) is None


def test_close_reasons_vocabulary_is_complete():
    assert set(archive.CLOSE_REASONS) == {
        "rejected",
        "cancelled",
        "expired",
        "bank_verification_expired",
        "repaid",
        "written_off",
    }


# ===========================================================================
# blacklists — normalization, masking, and THE never-auto-reject policy
# ===========================================================================


def test_normalize_value_per_category():
    assert blacklists.normalize_value("phone", "+1 (250) 555-0134") == "2505550134"
    assert blacklists.normalize_value("phone", "12505550134") == "2505550134"
    assert blacklists.normalize_value("sin", "123-456-789") == "123456789"
    assert blacklists.normalize_value("account_number", " 000 123 ") == "000123"
    assert blacklists.normalize_value("email", "  John@Example.COM ") == "john@example.com"
    assert blacklists.normalize_value("name", "  John   J   Doe ") == "john j doe"


def test_normalize_value_rejects_empty_and_unknown():
    with pytest.raises(blacklists.BlacklistError):
        blacklists.normalize_value("email", "  ")
    with pytest.raises(blacklists.BlacklistError):
        blacklists.normalize_value("nope", "x")
    with pytest.raises(blacklists.BlacklistError):
        blacklists.normalize_value("phone", "no-digits-here")


def test_mask_value_hides_the_middle():
    assert blacklists.mask_value("phone", "2505550134").startswith("25")
    assert blacklists.mask_value("phone", "2505550134").endswith("34")
    assert "*" in blacklists.mask_value("phone", "2505550134")
    masked_email = blacklists.mask_value("email", "johndoe@example.com")
    assert masked_email.endswith("@example.com")
    assert "*" in masked_email


def _match(category="phone"):
    return blacklists.BlacklistMatch(
        entry_id="e1", category=category, masked_value="25****34", reason="fraud ring"
    )


def test_apply_screen_no_match_is_passthrough():
    out = blacklists.apply_screen("approved", "approved", ["clean"], [])
    assert out.decision == "approved"
    assert out.next_state == "approved"
    assert out.flagged is False
    assert out.downgraded is False


def test_apply_screen_match_downgrades_approval_to_manual_review():
    out = blacklists.apply_screen("approved", "approved", ["clean"], [_match()])
    assert out.decision == "manual_review"
    assert out.next_state == "under_review"
    assert blacklists.BLACKLIST_REVIEW_REASON in out.decision_reasons
    assert out.flagged is True
    assert out.downgraded is True


def test_apply_screen_never_auto_rejects():
    # A declined file stays declined — a match NEVER worsens a non-approval.
    out = blacklists.apply_screen("declined", "declined", ["low_score"], [_match()])
    assert out.decision == "declined"
    assert out.next_state == "declined"
    assert out.flagged is True
    assert out.downgraded is False
    # manual_review file also unchanged (already going to a human).
    out2 = blacklists.apply_screen("manual_review", "under_review", [], [_match()])
    assert out2.decision == "manual_review"
    assert out2.downgraded is False


def test_screenable_values_collects_the_right_categories():
    class _App:
        id = uuid4()
        email = "borrower@example.com"
        main_phone = "250-555-0134"
        alternative_phone = None
        first_name = "John"
        last_name = "Doe"
        id_type = "drivers_license"
        id_number = "D1234-5678"

    class _Patient:
        email = "patient-login@example.com"

    values = blacklists.screenable_values(_App(), _Patient())
    assert "borrower@example.com" in values["email"]
    assert "patient-login@example.com" in values["email"]
    assert "250-555-0134" in values["phone"]
    assert values["name"] == ["John Doe"]
    assert values["drivers_license"] == ["D1234-5678"]
    # No account number source on this app.
    assert "account_number" not in values


# --- blacklist CRUD with a fake session --------------------------------------


class _RecordingSession:
    """Minimal fake: records added rows, no persistence, query returns nothing
    (first()/all() empty) unless seeded."""

    def __init__(self, existing=None):
        self.added = []
        self._existing = existing or []

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass

    def commit(self):
        pass

    def refresh(self, obj, **k):
        pass

    def query(self, *a, **k):
        return _SeededQuery(self._existing)


class _SeededQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


def test_add_entry_requires_reason_and_audits():
    db = _RecordingSession()
    with pytest.raises(blacklists.BlacklistError):
        blacklists.add_entry(db, category="phone", value="2505550134", reason="  ", actor="u1")

    db = _RecordingSession()
    row = blacklists.add_entry(
        db, category="phone", value="250-555-0134", reason="fraud ring", actor="u1"
    )
    assert row.value_normalized == "2505550134"
    assert row.active is True
    # An audit event was appended alongside the entry.
    event_types = {getattr(o, "event_type", None) for o in db.added}
    assert blacklists.ENTRY_ADDED_EVENT in event_types


def test_add_entry_idempotent_on_active_duplicate():
    existing = type("E", (), {"id": uuid4(), "active": True})()
    db = _RecordingSession(existing=[existing])
    row = blacklists.add_entry(
        db, category="email", value="dup@example.com", reason="again", actor="u1"
    )
    assert row is existing
    # No new row / event added (returned the existing active entry).
    assert db.added == []


def test_deactivate_entry_is_soft_delete_and_audited():
    entry = type(
        "E",
        (),
        {
            "id": uuid4(),
            "active": True,
            "category": "phone",
            "value_normalized": "2505550134",
            "deactivated_by": None,
            "deactivated_at": None,
        },
    )()
    db = _RecordingSession(existing=[entry])
    out = blacklists.deactivate_entry(db, entry.id, actor="u2")
    assert out is entry
    assert entry.active is False
    assert entry.deactivated_by == "u2"
    event_types = {getattr(o, "event_type", None) for o in db.added}
    assert blacklists.ENTRY_DEACTIVATED_EVENT in event_types


def test_deactivate_missing_entry_returns_none():
    db = _RecordingSession(existing=[])
    assert blacklists.deactivate_entry(db, uuid4(), actor="u2") is None


# ===========================================================================
# bureau_reporting — Metro2-style flat file (pure renderer)
# ===========================================================================


def _acct(bucket="pot_60", balance=500_00, dpd=65):
    return ReportableAccount(
        account_number="ACC-1001",
        consumer_name="Doe, John",
        date_opened=D(2026, 1, 15),
        original_amount_cents=1_000_00,
        balance_cents=balance,
        days_past_due=dpd,
        bucket=bucket,
    )


def test_generate_batch_content_structure():
    content = generate_batch_content(D(2026, 6, 1), [_acct()])
    lines = content.strip().split("\n")
    assert lines[0].startswith("HEADER")
    assert "PAYSPYRE FINANCIAL" in lines[0]
    assert "DO NOT TRANSMIT" in lines[0]
    assert lines[1].startswith("B")
    assert "ACC-1001" in lines[1]
    # status code for pot_60 is 78.
    assert "78" in lines[1]
    # trailer carries the record count (1).
    assert lines[-1].startswith("TRAILER")
    assert "000000001" in lines[-1]


def test_generate_batch_empty_month_has_header_and_zero_trailer():
    content = generate_batch_content(D(2026, 6, 1), [])
    lines = content.strip().split("\n")
    assert lines[0].startswith("HEADER")
    assert lines[-1].startswith("TRAILER")
    assert "000000000" in lines[-1]  # zero records


def test_metro2_status_covers_reportable_buckets():
    # Every bureau-reportable bucket (pot_60+) resolves to a Metro2 status code.
    for bucket in ("pot_60", "pot_90", "default", "insolvency", "written_off"):
        assert bureau_reporting.metro2_status(bucket, 95) is not None
    # written_off reports as charged off (97) regardless of DPD.
    assert bureau_reporting.metro2_status("written_off", 0) == "97"
    # Non-reportable buckets never resolve.
    assert bureau_reporting.metro2_status("pot_30", 45) is None


def test_metro2_aging_code_follows_actual_dpd_not_the_bucket():
    """P0/T4: `default` now starts at 91 DPD (Dave's ">90"), so mapping the
    bucket straight to a "120+" code would report a wrong ageing to Equifax."""
    assert bureau_reporting.metro2_status("default", 95) == "80"    # 90-119
    assert bureau_reporting.metro2_status("default", 130) == "84"   # 120+
    assert bureau_reporting.metro2_status("pot_60", 65) == "78"     # 60-89
    assert bureau_reporting.metro2_status("pot_90", 90) == "80"


def test_generate_batch_skips_non_reportable_bucket():
    # A stray current-bucket account never leaks into the file.
    good = _acct(bucket="pot_60")
    stray = ReportableAccount(
        account_number="ACC-STRAY",
        consumer_name="X",
        date_opened=None,
        original_amount_cents=0,
        balance_cents=100,
        days_past_due=0,
        bucket="current",
    )
    content = generate_batch_content(D(2026, 6, 1), [good, stray])
    assert "ACC-STRAY" not in content
    assert "ACC-1001" in content


# ===========================================================================
# audit_diffs — old→new field diffing
# ===========================================================================


def test_diff_fields_changed_set_cleared():
    diffs = audit_diffs.diff_fields(
        {"status": "under_review", "amount": 100, "same": 1, "old_key": "gone"},
        {"status": "approved", "amount": 100, "same": 1, "new_key": "x"},
    )
    by_field = {d["field"]: d for d in diffs}
    # changed
    assert by_field["status"]["old"] == "under_review"
    assert by_field["status"]["new"] == "approved"
    assert "under_review → approved" in by_field["status"]["display"]
    # set (only in after)
    assert by_field["new_key"]["old"] is None
    # cleared (only in before)
    assert by_field["old_key"]["new"] is None
    # unchanged 'same' + equal 'amount' dropped
    assert "same" not in by_field
    assert "amount" not in by_field


def test_render_event_diff_splits_envelope_and_context():
    payload = {
        "v": 1,
        "actor": {"type": "staff", "id": "u1"},
        "before": {"status": "active"},
        "after": {"status": "paid_off"},
        "loan_id": "loan-9",
        "comment": "paid in full",
    }
    row = audit_diffs.render_event_diff(
        42, datetime(2026, 7, 1, tzinfo=timezone.utc), "loan_closed", "u1", payload
    )
    assert row["event_id"] == 42
    assert row["actor"] == "u1"
    assert any(c["field"] == "status" for c in row["changes"])
    # non-envelope keys become context
    assert row["context"]["loan_id"] == "loan-9"
    assert row["context"]["comment"] == "paid in full"
    assert "before" not in row["context"] and "after" not in row["context"]


# ===========================================================================
# loan_ledger — backdate validation bounds + payment-type set
# ===========================================================================


def test_payment_types_set():
    assert loan_ledger.PAYMENT_TYPES == ("cash", "check", "eft", "credit_card", "adjustment")


def test_validate_backdate_ok_within_window():
    proc = D(2026, 7, 20)
    # 10 days back, with a comment — fine.
    loan_ledger.validate_backdate(proc - timedelta(days=10), proc, comment="borrower cheque dated earlier")


def test_validate_backdate_rejects_forward_dating():
    proc = D(2026, 7, 20)
    with pytest.raises(ValueError, match="forward-dated"):
        loan_ledger.validate_backdate(proc + timedelta(days=1), proc, comment="x")


def test_validate_backdate_enforces_window():
    proc = D(2026, 7, 20)
    with pytest.raises(ValueError, match="bounded"):
        loan_ledger.validate_backdate(
            proc - timedelta(days=loan_ledger.BACKDATE_WINDOW_DAYS + 1), proc, comment="x"
        )


def test_validate_backdate_requires_comment():
    proc = D(2026, 7, 20)
    with pytest.raises(ValueError, match="comment"):
        loan_ledger.validate_backdate(proc - timedelta(days=5), proc, comment="  ")


# ===========================================================================
# record_payment — permission-bounded backdating + explicit payment_type
# (fake session, MONEY-PATH — mirrors test_repayment_modes fakes)
# ===========================================================================


class _NoResultQuery:
    def filter(self, *a, **k):
        return self

    def first(self):
        return None

    def all(self):
        return []


class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def query(self, *a, **k):
        return _NoResultQuery()

    def flush(self):
        pass

    def commit(self):
        pass

    def refresh(self, obj, **kwargs):
        pass


class _Item:
    def __init__(self, n, principal, interest, *, status="scheduled"):
        self.installment_number = n
        self.principal_cents = principal
        self.interest_cents = interest
        self.total_cents = principal + interest
        self.status = status
        self.paid_cents = 0
        self.due_date = D(2026, 1 + n, 1)


class _Loan:
    def __init__(self):
        self.id = "loan-1"
        self.application_id = None
        self.principal_cents = 100_000
        self.principal_balance_cents = 100_000
        self.annual_rate_bps = 3650
        self.status = "active"
        self.disbursed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.schedule = [_Item(n, 25_000, 1_500) for n in range(1, 5)]
        self.transactions = []


def test_record_payment_default_effective_equals_processing():
    loan = _Loan()
    received = datetime(2026, 3, 1, tzinfo=timezone.utc)
    record_payment(_FakeSession(), loan, 10_000, received, "eft")
    txn = next(t for t in loan.transactions if t.txn_type == "payment")
    assert txn.effective_date == D(2026, 3, 1)
    assert txn.processing_date == D(2026, 3, 1)


def test_record_payment_backdate_within_window_sets_dual_dates():
    loan = _Loan()
    received = datetime(2026, 3, 20, tzinfo=timezone.utc)
    record_payment(
        _FakeSession(),
        loan,
        10_000,
        received,
        "cheque",
        effective_date=D(2026, 3, 5),
        comment="borrower cheque dated Mar 5",
    )
    txn = next(t for t in loan.transactions if t.txn_type == "payment")
    assert txn.effective_date == D(2026, 3, 5)
    assert txn.processing_date == D(2026, 3, 20)  # processing stays the received date


def test_record_payment_backdate_without_comment_rejected():
    loan = _Loan()
    received = datetime(2026, 3, 20, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="comment"):
        record_payment(
            _FakeSession(), loan, 10_000, received, "cash",
            effective_date=D(2026, 3, 5),
        )


def test_record_payment_forward_effective_rejected():
    loan = _Loan()
    received = datetime(2026, 3, 20, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="forward-dated"):
        record_payment(
            _FakeSession(), loan, 10_000, received, "cash",
            effective_date=D(2026, 3, 25), comment="x",
        )


def test_record_payment_explicit_payment_type_surfaced():
    loan = _Loan()
    received = datetime(2026, 3, 1, tzinfo=timezone.utc)
    record_payment(
        _FakeSession(), loan, 10_000, received, "manual", payment_type="cash"
    )
    txn = next(t for t in loan.transactions if t.txn_type == "payment")
    assert txn.payment_type == "cash"


def test_record_payment_unknown_payment_type_rejected():
    loan = _Loan()
    received = datetime(2026, 3, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="unknown payment type"):
        record_payment(
            _FakeSession(), loan, 10_000, received, "manual", payment_type="bitcoin"
        )


# ===========================================================================
# migration 063 chain pin (merge-train convention)
# ===========================================================================


def test_migration_063_chain_pin():
    """The down_revision is the single head this branch was cut from. The
    orchestrator re-chains parallel workstreams; this pin makes a silent fork
    (which would split the alembic head) fail loudly."""
    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "063_archive_misc.py"
    )
    spec = importlib.util.spec_from_file_location("m063", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "063_archive_misc"
    assert mod.down_revision == "062_reports_depth"
