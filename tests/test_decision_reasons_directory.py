"""WS-E reason-directory validation + decision-notice wording flow — pure (no DB).

Covers:
* slug/field validation used by the admin CRUD endpoint;
* the default seed data (app.services.decision_reasons — the single source of
  truth consumed by BOTH migration 048 and the test-DB fixture in conftest):
  every seed passes validation, codes are unique per kind, and the seeded
  borrower-facing wording for engine codes is consistent with the
  notice of decision's built-in wording (so the directory taking over the
  wording changes nothing until an admin edits it);
* migration 048 chains correctly and delegates to the shared seeder;
* directory borrower_facing_text overriding the notice of decision content;
* the cancellation email template rendering.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.services import decision_notice
from app.services.decision_reasons import (
    CANCEL_REASON_SEEDS,
    CODE_RE,
    REJECT_REASON_SEEDS,
    validate_reason_fields,
)
from app.services.flow_engine import (
    REASON_ACTIVE_BANKRUPTCY,
    REASON_BANKRUPTCY_DISCHARGE_RECENT,
    REASON_BUREAU_BELOW_MINIMUM,
    REASON_FRAUD_SIGNAL_REVIEW,
    REASON_IDENTITY_MANUAL_REVIEW,
    REASON_QUEBEC,
)

# ---------------------------------------------------------------------------
# Load migration 048 by path (alembic/versions is not importable as a package)
# ---------------------------------------------------------------------------

_MIGRATION = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "048_underwriting_ops.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_048", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MIG = _load_migration()


# ---------------------------------------------------------------------------
# Field validation (used by the admin CRUD endpoint)
# ---------------------------------------------------------------------------


def test_valid_fields_pass():
    validate_reason_fields(
        kind="reject", code="my_reason_2", internal_label="Label",
        borrower_facing_text="Text.",
    )


@pytest.mark.parametrize(
    "kind,code,label,text",
    [
        ("nope", "ok_code", "L", "T"),          # bad kind
        ("reject", "Bad Code", "L", "T"),       # spaces / capitals
        ("reject", "1starts_with_digit", "L", "T"),
        ("reject", "", "L", "T"),               # empty code
        ("reject", "has-dash", "L", "T"),       # dash not allowed
        ("cancel", "ok_code", "  ", "T"),       # blank label
        ("cancel", "ok_code", "L", ""),         # blank borrower text
        ("reject", "x" * 100, "L", "T"),        # too long
    ],
)
def test_invalid_fields_raise(kind, code, label, text):
    with pytest.raises(ValueError):
        validate_reason_fields(
            kind=kind, code=code, internal_label=label, borrower_facing_text=text
        )


# ---------------------------------------------------------------------------
# Default seed integrity (single source: app.services.decision_reasons)
# ---------------------------------------------------------------------------


def test_seed_rows_all_pass_validation():
    for kind, seeds in (("reject", REJECT_REASON_SEEDS), ("cancel", CANCEL_REASON_SEEDS)):
        for code, label, text in seeds:
            validate_reason_fields(
                kind=kind, code=code, internal_label=label, borrower_facing_text=text
            )
            assert CODE_RE.match(code), code


def test_seed_codes_unique_per_kind():
    for seeds in (REJECT_REASON_SEEDS, CANCEL_REASON_SEEDS):
        codes = [c for c, _, _ in seeds]
        assert len(codes) == len(set(codes))


def test_engine_decline_codes_are_seeded_as_reject_reasons():
    """Every stable engine code that can accompany a DECLINE has a directory row,
    so its decision-notice wording is admin-editable."""
    seeded = {c for c, _, _ in REJECT_REASON_SEEDS}
    for engine_code in (
        REASON_BUREAU_BELOW_MINIMUM,
        REASON_ACTIVE_BANKRUPTCY,
        REASON_BANKRUPTCY_DISCHARGE_RECENT,
        REASON_FRAUD_SIGNAL_REVIEW,
        REASON_IDENTITY_MANUAL_REVIEW,
        REASON_QUEBEC,
    ):
        assert engine_code in seeded, engine_code


def test_seed_wording_matches_decision_notice_builtin_wording():
    """The seeds start out IDENTICAL to the notice's built-in fallback wording —
    applying migration 048 changes no applicant-facing text until an admin edits
    the directory."""
    for code, _, text in REJECT_REASON_SEEDS:
        builtin = decision_notice._REASON_TEXT.get(code)
        if builtin is not None:
            assert text == builtin, code


def test_cancel_seeds_cover_turnkey_directory():
    seeded = {c for c, _, _ in CANCEL_REASON_SEEDS}
    assert {
        "customer_request", "duplicate_application", "vendor_request",
        "offer_expired", "bank_verification_expired", "other",
    } == seeded


def test_migration_chain():
    assert MIG.revision == "048_underwriting_ops"
    assert MIG.down_revision == "047_import_batches"


def test_migration_delegates_to_shared_seeder():
    """Migration 048 and the test-DB fixture must share ONE seeder — the
    conftest TRUNCATE wipes migration-inserted rows before every test, so a
    divergent copy in either place would silently drift."""
    source = _MIGRATION.read_text()
    assert "from app.services.decision_reasons import seed_defaults" in source
    assert "seed_defaults(op.get_bind())" in source


# ---------------------------------------------------------------------------
# Directory wording flows into the notice of decision
# ---------------------------------------------------------------------------


def test_humanize_reasons_prefers_directory_override():
    out = decision_notice.humanize_reasons(
        ["bureau_below_minimum", "some_new_staff_code"],
        {"bureau_below_minimum": "Directory-edited wording.",
         "some_new_staff_code": "Wording for the new staff reason."},
    )
    assert "Directory-edited wording." in out
    assert "Wording for the new staff reason." in out


def test_humanize_reasons_falls_back_without_override():
    out = decision_notice.humanize_reasons(["bureau_below_minimum"], {})
    assert out == ["Your credit score did not meet our minimum requirement."]
    # unknown code with no override → safe generic line, never a KeyError
    out2 = decision_notice.humanize_reasons(["mystery_code"], None)
    assert out2 == ["Your application did not meet our current lending criteria."]


def test_notice_content_and_render_carry_override_text():
    content = decision_notice.build_notice_content(
        applicant_name="Jordan Lee",
        application_id="app-123",
        reasons=["bankruptcy_discharge_recent"],
        reason_texts={"bankruptcy_discharge_recent": "Custom vetted wording."},
    )
    assert content["principal_reasons"] == ["Custom vetted wording."]
    html = decision_notice.render_notice_html(content)
    text = decision_notice.render_notice_text(content)
    assert "Custom vetted wording." in html
    assert "Custom vetted wording." in text


def test_new_bankruptcy_code_has_builtin_notice_wording():
    out = decision_notice.humanize_reasons([REASON_BANKRUPTCY_DISCHARGE_RECENT])
    assert out != ["Your application did not meet our current lending criteria."]
    assert "discharged" in out[0]


# ---------------------------------------------------------------------------
# Cancellation notification rendering (non-credit closure)
# ---------------------------------------------------------------------------


def test_application_cancelled_email_renders():
    from app.services.notification_render import render_email

    subject, html = render_email(
        "application_cancelled",
        {
            "borrower_name": "Jordan Lee",
            "application_id": "app-123",
            "cancel_reason_text": "Your application was cancelled at your request.",
        },
    )
    assert "cancelled" in subject.lower()
    assert "Jordan Lee" in html
    assert "Your application was cancelled at your request." in html
    # NON-CREDIT closure: the cancellation notice must never read as an
    # credit-decision notice.
    assert "Notice of Action Taken" not in html
    assert "not a credit decision" in html
