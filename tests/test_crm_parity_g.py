"""DB-FREE unit tests for WS-G (Vendor + Customer CRM).

Shared remote test DB → these tests touch NO database. They exercise the pure
functions and the fake-session service paths only:

* migration ``061_crm`` down_revision pin (re-chain guard);
* clinic 9-role permission matrix (validate / has_permission / dependency);
* vendor bank-account masking + last-4 extraction;
* onboarding forward-only status math;
* vendor document expiry-alert threshold math + render;
* customer-block gate + block/unblock over a fake session.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.api.v1.endpoints import admin_crm_vendors as crm_v
from app.services import clinic_permissions as cp
from app.services import customer_blocks as cb
from app.services import vendor_doc_alerts as vda


# ---------------------------------------------------------------------------
# Migration re-chain pin
# ---------------------------------------------------------------------------


def test_migration_061_down_revision():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "061_crm.py"
    )
    spec = importlib.util.spec_from_file_location("migration_061_crm", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "061_crm"
    # Pinned to the single head at branch time. The merge train may re-chain
    # this onto the final wave-1 head — update BOTH this assert and the
    # migration's down_revision together.
    assert mod.down_revision == "060_settings_suite"


# ---------------------------------------------------------------------------
# Clinic 9-role permission matrix (pure)
# ---------------------------------------------------------------------------




def test_role_keys_are_nine():
    assert len(cp.CLINIC_ROLE_KEYS) == 9
    assert cp.ADDON_ROLE_KEYS <= set(cp.CLINIC_ROLE_KEYS)


def test_validate_role_assignment_ok():
    assert cp.validate_role_assignment(["loan_origination"]) == []
    assert cp.validate_role_assignment(["loan_servicing", "assignment_officer"]) == []


def test_validate_role_assignment_empty():
    errors = cp.validate_role_assignment([])
    assert errors and "At least one role" in errors[0]


def test_validate_role_assignment_unknown():
    errors = cp.validate_role_assignment(["not_a_role"])
    assert any("Unknown role" in e for e in errors)


def test_validate_role_assignment_duplicate():
    errors = cp.validate_role_assignment(["export", "export"])
    assert any("Duplicate" in e for e in errors)


def test_validate_role_assignment_addon_only_rejected():
    # Add-on roles work ONLY in conjunction with another role (TL on-screen note).
    errors = cp.validate_role_assignment(["assignment_officer"])
    assert any("add-on" in e for e in errors)
    errors2 = cp.validate_role_assignment(["assignment_officer", "document_verification"])
    assert any("add-on" in e for e in errors2)


def test_has_clinic_permission_legacy_null_is_full_access():
    # NULL roles = legacy membership = full access (no breaking migration).
    assert cp.has_clinic_permission(None, "loan_origination") is True
    assert cp.has_clinic_permission(None, "export") is True


def test_has_clinic_permission_explicit():
    roles = ["loan_servicing", "monitoring"]
    assert cp.has_clinic_permission(roles, "loan_servicing") is True
    assert cp.has_clinic_permission(roles, "loan_origination") is False
    # Explicit empty list grants NOTHING (deliberate lockdown).
    assert cp.has_clinic_permission([], "loan_servicing") is False


def test_has_clinic_permission_unknown_permission_raises():
    with pytest.raises(ValueError):
        cp.has_clinic_permission(["export"], "nonexistent")


def test_require_clinic_permission_dependency():
    from fastapi import HTTPException

    from app.api.clinic.v1.deps import ClinicPrincipal

    dep = cp.require_clinic_permission("loan_origination")

    # Legacy principal (roles=None) passes.
    legacy = ClinicPrincipal(user=object(), vendor_id=_uuid(), role="staff", roles=None)
    assert dep(principal=legacy) is legacy

    # Principal without the role is 403.
    scoped = ClinicPrincipal(
        user=object(), vendor_id=_uuid(), role="staff", roles=("monitoring",)
    )
    with pytest.raises(HTTPException) as exc:
        dep(principal=scoped)
    assert exc.value.status_code == 403


def test_require_clinic_permission_bad_permission_fails_fast():
    with pytest.raises(ValueError):
        cp.require_clinic_permission("not_real")


# ---------------------------------------------------------------------------
# Bank-account masking (pure)
# ---------------------------------------------------------------------------




def test_account_last4():
    assert crm_v.account_last4("1234567890") == "7890"
    assert crm_v.account_last4("00 12-3456") == "3456"


def test_account_last4_too_short():
    with pytest.raises(ValueError):
        crm_v.account_last4("12")


def test_mask_account():
    assert crm_v.mask_account("7890") == "•••• 7890"


# ---------------------------------------------------------------------------
# Onboarding forward-only math (pure)
# ---------------------------------------------------------------------------


def test_next_onboarding_statuses():
    assert crm_v.next_onboarding_statuses("invited") == [
        "docs_collected",
        "msa_signed",
        "live",
    ]
    assert crm_v.next_onboarding_statuses("msa_signed") == ["live"]
    assert crm_v.next_onboarding_statuses("live") == []
    assert crm_v.next_onboarding_statuses("bogus") == []


# ---------------------------------------------------------------------------
# Vendor document expiry-alert thresholds (pure)
# ---------------------------------------------------------------------------




def test_due_thresholds_far_out_none():
    # 90 days left, no threshold hit yet.
    assert vda.due_thresholds(date(2026, 12, 31), date(2026, 10, 2)) == []


def test_due_thresholds_first_60():
    from datetime import timedelta

    expiry = date(2026, 3, 1)
    # Exactly 60 days out → the 60-day alert; 61 days out → nothing yet.
    assert vda.due_thresholds(expiry, expiry - timedelta(days=60)) == [60]
    assert vda.due_thresholds(expiry, expiry - timedelta(days=61)) == []


def test_due_thresholds_single_most_urgent():
    # First seen 5 days out with nothing sent → one 7-day alert (not 60+30+7).
    assert vda.due_thresholds(date(2026, 1, 10), date(2026, 1, 5), already_sent=[]) == [7]


def test_due_thresholds_skips_already_sent():
    # 30-day window, 60 already sent → 30 fires.
    assert vda.due_thresholds(date(2026, 2, 1), date(2026, 1, 5), already_sent=[60]) == [30]
    # 30 already sent, still in 30 window → nothing.
    assert vda.due_thresholds(date(2026, 2, 1), date(2026, 1, 5), already_sent=[60, 30]) == []


def test_due_thresholds_expired():
    # Past expiry, nothing sent → the 0-day "expired" alert.
    assert vda.due_thresholds(date(2026, 1, 1), date(2026, 1, 10), already_sent=[60, 30, 7]) == [0]
    # 0 already sent → nothing more.
    assert vda.due_thresholds(date(2026, 1, 1), date(2026, 1, 10), already_sent=[0]) == []


def test_render_alert_expiring_vs_expired():
    subj, html = vda.render_alert(
        vendor_name="KDC", title="MSA 2026", doc_type="msa",
        expiry_date=date(2026, 3, 1), threshold=30,
    )
    assert "30 days" in subj and "MSA" in subj and "KDC" in html
    subj2, _ = vda.render_alert(
        vendor_name="KDC", title="MSA 2026", doc_type="msa",
        expiry_date=date(2026, 1, 1), threshold=0,
    )
    assert "EXPIRED" in subj2


# ---------------------------------------------------------------------------
# Customer block/unblock over a FAKE session
# ---------------------------------------------------------------------------




class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Minimal Session stand-in: records adds, serves canned query results."""

    def __init__(self, *, active=None, reason_active=True):
        self.added = []
        self._active_block = active
        self._reason_active = reason_active

    def query(self, model):
        from app.models.platform.crm import (
            PlatformCustomerBlock,
            PlatformCustomerBlockReason,
        )

        if model is PlatformCustomerBlock:
            return _FakeQuery([self._active_block] if self._active_block else [])
        if model is PlatformCustomerBlockReason:
            reason = object() if self._reason_active else None
            return _FakeQuery([reason] if reason else [])
        return _FakeQuery([])

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass


def test_ensure_not_blocked_allows_when_clear():
    db = _FakeSession(active=None)
    cb.ensure_not_blocked(db, _uuid())  # no raise


def test_ensure_not_blocked_raises_when_blocked():
    db = _FakeSession(active=object())
    with pytest.raises(cb.CustomerBlockedError):
        cb.ensure_not_blocked(db, _uuid())


def test_block_patient_requires_reason_text():
    db = _FakeSession(active=None)
    with pytest.raises(cb.CustomerBlockError):
        cb.block_patient(
            db, patient_id=_uuid(), reason_code="other", reason_text="   ", actor_id="admin1"
        )


def test_block_patient_unknown_reason_code():
    db = _FakeSession(active=None, reason_active=False)
    with pytest.raises(cb.CustomerBlockError):
        cb.block_patient(
            db, patient_id=_uuid(), reason_code="bogus", reason_text="bad actor", actor_id="a"
        )


def test_block_patient_double_block_rejected():
    db = _FakeSession(active=object())
    with pytest.raises(cb.CustomerBlockError):
        cb.block_patient(
            db, patient_id=_uuid(), reason_code="other", reason_text="x", actor_id="a"
        )


def test_block_patient_happy_path_writes_row_and_event():
    from app.models.platform.crm import PlatformCustomerBlock
    from app.models.platform.event import PlatformEvent

    db = _FakeSession(active=None)
    pid = _uuid()
    row = cb.block_patient(
        db, patient_id=pid, reason_code="suspected_fraud",
        reason_text="chargebacks", actor_id="admin7",
    )
    assert isinstance(row, PlatformCustomerBlock)
    assert row.reason_text == "chargebacks"
    assert row.blocked_by == "admin7"
    # A block row AND an audit event were added.
    assert any(isinstance(o, PlatformCustomerBlock) for o in db.added)
    assert any(
        isinstance(o, PlatformEvent) and o.event_type == cb.BLOCKED_EVENT
        for o in db.added
    )


def test_unblock_patient_closes_row():
    from app.models.platform.crm import PlatformCustomerBlock
    from app.models.platform.event import PlatformEvent

    existing = PlatformCustomerBlock(
        patient_id=_uuid(), reason_code="other", reason_text="x", blocked_by="a"
    )
    db = _FakeSession(active=existing)
    row = cb.unblock_patient(db, patient_id=existing.patient_id, actor_id="admin9", note="cleared")
    assert row.unblocked_at is not None
    assert row.unblocked_by == "admin9"
    assert row.unblock_note == "cleared"
    assert any(
        isinstance(o, PlatformEvent) and o.event_type == cb.UNBLOCKED_EVENT
        for o in db.added
    )


def test_unblock_patient_not_blocked_raises():
    db = _FakeSession(active=None)
    with pytest.raises(cb.CustomerBlockError):
        cb.unblock_patient(db, patient_id=_uuid(), actor_id="a")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _uuid():
    import uuid

    return uuid.uuid4()
