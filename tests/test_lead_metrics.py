"""Tests for marketplace lead-metric maintenance (patient lead_state + verification_depth)."""
import uuid

from app.services import lead_metrics
from app.models.platform.patient import PlatformPatient
from app.models.platform.verification import PlatformVerification


def test_derive_verification_depth():
    assert lead_metrics.derive_verification_depth(set()) == "none"
    assert lead_metrics.derive_verification_depth({"kyc_id"}) == "id_verified"
    assert lead_metrics.derive_verification_depth({"kyc_id", "bank_link"}) == "id_bank_verified"
    assert (
        lead_metrics.derive_verification_depth({"kyc_id", "bank_link", "bureau_soft"})
        == "id_bank_cb_verified"
    )
    # bank/bureau without id never elevates past 'none'
    assert lead_metrics.derive_verification_depth({"bank_link", "bureau_hard"}) == "none"


def _patient(db) -> PlatformPatient:
    p = PlatformPatient(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:8]}@example.com")
    db.add(p)
    db.flush()
    return p


def _pass(db, patient_id, vtype):
    db.add(
        PlatformVerification(
            id=uuid.uuid4(), patient_id=patient_id, verification_type=vtype, status="passed"
        )
    )
    db.flush()


def test_refresh_advances_forward(db_session):
    p = _patient(db_session)
    assert p.lead_state == "unqualified"
    assert p.verification_depth == "none"

    _pass(db_session, p.id, "kyc_id")
    lead_metrics.refresh_from_verifications(db_session, p)
    assert p.lead_state == "pre_qualified"
    assert p.verification_depth == "id_verified"

    _pass(db_session, p.id, "bank_link")
    _pass(db_session, p.id, "bureau_soft")
    lead_metrics.refresh_from_verifications(db_session, p)
    assert p.lead_state == "pre_approved"
    assert p.verification_depth == "id_bank_cb_verified"


def test_refresh_never_regresses(db_session):
    p = _patient(db_session)
    p.lead_state = "pre_approved"
    p.verification_depth = "id_bank_cb_verified"
    db_session.flush()
    # no passed verifications must NOT drop the snapshot back down
    lead_metrics.refresh_from_verifications(db_session, p)
    assert p.lead_state == "pre_approved"
    assert p.verification_depth == "id_bank_cb_verified"


def test_apply_decision_sets_terminal(db_session):
    p = _patient(db_session)
    _pass(db_session, p.id, "kyc_id")
    lead_metrics.apply_decision(db_session, p, "approved")
    assert p.lead_state == "approved"
    assert p.verification_depth == "id_verified"  # captured during the deciding flow

    p2 = _patient(db_session)
    # apply_decision takes the application STATUS ("rejected"); the marketplace
    # lead_state terminal value stays "declined" (separate enum, not renamed).
    lead_metrics.apply_decision(db_session, p2, "rejected")
    assert p2.lead_state == "declined"


def test_under_review_is_noop_for_lead_state(db_session):
    p = _patient(db_session)
    _pass(db_session, p.id, "kyc_id")
    lead_metrics.apply_decision(db_session, p, "under_review")
    # under_review is not terminal: lead_state stays on the forward ladder
    assert p.lead_state == "pre_qualified"


def test_terminal_preserved_against_later_refresh(db_session):
    p = _patient(db_session)
    lead_metrics.apply_decision(db_session, p, "approved")
    _pass(db_session, p.id, "kyc_id")
    lead_metrics.refresh_from_verifications(db_session, p)
    assert p.lead_state == "approved"  # terminal not knocked back to pre_qualified
