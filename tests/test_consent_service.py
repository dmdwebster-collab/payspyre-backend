"""Tests for the P5 consent service.

Two layers, matching the service split:

- ``TestConsentTextLoader`` — pure filesystem; **no database**. These are the
  verbatim merge-gate evidence runnable anywhere
  (``python -m pytest tests/test_consent_service.py::TestConsentTextLoader -v``).
- ``TestConsentService`` — DB-backed (record/revoke/query/immutability/no-PII);
  uses the ``db_session`` fixture and runs against the live Supabase pooler as the
  merge gate (same as the other platform service tests).
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.services.consent_service import (
    VALID_PURPOSES,
    ConsentService,
    ConsentTextLoader,
)

ALL_PURPOSES = sorted(VALID_PURPOSES)


# --------------------------------------------------------------------------- #
# Pure loader tests — no DB                                                    #
# --------------------------------------------------------------------------- #
class TestConsentTextLoader:
    def test_nine_purposes(self):
        assert len(VALID_PURPOSES) == 9

    def test_active_version_for_every_purpose(self):
        loader = ConsentTextLoader()
        for purpose in ALL_PURPOSES:
            assert loader.active_version(purpose) == "v1_2026-05"

    def test_text_nonempty_for_every_active_purpose(self):
        loader = ConsentTextLoader()
        for purpose in ALL_PURPOSES:
            text = loader.text(purpose)
            assert isinstance(text, str) and len(text) > 50

    def test_text_explicit_version_matches_active(self):
        loader = ConsentTextLoader()
        assert loader.text("id_verification", "v1_2026-05") == loader.text("id_verification")

    def test_version_tag_format(self):
        loader = ConsentTextLoader()
        assert loader.version_tag("id_verification") == "id_verification/v1_2026-05"

    def test_text_is_cached(self):
        loader = ConsentTextLoader()
        first = loader.text("bank_verification")
        second = loader.text("bank_verification")
        assert first == second
        assert ("bank_verification", "v1_2026-05") in loader._text_cache

    def test_invalid_purpose_raises(self):
        loader = ConsentTextLoader()
        for call in (
            lambda: loader.active_version("not_a_purpose"),
            lambda: loader.text("not_a_purpose"),
            lambda: loader.version_tag("not_a_purpose"),
        ):
            with pytest.raises(ValueError, match="Unknown consent purpose"):
                call()

    def test_marketing_email_is_optional_and_not_prechecked(self):
        """Compliance check: CASL marketing copy must state it's optional and never pre-checked."""
        text = ConsentTextLoader().text("marketing_email").lower()
        assert "optional" in text
        assert "never pre-checked" in text or "never pre checked" in text

    def test_aggregate_data_use_marked_disabled_at_launch(self):
        """Spec §8.1: aggregate_data_use record is built but not surfaced at launch."""
        text = ConsentTextLoader().text("aggregate_data_use").lower()
        assert "not active at launch" in text or "disabled at launch" in text

    # --- error paths against a synthetic config dir (still no DB) --- #
    def test_missing_active_yaml_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="active config not found"):
            ConsentTextLoader(config_dir=tmp_path).active_version("id_verification")

    def test_active_yaml_missing_a_purpose_raises(self, tmp_path: Path):
        (tmp_path / "active.yaml").write_text(
            "active:\n  id_verification: v1_2026-05\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="missing active versions"):
            ConsentTextLoader(config_dir=tmp_path).active_version("id_verification")

    def test_missing_text_file_raises(self, tmp_path: Path):
        lines = ["active:"] + [f"  {p}: v9_9999-99" for p in ALL_PURPOSES]
        (tmp_path / "active.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(FileNotFoundError, match="Consent text not found"):
            ConsentTextLoader(config_dir=tmp_path).text("id_verification")

    def test_empty_text_file_raises(self, tmp_path: Path):
        lines = ["active:"] + [f"  {p}: v1_2026-05" for p in ALL_PURPOSES]
        (tmp_path / "active.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (tmp_path / "id_verification").mkdir()
        (tmp_path / "id_verification" / "v1_2026-05.md").write_text("   \n", encoding="utf-8")
        with pytest.raises(ValueError, match="empty"):
            ConsentTextLoader(config_dir=tmp_path).text("id_verification")


# --------------------------------------------------------------------------- #
# DB-backed service tests — require db_session (Supabase pooler merge gate)     #
# --------------------------------------------------------------------------- #
@pytest.fixture
def patient(db_session):
    from app.models.platform.patient import PlatformPatient

    p = PlatformPatient(
        legal_first_name="Consent",
        legal_last_name="Tester",
        email=f"consent-{uuid4().hex[:10]}@example.test",
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def application(db_session, patient):
    """A real credit application off the seeded dental_full_arch_v1 product, so the
    application_id FK is satisfied against the migrated DB."""
    from app.models.platform.credit_product import PlatformCreditProduct
    from app.models.platform.credit_application import PlatformCreditApplication

    product = (
        db_session.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )
    assert product is not None, "seed product dental_full_arch_v1 missing (migration 022)"
    app = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=product.id,
        credit_product_version=product.version,
        requested_amount_cents=2_000_000,
        requested_amount_source="patient",
    )
    db_session.add(app)
    db_session.commit()
    db_session.refresh(app)
    return app


def _latest_event(db_session, patient_id, event_type):
    from app.models.platform.event import PlatformEvent

    return (
        db_session.query(PlatformEvent)
        .filter(PlatformEvent.patient_id == patient_id, PlatformEvent.event_type == event_type)
        .order_by(PlatformEvent.id.desc())
        .first()
    )


class TestConsentService:
    def test_record_consent_captures_text_and_version(self, db_session, patient):
        svc = ConsentService(db_session)
        c = svc.record_consent(patient.id, "id_verification", True, ip_address="203.0.113.7")
        assert c.id is not None
        assert c.consent_granted is True
        assert c.consent_text_version == "id_verification/v1_2026-05"
        assert c.consent_text_shown == ConsentTextLoader().text("id_verification")
        assert c.revoked_at is None

    def test_record_consent_logs_event_without_pii(self, db_session, patient):
        svc = ConsentService(db_session)
        svc.record_consent(
            patient.id,
            "bank_verification",
            True,
            ip_address="203.0.113.9",
            user_agent="Mozilla/5.0 SENTINEL-UA",
        )
        ev = _latest_event(db_session, patient.id, "consent.granted")
        assert ev is not None
        payload = ev.payload
        assert payload["purpose"] == "bank_verification"
        assert payload["consent_text_version"] == "bank_verification/v1_2026-05"
        assert payload["granted"] is True
        # No PII / no full text in the WORM event payload.
        blob = str(payload)
        assert "203.0.113.9" not in blob
        assert "SENTINEL-UA" not in blob
        assert "consent_text_shown" not in payload
        assert "I consent" not in blob

    def test_record_denial_is_not_active(self, db_session, patient):
        svc = ConsentService(db_session)
        svc.record_consent(patient.id, "marketing_email", False)
        assert svc.has_active_consent(patient.id, "marketing_email") is False
        ev = _latest_event(db_session, patient.id, "consent.denied")
        assert ev is not None and ev.payload["granted"] is False

    def test_has_active_consent_true_after_grant(self, db_session, patient):
        svc = ConsentService(db_session)
        svc.record_consent(patient.id, "soft_bureau_pull", True)
        assert svc.has_active_consent(patient.id, "soft_bureau_pull") is True

    def test_revoke_sets_revoked_at_and_deactivates(self, db_session, patient):
        svc = ConsentService(db_session)
        c = svc.record_consent(patient.id, "hard_bureau_pull", True)
        revoked = svc.revoke_consent(c.id)
        assert revoked.revoked_at is not None
        assert svc.has_active_consent(patient.id, "hard_bureau_pull") is False

    def test_revoke_preserves_text_and_version(self, db_session, patient):
        svc = ConsentService(db_session)
        c = svc.record_consent(patient.id, "automated_decision_making", True)
        original_text, original_version = c.consent_text_shown, c.consent_text_version
        revoked = svc.revoke_consent(c.id)
        assert revoked.consent_text_shown == original_text
        assert revoked.consent_text_version == original_version

    def test_immutability_guard_blocks_text_update(self, db_session, patient):
        """§2.6 hard rule — consent_text_shown can never be UPDATEd."""
        svc = ConsentService(db_session)
        c = svc.record_consent(patient.id, "id_verification", True)
        c.consent_text_shown = "TAMPERED TEXT"
        with pytest.raises(ValueError, match="immutable"):
            db_session.commit()
        db_session.rollback()

    def test_immutability_guard_blocks_version_update(self, db_session, patient):
        svc = ConsentService(db_session)
        c = svc.record_consent(patient.id, "id_verification", True)
        c.consent_text_version = "id_verification/v99_2099-99"
        with pytest.raises(ValueError, match="immutable"):
            db_session.commit()
        db_session.rollback()

    def test_revoke_nonexistent_raises(self, db_session):
        svc = ConsentService(db_session)
        with pytest.raises(ValueError, match="not found"):
            svc.revoke_consent(uuid4())

    def test_double_revoke_raises(self, db_session, patient):
        svc = ConsentService(db_session)
        c = svc.record_consent(patient.id, "marketplace_listing", True)
        svc.revoke_consent(c.id)
        with pytest.raises(ValueError, match="already revoked"):
            svc.revoke_consent(c.id)

    def test_get_consents_for_patient_excludes_revoked_by_default(self, db_session, patient):
        svc = ConsentService(db_session)
        keep = svc.record_consent(patient.id, "id_verification", True)
        gone = svc.record_consent(patient.id, "marketing_email", True)
        svc.revoke_consent(gone.id)
        active = svc.get_consents_for_patient(patient.id)
        active_ids = {c.id for c in active}
        assert keep.id in active_ids
        assert gone.id not in active_ids
        all_rows = svc.get_consents_for_patient(patient.id, include_revoked=True)
        assert gone.id in {c.id for c in all_rows}

    def test_get_consents_for_application(self, db_session, patient, application):
        svc = ConsentService(db_session)
        c = svc.record_consent(patient.id, "id_verification", True, application_id=application.id)
        rows = svc.get_consents_for_application(application.id)
        assert c.id in {r.id for r in rows}
