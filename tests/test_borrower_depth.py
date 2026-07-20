"""WS-J borrower-portal depth — DB-FREE tests.

Deliberately touches NO database (the shared remote test DB doesn't have
migration 064 yet, and the suite must not be run wholesale): everything here
exercises pure helpers, in-memory model objects, JWT round-trips, and router
surface introspection.

Covers:
  * TOTP correctness against the RFC-6238 reference vectors + drift window;
  * step-up token issue/validate (purpose + patient binding);
  * step-up dependency logic (enrolled / enforced / neither) with a stubbed
    2FA state;
  * status-banner stage mapping;
  * initial-vs-current schedule shaping + closed toggle + next-payment math;
  * payout date-window validation + the never-suspends disclaimer;
  * re-origination prefill field carry-over (SIN never carried);
  * bank-account masking;
  * SURFACE GUARANTEES: the borrower routers expose NO bank-account add or
    delete route and NO ID-document download/read route (write-only enforced
    at the endpoint layer).

Run just this file:
    source .venv/bin/activate && python -m pytest tests/test_borrower_depth.py -q
"""
from __future__ import annotations

import base64
import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from jose import jwt

from app.core.config import settings
from app.services import borrower_portal, borrower_security

# ---------------------------------------------------------------------------
# TOTP (RFC 6238 vectors: SHA-1, secret "12345678901234567890")
# ---------------------------------------------------------------------------

_RFC_SECRET_B32 = base64.b32encode(b"12345678901234567890").decode()

# (unix time, expected 8-digit TOTP) from RFC 6238 Appendix B.
_RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
]


@pytest.mark.parametrize("at,expected", _RFC_VECTORS)
def test_totp_rfc6238_vectors(at, expected):
    assert borrower_security.totp_at(_RFC_SECRET_B32, at, digits=8) == expected


def test_totp_verify_accepts_current_and_adjacent_periods():
    secret = borrower_security.generate_totp_secret()
    now = 1_700_000_000
    current = borrower_security.totp_at(secret, now)
    previous = borrower_security.totp_at(secret, now - 30)
    next_ = borrower_security.totp_at(secret, now + 30)
    assert borrower_security.verify_totp(secret, current, at_unix=now)
    assert borrower_security.verify_totp(secret, previous, at_unix=now)
    assert borrower_security.verify_totp(secret, next_, at_unix=now)


def test_totp_verify_rejects_wrong_and_stale_codes():
    secret = borrower_security.generate_totp_secret()
    now = 1_700_000_000
    stale = borrower_security.totp_at(secret, now - 120)  # 4 periods back
    # A stale code only counts as a rejection if it isn't (by coincidence) the
    # current/adjacent value — otherwise the assertion is vacuous.
    live_values = {
        borrower_security.totp_at(secret, now + drift * 30) for drift in (-1, 0, 1)
    }
    if stale not in live_values:
        assert not borrower_security.verify_totp(secret, stale, at_unix=now)


def test_totp_provisioning_uri_contains_secret_and_issuer():
    uri = borrower_security.totp_provisioning_uri("ABC234", "patient-1")
    assert uri.startswith("otpauth://totp/PaySpyre:patient-1?")
    assert "secret=ABC234" in uri and "issuer=PaySpyre" in uri


# ---------------------------------------------------------------------------
# Step-up tokens
# ---------------------------------------------------------------------------


def test_step_up_token_roundtrip_and_patient_binding():
    pid = uuid.uuid4()
    other = uuid.uuid4()
    token, expires_at = borrower_security.issue_step_up_token(pid)
    assert borrower_security.validate_step_up_token(token, pid)
    # Bound to THIS patient — replay against another session fails.
    assert not borrower_security.validate_step_up_token(token, other)
    assert expires_at.timestamp() > 0


def test_patient_session_jwt_is_not_a_step_up_token():
    """A 24h session JWT must NEVER double as step-up proof (purpose claim)."""
    pid = uuid.uuid4()
    session_like = jwt.encode(
        {"sub": str(pid), "app_ids": [], "iat": 0, "exp": 9999999999},
        settings.PATIENT_JWT_SECRET,
        algorithm="HS256",
    )
    assert not borrower_security.validate_step_up_token(session_like, pid)


def test_garbage_step_up_token_rejected():
    assert not borrower_security.validate_step_up_token("not-a-jwt", uuid.uuid4())


# ---------------------------------------------------------------------------
# Step-up dependency logic (stubbed 2FA state — no DB)
# ---------------------------------------------------------------------------


def _run_step_up(state, patient_id, token=None):
    """Drive deps.require_step_up with a stubbed TwoFactorService.get_state."""
    from unittest.mock import patch

    from fastapi import HTTPException

    from app.api.applicant.v1 import deps

    claims = deps.ApplicantClaims(patient_id=patient_id, app_ids=[])
    with patch.object(
        borrower_security.TwoFactorService, "get_state", return_value=state
    ):
        try:
            return deps.require_step_up(claims=claims, x_step_up_token=token, db=None), None
        except HTTPException as exc:
            return None, exc


def test_step_up_passthrough_when_not_enrolled():
    pid = uuid.uuid4()
    result, err = _run_step_up(None, pid)
    assert err is None and result.patient_id == pid


def test_step_up_required_when_active_and_missing_token():
    pid = uuid.uuid4()
    state = SimpleNamespace(status="active", enforced=False)
    _, err = _run_step_up(state, pid)
    assert err is not None and err.status_code == 401
    assert err.detail["code"] == "step_up_required"


def test_step_up_accepts_valid_token_when_active():
    pid = uuid.uuid4()
    token, _ = borrower_security.issue_step_up_token(pid)
    state = SimpleNamespace(status="active", enforced=False)
    result, err = _run_step_up(state, pid, token=token)
    assert err is None and result.patient_id == pid


def test_step_up_rejects_other_patients_token():
    pid = uuid.uuid4()
    token, _ = borrower_security.issue_step_up_token(uuid.uuid4())
    state = SimpleNamespace(status="active", enforced=False)
    _, err = _run_step_up(state, pid, token=token)
    assert err is not None and err.status_code == 401


def test_step_up_enforced_but_unenrolled_blocks_with_403():
    pid = uuid.uuid4()
    state = SimpleNamespace(status="pending", enforced=True)
    _, err = _run_step_up(state, pid)
    assert err is not None and err.status_code == 403
    assert err.detail["code"] == "step_up_enrollment_required"


def test_step_up_pending_unenforced_passes_through():
    pid = uuid.uuid4()
    state = SimpleNamespace(status="pending", enforced=False)
    result, err = _run_step_up(state, pid)
    assert err is None and result.patient_id == pid


# ---------------------------------------------------------------------------
# Status banner
# ---------------------------------------------------------------------------


def test_banner_under_review_states_share_the_submitted_message():
    for status_ in ("under_review", "underwriting", "pre_qualified", "awaiting_hard_pull"):
        banner = borrower_portal.banner_for(status_, None)
        assert banner["stage"] == "under_review"
        assert "submitted for approval" in banner["message"]


def test_banner_loan_status_wins_over_application_status():
    banner = borrower_portal.banner_for("approved", "active")
    assert banner["stage"] == "active"
    assert "timely payments" in banner["message"]


def test_banner_lifecycle_stages():
    assert borrower_portal.banner_for("started", None)["stage"] == "in_progress"
    assert borrower_portal.banner_for("declined", None)["stage"] == "declined"
    assert borrower_portal.banner_for("approved", "pending_disbursement")["stage"] == "funding"
    assert borrower_portal.banner_for("approved", "paid_off")["stage"] == "paid_off"
    assert borrower_portal.banner_for("approved", "delinquent")["stage"] == "past_due"
    assert borrower_portal.banner_for(None, None)["stage"] == "welcome"
    # Unknown statuses degrade to the welcome banner, never KeyError.
    assert borrower_portal.banner_for("weird", "weirder")["stage"] == "welcome"


# ---------------------------------------------------------------------------
# Schedule views + next payment (in-memory schedule rows)
# ---------------------------------------------------------------------------


def _row(n, due, principal=10_000, interest=1_000, status="scheduled", paid=0):
    return SimpleNamespace(
        installment_number=n,
        due_date=due,
        principal_cents=principal,
        interest_cents=interest,
        total_cents=principal + interest,
        status=status,
        paid_cents=paid,
    )


_TODAY = date(2026, 7, 20)


def _schedule():
    return [
        _row(1, _TODAY - timedelta(days=60), status="paid", paid=11_000),
        _row(2, _TODAY - timedelta(days=30), status="partial", paid=5_000),
        _row(3, _TODAY + timedelta(days=2)),
        _row(4, _TODAY + timedelta(days=32), status="waived"),
    ]


def test_current_view_includes_paid_overlay_and_remaining():
    rows = borrower_portal.shape_schedule_rows(_schedule(), "current", include_closed=True)
    assert [r["installment_number"] for r in rows] == [1, 2, 3, 4]
    assert rows[0]["status"] == "paid" and rows[0]["remaining_cents"] == 0
    assert rows[1]["status"] == "partial" and rows[1]["remaining_cents"] == 6_000


def test_current_view_closed_toggle_hides_paid_and_waived():
    rows = borrower_portal.shape_schedule_rows(_schedule(), "current", include_closed=False)
    assert [r["installment_number"] for r in rows] == [2, 3]


def test_initial_view_shows_as_agreed_plan_without_payment_overlay():
    rows = borrower_portal.shape_schedule_rows(_schedule(), "initial", include_closed=False)
    # The initial plan never shrinks and carries no actuals.
    assert len(rows) == 4
    assert all(r["status"] == "scheduled" and r["paid_cents"] == 0 for r in rows)
    assert rows[0]["remaining_cents"] == rows[0]["total_cents"]


def test_next_payment_widget_earliest_open_row_and_countdown():
    widget = borrower_portal.next_payment_widget(_schedule(), _TODAY)
    assert widget["installment_number"] == 2  # earliest open (partial)
    assert widget["amount_cents"] == 6_000
    assert widget["overdue"] is True and widget["days_until"] == 0


def test_next_payment_widget_future_countdown_and_done_case():
    future_only = [_row(1, _TODAY + timedelta(days=5))]
    widget = borrower_portal.next_payment_widget(future_only, _TODAY)
    assert widget["days_until"] == 5 and widget["overdue"] is False
    all_closed = [_row(1, _TODAY, status="paid", paid=11_000)]
    assert borrower_portal.next_payment_widget(all_closed, _TODAY) is None


def test_next_payment_widget_skips_suspended_rows():
    rows = [
        _row(1, _TODAY - timedelta(days=3), status="suspended"),
        _row(2, _TODAY + timedelta(days=11)),
    ]
    widget = borrower_portal.next_payment_widget(rows, _TODAY)
    assert widget["installment_number"] == 2


# ---------------------------------------------------------------------------
# Payout requests
# ---------------------------------------------------------------------------


def test_payout_date_window():
    today = date(2026, 7, 20)
    assert borrower_portal.validate_payout_date(today, today) is None
    assert borrower_portal.validate_payout_date(today + timedelta(days=30), today) is None
    assert borrower_portal.validate_payout_date(today - timedelta(days=1), today) is not None
    assert borrower_portal.validate_payout_date(today + timedelta(days=31), today) is not None


def test_payout_disclaimer_states_payments_not_suspended():
    assert "does not suspend" in borrower_portal.PAYOUT_DISCLAIMER


# ---------------------------------------------------------------------------
# Re-origination prefill
# ---------------------------------------------------------------------------


def test_prefill_carries_canonical_fields_and_skips_none():
    src = SimpleNamespace(
        first_name="Raleigh",
        last_name="Bailey",
        email="raleigh@example.com",
        residence_city="Kelowna",
        employer_name="XYZ Corp",
        net_monthly_income_cents=550_000,
        middle_name=None,  # skipped
    )
    out = borrower_portal.prefill_from_application(src)
    assert out["first_name"] == "Raleigh"
    assert out["residence_city"] == "Kelowna"
    assert out["net_monthly_income_cents"] == 550_000
    assert "middle_name" not in out


def test_prefill_never_carries_sin():
    """The SIN lives ONLY encrypted on the patient — never copied via prefill."""
    sin_field_names = {"sin", "social_insurance_number", "sin_encrypted", "sin_last3"}
    assert not (set(borrower_portal.PREFILL_FIELDS) & sin_field_names)
    leaky = SimpleNamespace(sin_encrypted="secret", social_insurance_number="123")
    assert borrower_portal.prefill_from_application(leaky) == {}


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


def test_mask_identifier():
    assert borrower_portal.mask_identifier("1111000") == "••••1000"
    assert borrower_portal.mask_identifier("77777", keep=2) == "••••77"
    assert borrower_portal.mask_identifier(None) is None
    assert borrower_portal.mask_identifier("") is None
    # Short values are masked-prefixed, never length-revealed beyond themselves.
    assert borrower_portal.mask_identifier("42") == "••••42"


# ---------------------------------------------------------------------------
# Surface guarantees (router introspection — the lockdowns are structural)
# ---------------------------------------------------------------------------


def _routes(router):
    return [(sorted(r.methods), r.path) for r in router.routes]


def test_borrower_bank_router_has_no_add_or_delete():
    """Dave: borrowers cannot add or delete bank accounts. The borrower router
    must expose ONLY the list and the set-default routes."""
    from app.api.applicant.v1.endpoints import bank_accounts

    routes = _routes(bank_accounts.router)
    assert (["GET"], "/bank-accounts") in routes
    assert (["POST"], "/bank-accounts/{account_id}/default") in routes
    assert len(routes) == 2  # nothing else — no create, no delete
    assert all("DELETE" not in methods for methods, _ in routes)


def test_borrower_id_documents_are_write_only():
    """No borrower route may read/download an ID image: the profile router's
    id-document paths are upload-url, confirm, and a metadata list only."""
    from app.api.applicant.v1.endpoints import profile

    id_routes = [(m, p) for m, p in _routes(profile.router) if "id-documents" in p]
    assert (["POST"], "/profile/id-documents/upload-url") in id_routes
    assert (["POST"], "/profile/id-documents/{document_id}/confirm") in id_routes
    assert (["GET"], "/profile/id-documents") in id_routes
    assert len(id_routes) == 3
    for methods, path in id_routes:
        assert "download" not in path
    # The metadata response model exposes no object key and no URL field.
    fields = set(profile.IdDocMeta.model_fields)
    assert "object_key" not in fields
    assert not any("url" in f for f in fields)


def test_pay_now_and_sensitive_routes_are_step_up_gated():
    """The sensitive endpoints depend on require_step_up (additive 2FA gate)."""
    from app.api.applicant.v1.deps import require_step_up
    from app.api.applicant.v1.endpoints import bank_accounts, loans, profile

    def _uses_step_up(router, path, method):
        for r in router.routes:
            if r.path == path and method in r.methods:
                return any(
                    getattr(d, "call", None) is require_step_up
                    for d in r.dependant.dependencies
                )
        raise AssertionError(f"route {method} {path} not found")

    assert _uses_step_up(loans.router, "/loans/{loan_id}/payments", "POST")
    assert _uses_step_up(profile.router, "/profile", "PUT")
    assert _uses_step_up(bank_accounts.router, "/bank-accounts/{account_id}/default", "POST")


def test_admin_router_is_role_gated():
    from app.api.v1.endpoints import admin_borrower_security

    # The router-level dependency list is non-empty (require_roles admin/staff).
    assert admin_borrower_security.router.dependencies
