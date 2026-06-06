"""Tests for the Consent Service (PR P5).

Two layers, matching the service's two responsibilities:

- **Pure loader/cache tests** (class ``TestConsentTextLoader``) — no DB. These
  read the real ``config/consent_text/`` tree (and a temp tree for the
  missing-file case). Run them standalone with::

      python -m pytest tests/test_consent_service.py -v -k TestConsentTextLoader

- **DB-backed tests** — run against the live Supabase Session Pooler via the
  ``db_session`` fixture (TEST_DATABASE_URL). No SQLite, no mocks.

The WORM suite proves immutability of ``consent_text_shown`` /
``consent_text_version`` (Hard Rule #1) at BOTH layers: the application layer
(``revoke_consent`` only ever sets ``revoked_at``) and, as of migration 025, a
column-specific DB-level trigger (``platform_consents_text_immutable``) that
blocks any UPDATE changing those columns while still allowing ``revoked_at``.
"""
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import (
    IntegrityError,
    InternalError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.orm import Session

import app.services.consent_service as consent_service
from app.models.platform.consent import PlatformConsent
from app.models.platform.patient import PlatformPatient
from app.services.consent_service import (
    ConsentText,
    ConsentVersionNotFoundError,
    UnknownConsentPurposeError,
    get_active_consent_text,
    get_active_consents_for_patient,
    record_consent,
    revoke_consent,
)

# The five launch-required purposes that ship with v1 placeholder text.
_LAUNCH_PURPOSES = [
    "id_verification",
    "soft_bureau_pull",
    "bank_verification",
    "hard_bureau_pull",
    "automated_decision_making",
]
_ACTIVE_VERSION = "v1_2026-05"


@pytest.fixture(autouse=True)
def _reset_consent_cache():
    """Guarantee a clean in-process cache around every test (the cache is a
    module-level global and survives across tests otherwise)."""
    consent_service._reset_cache()
    yield
    consent_service._reset_cache()


def _make_patient(db: Session) -> PlatformPatient:
    """Insert a minimal patient to satisfy the consent FK and return it."""
    patient = PlatformPatient(email=f"consent-test-{uuid.uuid4().hex[:8]}@example.com")
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient


# ---------------------------------------------------------------------------
# Pure loader / cache tests (no DB)
# ---------------------------------------------------------------------------


class TestConsentTextLoader:
    @pytest.mark.parametrize("purpose", _LAUNCH_PURPOSES)
    def test_returns_correct_version_and_text(self, purpose):
        ct = get_active_consent_text(purpose)
        assert isinstance(ct, ConsentText)
        assert ct.purpose == purpose
        assert ct.version == _ACTIVE_VERSION
        assert ct.text  # non-empty
        # The verbatim header block is part of the stored text (design dec. #3).
        assert ct.text.startswith("<!-- LEGAL-REVIEW-REQUIRED")
        assert f"<!-- Purpose: {purpose} -->" in ct.text
        assert f"<!-- Version: {_ACTIVE_VERSION} -->" in ct.text
        # loaded_at is tz-aware UTC.
        assert ct.loaded_at.tzinfo is not None
        assert ct.loaded_at.utcoffset().total_seconds() == 0

    def test_unknown_purpose_raises_clear_error(self):
        # 'marketing_email' is a valid enum value but is opt-in/deferred (spec
        # §8.1) — it has no entry in active.json, so the loader must reject it.
        with pytest.raises(UnknownConsentPurposeError, match="marketing_email"):
            get_active_consent_text("marketing_email")

    def test_missing_file_raises_clear_error(self, tmp_path, monkeypatch):
        cfg_dir = tmp_path / "consent_text"
        (cfg_dir / "id_verification").mkdir(parents=True)
        (cfg_dir / "active.json").write_text(
            json.dumps({"id_verification": "v99_missing"}), encoding="utf-8"
        )
        monkeypatch.setattr(consent_service, "_CONSENT_TEXT_DIR", cfg_dir)
        consent_service._reset_cache()

        with pytest.raises(ConsentVersionNotFoundError, match="v99_missing"):
            get_active_consent_text("id_verification")

    def test_cache_returns_same_instance(self):
        first = get_active_consent_text("id_verification")
        second = get_active_consent_text("id_verification")
        assert first is second  # identity => served from cache, not re-built

    def test_cache_avoids_filesystem_reread(self, monkeypatch):
        # Populate the cache (this performs the only allowed reads), then assert
        # subsequent calls never touch the filesystem again.
        first = get_active_consent_text("soft_bureau_pull")

        reads = {"n": 0}
        real_read_text = Path.read_text

        def counting_read_text(self, *args, **kwargs):
            reads["n"] += 1
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", counting_read_text)
        second = get_active_consent_text("soft_bureau_pull")

        assert first is second
        assert reads["n"] == 0  # cache hit => zero filesystem reads


# ---------------------------------------------------------------------------
# DB-backed: record_consent
# ---------------------------------------------------------------------------


class TestRecordConsent:
    def test_inserts_row_with_verbatim_text_and_version(self, db_session: Session):
        patient = _make_patient(db_session)
        consent = record_consent(
            db_session, patient.id, "id_verification", granted=True
        )

        expected = get_active_consent_text("id_verification")
        assert consent.id is not None
        assert consent.patient_id == patient.id
        assert consent.purpose == "id_verification"
        assert consent.consent_granted is True
        assert consent.consent_text_version == expected.version == _ACTIVE_VERSION
        # Verbatim, byte-for-byte (Hard Rule #5).
        assert consent.consent_text_shown == expected.text
        # The legal-review marker is intentionally embedded in the stored record.
        assert "LEGAL-REVIEW-REQUIRED" in consent.consent_text_shown

    def test_writes_granted_at_leaves_revoked_at_null(self, db_session: Session):
        patient = _make_patient(db_session)
        consent = record_consent(
            db_session, patient.id, "soft_bureau_pull", granted=True
        )
        assert consent.granted_at is not None
        assert consent.revoked_at is None

    def test_persists_optional_audit_fields(self, db_session: Session):
        patient = _make_patient(db_session)
        consent = record_consent(
            db_session,
            patient.id,
            "bank_verification",
            granted=True,
            ip_address="203.0.113.7",
            user_agent="pytest-agent/1.0",
        )
        fetched = (
            db_session.query(PlatformConsent)
            .filter(PlatformConsent.id == consent.id)
            .first()
        )
        assert str(fetched.ip_address) == "203.0.113.7"
        assert fetched.user_agent == "pytest-agent/1.0"

    def test_records_a_decline(self, db_session: Session):
        patient = _make_patient(db_session)
        consent = record_consent(
            db_session, patient.id, "hard_bureau_pull", granted=False
        )
        assert consent.consent_granted is False
        # The declined decision is still a permanent record with the text shown.
        assert "LEGAL-REVIEW-REQUIRED" in consent.consent_text_shown

    def test_unknown_purpose_does_not_write_a_row(self, db_session: Session):
        patient = _make_patient(db_session)
        with pytest.raises(UnknownConsentPurposeError):
            record_consent(db_session, patient.id, "marketing_email", granted=True)

        rows = (
            db_session.query(PlatformConsent)
            .filter(PlatformConsent.patient_id == patient.id)
            .all()
        )
        assert rows == []


# ---------------------------------------------------------------------------
# DB-backed: revoke_consent
# ---------------------------------------------------------------------------


class TestRevokeConsent:
    def test_sets_revoked_at(self, db_session: Session):
        patient = _make_patient(db_session)
        consent = record_consent(
            db_session, patient.id, "id_verification", granted=True
        )
        assert consent.revoked_at is None

        revoked = revoke_consent(db_session, consent.id)
        assert revoked.id == consent.id
        assert revoked.revoked_at is not None

    def test_revoke_missing_raises(self, db_session: Session):
        with pytest.raises(ValueError, match="not found"):
            revoke_consent(db_session, uuid.uuid4())


# ---------------------------------------------------------------------------
# DB-backed: get_active_consents_for_patient
# ---------------------------------------------------------------------------


class TestGetActiveConsentsForPatient:
    def test_excludes_revoked_rows(self, db_session: Session):
        patient = _make_patient(db_session)
        c1 = record_consent(db_session, patient.id, "id_verification", granted=True)
        c2 = record_consent(db_session, patient.id, "soft_bureau_pull", granted=True)
        revoke_consent(db_session, c1.id)

        active_ids = {c.id for c in get_active_consents_for_patient(db_session, patient.id)}
        assert c2.id in active_ids
        assert c1.id not in active_ids

    def test_ordered_by_granted_at_desc(self, db_session: Session):
        patient = _make_patient(db_session)
        record_consent(db_session, patient.id, "id_verification", granted=True)
        record_consent(db_session, patient.id, "soft_bureau_pull", granted=True)
        record_consent(db_session, patient.id, "bank_verification", granted=True)

        active = get_active_consents_for_patient(db_session, patient.id)
        granted_times = [c.granted_at for c in active]
        assert granted_times == sorted(granted_times, reverse=True)

    def test_scoped_to_the_requested_patient(self, db_session: Session):
        patient_a = _make_patient(db_session)
        patient_b = _make_patient(db_session)
        a_consent = record_consent(
            db_session, patient_a.id, "id_verification", granted=True
        )
        record_consent(db_session, patient_b.id, "id_verification", granted=True)

        a_active = get_active_consents_for_patient(db_session, patient_a.id)
        assert [c.id for c in a_active] == [a_consent.id]


# ---------------------------------------------------------------------------
# DB-backed: WORM enforcement (Hard Rule #1)
# ---------------------------------------------------------------------------


class TestWormEnforcement:
    """``consent_text_shown`` / ``consent_text_version`` are never UPDATEd.

    Enforced at two layers: the application layer (``revoke_consent`` only ever
    sets ``revoked_at``) and a column-specific DB-level trigger
    (``platform_consents_text_immutable``, migration 025) that blocks any UPDATE
    changing the text columns while leaving ``revoked_at`` updatable. The backlog
    item (payspyre_backlog.md, 2026-05-25) is closed by migration 025.
    """

    def test_revoke_never_modifies_text_columns(self, db_session: Session):
        patient = _make_patient(db_session)
        consent = record_consent(
            db_session, patient.id, "hard_bureau_pull", granted=True
        )
        original_text = consent.consent_text_shown
        original_version = consent.consent_text_version

        revoke_consent(db_session, consent.id)

        db_session.expire_all()
        fetched = (
            db_session.query(PlatformConsent)
            .filter(PlatformConsent.id == consent.id)
            .first()
        )
        assert fetched.consent_text_shown == original_text
        assert fetched.consent_text_version == original_version
        assert fetched.revoked_at is not None  # revocation still recorded

    def test_db_trigger_blocks_text_column_update(self, db_session: Session):
        """Migration 025: a raw UPDATE to consent_text_shown is blocked by the
        DB-level trigger (not just the application layer)."""
        patient = _make_patient(db_session)
        consent = record_consent(
            db_session, patient.id, "automated_decision_making", granted=True
        )

        with pytest.raises(
            (IntegrityError, InternalError, ProgrammingError, OperationalError)
        ) as exc_info:
            db_session.execute(
                text(
                    "UPDATE platform_consents SET consent_text_shown = :t WHERE id = :id"
                ),
                {"t": "TAMPERED", "id": str(consent.id)},
            )
            db_session.commit()
        db_session.rollback()

        msg = str(exc_info.value).lower()
        assert "immutable consent" in msg or "worm" in msg, (
            f"Expected the WORM trigger message, got: {exc_info.value}"
        )

    def test_db_trigger_blocks_version_column_update(self, db_session: Session):
        """The trigger also blocks tampering with consent_text_version."""
        patient = _make_patient(db_session)
        consent = record_consent(db_session, patient.id, "id_verification", granted=True)

        with pytest.raises(
            (IntegrityError, InternalError, ProgrammingError, OperationalError)
        ):
            db_session.execute(
                text(
                    "UPDATE platform_consents SET consent_text_version = :v WHERE id = :id"
                ),
                {"v": "tampered/v9_2099-99", "id": str(consent.id)},
            )
            db_session.commit()
        db_session.rollback()

    def test_db_trigger_present_in_pg_catalog(self, db_session: Session):
        """Belt-and-suspenders: confirm the trigger is registered (migration 025
        not silently dropped by a later migration)."""
        result = db_session.execute(
            text(
                """
                SELECT tgname
                FROM pg_trigger
                WHERE tgname = 'platform_consents_text_immutable'
                  AND tgrelid = 'platform_consents'::regclass
                  AND NOT tgisinternal
                """
            )
        ).scalar()
        assert result == "platform_consents_text_immutable", (
            "WORM trigger missing from pg_catalog — migration 025 may have been altered"
        )
