"""Integration tests for the P6 flow orchestrator (live Supabase, sync).

Covers create/consent/initiate/result/submit, idempotency, consent gating,
the run_flow decision paths (decline/manual-review/approve), and the
application-row lock under concurrent webhook delivery.
"""
import threading
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

import app.services.consent_service as consent_service
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.event import PlatformEvent
from app.models.platform.patient import PlatformPatient
from app.services.consent_service import UnknownConsentPurposeError
from app.services.flow_orchestrator import (
    ConsentMissingError,
    DuplicateVerificationError,
    FlowOrchestrator,
    InvalidStateTransition,
    StillPendingError,
)
from app.services.verifications.mock_dispatcher import MockVerificationDispatcher

_REQUIRED_PURPOSES = ["id_verification", "soft_bureau_pull", "bank_verification", "hard_bureau_pull"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _orch(db: Session) -> FlowOrchestrator:
    return FlowOrchestrator(db, consent_service, MockVerificationDispatcher())


def _make_patient(db: Session) -> PlatformPatient:
    patient = PlatformPatient(email=f"p6-{uuid.uuid4().hex[:8]}@example.com")
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient


def _seed_product_id(db: Session) -> uuid.UUID:
    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )
    assert product is not None, "seed product missing"
    return product.id


def _create_app(orch: FlowOrchestrator, patient_id, product_id) -> PlatformCreditApplication:
    return orch.create_application(
        patient_id=patient_id,
        credit_product_id=product_id,
        requested_amount_cents=2_500_000,
        requested_amount_source="clinic",
    )


def _grant(orch: FlowOrchestrator, app_id, purpose):
    return orch.record_consent_grant(app_id, purpose, ip_address="203.0.113.5", user_agent="pytest")


def _rich(dispatcher, vtype, score=720):
    overrides = {"credit_score": score} if vtype in ("bureau_soft", "bureau_hard") else None
    return dispatcher.simulate_callback(vtype, result="passed", rich_payload=overrides)["rich_payload"]


def _drive_to_decision(db, orch, *, score, purposes=_REQUIRED_PURPOSES):
    """Create app, grant consents, initiate + complete verifications. Returns the app id."""
    patient = _make_patient(db)
    app = _create_app(orch, patient.id, _seed_product_id(db))
    for p in purposes:
        _grant(orch, app.id, p)
    # automated_decision_making consent is required before _decide (finding #4).
    # It is a decision-gate consent, not tied to a verification, so it is granted
    # here rather than in the verification-purposes loop above.
    _grant(orch, app.id, "automated_decision_making")
    dispatcher = orch.dispatcher
    verifs = {p: orch.initiate_verification(app.id, p) for p in purposes}
    for p, verif in verifs.items():
        orch.handle_verification_result(
            app.id,
            verif.id,
            vendor_event_id=f"evt-{verif.id}",
            result="passed",
            rich_payload=_rich(dispatcher, verif.verification_type, score=score),
        )
    return app.id


# ---------------------------------------------------------------------------
# _decision_product_view — finding #6 (pure, no DB)
# ---------------------------------------------------------------------------


class TestDecisionProductView:
    """Finding #6 / Hard Rule #7-8: the decision runs against the matrix
    snapshotted onto the application at creation, NOT the live product row."""

    def test_uses_snapshot_when_present(self):
        from types import SimpleNamespace

        live = SimpleNamespace(
            id="prod-1", version=2, verification_matrix={"bureau": {"min_score": 999}}
        )
        app = SimpleNamespace(
            id="app-1",
            credit_product_id="prod-1",
            credit_product_version=1,
            product_config_snapshot={"bureau": {"min_score": 660}},
        )
        view = FlowOrchestrator._decision_product_view(app, live)
        # The snapshot wins — a live edit to 999 must NOT reach the decision.
        assert view.verification_matrix == {"bureau": {"min_score": 660}}
        assert view.version == 1

    def test_falls_back_to_live_when_snapshot_missing(self):
        from types import SimpleNamespace

        live = SimpleNamespace(
            id="prod-1", version=2, verification_matrix={"bureau": {"min_score": 660}}
        )
        app = SimpleNamespace(
            id="app-1",
            credit_product_id="prod-1",
            credit_product_version=1,
            product_config_snapshot=None,  # legacy row, pre-migration-026
        )
        view = FlowOrchestrator._decision_product_view(app, live)
        assert view is live


# ---------------------------------------------------------------------------
# create_application
# ---------------------------------------------------------------------------


class TestCreateApplication:
    def test_snapshots_version_emits_event_status_started(self, db_session: Session):
        orch = _orch(db_session)
        patient = _make_patient(db_session)
        product = (
            db_session.query(PlatformCreditProduct)
            .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
            .first()
        )
        app = _create_app(orch, patient.id, product.id)
        assert app.id is not None
        assert app.status == "started"
        assert app.credit_product_version == product.version

        ev = db_session.execute(
            text(
                "SELECT id FROM platform_events WHERE event_type='application_created' "
                "AND application_id=:aid"
            ),
            {"aid": str(app.id)},
        ).first()
        assert ev is not None

    def test_invalid_amount_source_raises_and_writes_no_row(self, db_session: Session):
        orch = _orch(db_session)
        patient = _make_patient(db_session)
        product_id = _seed_product_id(db_session)
        with pytest.raises((DataError, IntegrityError, ProgrammingError)):
            orch.create_application(
                patient_id=patient.id,
                credit_product_id=product_id,
                requested_amount_cents=2_500_000,  # in-bounds: isolate the invalid source
                requested_amount_source="bogus_source",  # not a valid enum
            )
        db_session.rollback()
        count = db_session.query(PlatformCreditApplication).filter(
            PlatformCreditApplication.patient_id == patient.id
        ).count()
        assert count == 0


# ---------------------------------------------------------------------------
# get_required_consents
# ---------------------------------------------------------------------------


class TestGetRequiredConsents:
    def test_returns_matrix_derived_purposes_for_seed_product(self, db_session: Session):
        orch = _orch(db_session)
        patient = _make_patient(db_session)
        app = _create_app(orch, patient.id, _seed_product_id(db_session))
        required = orch.get_required_consents(app.id)
        assert set(required) == set(_REQUIRED_PURPOSES)


# ---------------------------------------------------------------------------
# record_consent_grant
# ---------------------------------------------------------------------------


class TestRecordConsentGrant:
    def test_succeeds_for_known_purpose(self, db_session: Session):
        orch = _orch(db_session)
        patient = _make_patient(db_session)
        app = _create_app(orch, patient.id, _seed_product_id(db_session))
        consent = _grant(orch, app.id, "id_verification")
        assert consent.id is not None
        assert consent.purpose == "id_verification"
        assert consent.consent_granted is True

        ev = db_session.execute(
            text(
                "SELECT id FROM platform_events WHERE event_type='consent_granted' "
                "AND application_id=:aid"
            ),
            {"aid": str(app.id)},
        ).first()
        assert ev is not None

    def test_unknown_purpose_fails_clean(self, db_session: Session):
        orch = _orch(db_session)
        patient = _make_patient(db_session)
        app = _create_app(orch, patient.id, _seed_product_id(db_session))
        with pytest.raises(UnknownConsentPurposeError):
            _grant(orch, app.id, "not_a_real_purpose")


# ---------------------------------------------------------------------------
# initiate_verification
# ---------------------------------------------------------------------------


class TestInitiateVerification:
    def test_raises_consent_missing_when_no_consent(self, db_session: Session):
        orch = _orch(db_session)
        patient = _make_patient(db_session)
        app = _create_app(orch, patient.id, _seed_product_id(db_session))
        with pytest.raises(ConsentMissingError):
            orch.initiate_verification(app.id, "id_verification")

    def test_raises_consent_missing_when_revoked(self, db_session: Session):
        orch = _orch(db_session)
        patient = _make_patient(db_session)
        app = _create_app(orch, patient.id, _seed_product_id(db_session))
        consent = _grant(orch, app.id, "id_verification")
        consent_service.revoke_consent(db_session, consent.id)
        with pytest.raises(ConsentMissingError):
            orch.initiate_verification(app.id, "id_verification")

    def test_succeeds_maps_enum_emits_event_sets_verifying(self, db_session: Session):
        orch = _orch(db_session)
        patient = _make_patient(db_session)
        app = _create_app(orch, patient.id, _seed_product_id(db_session))
        _grant(orch, app.id, "id_verification")
        verif = orch.initiate_verification(app.id, "id_verification")
        assert verif.verification_type == "kyc_id"  # mapped
        assert verif.status == "pending"
        assert verif.consent_id is not None

        db_session.refresh(app)
        assert app.status == "verifying"

        ev = db_session.execute(
            text(
                "SELECT id FROM platform_events WHERE event_type='verification_initiated' "
                "AND application_id=:aid"
            ),
            {"aid": str(app.id)},
        ).first()
        assert ev is not None

    def test_raises_on_duplicate_pending_verification(self, db_session: Session):
        orch = _orch(db_session)
        patient = _make_patient(db_session)
        app = _create_app(orch, patient.id, _seed_product_id(db_session))
        _grant(orch, app.id, "id_verification")
        orch.initiate_verification(app.id, "id_verification")
        with pytest.raises(DuplicateVerificationError):
            orch.initiate_verification(app.id, "id_verification")


# ---------------------------------------------------------------------------
# handle_verification_result
# ---------------------------------------------------------------------------


class TestHandleVerificationResult:
    def test_idempotent_on_repeated_vendor_event_id(self, db_session: Session):
        orch = _orch(db_session)
        patient = _make_patient(db_session)
        app = _create_app(orch, patient.id, _seed_product_id(db_session))
        _grant(orch, app.id, "id_verification")
        verif = orch.initiate_verification(app.id, "id_verification")

        def _count_events():
            return db_session.execute(
                text(
                    "SELECT count(*) FROM platform_events "
                    "WHERE event_type='verification_completed' AND application_id=:aid"
                ),
                {"aid": str(app.id)},
            ).scalar()

        rp = _rich(orch.dispatcher, "kyc_id")
        first = orch.handle_verification_result(app.id, verif.id, "evt-dup", "passed", rp)
        assert first.idempotent_replay is False
        after_first = _count_events()

        second = orch.handle_verification_result(app.id, verif.id, "evt-dup", "passed", rp)
        assert second.idempotent_replay is True
        assert _count_events() == after_first  # no extra event

    def test_does_not_decide_while_verifications_pending(self, db_session: Session):
        orch = _orch(db_session)
        patient = _make_patient(db_session)
        app = _create_app(orch, patient.id, _seed_product_id(db_session))
        for p in _REQUIRED_PURPOSES:
            _grant(orch, app.id, p)
        verifs = {p: orch.initiate_verification(app.id, p) for p in _REQUIRED_PURPOSES}

        # Complete only one of four.
        v = verifs["id_verification"]
        res = orch.handle_verification_result(
            app.id, v.id, f"evt-{v.id}", "passed", _rich(orch.dispatcher, v.verification_type)
        )
        assert res.decided is False
        db_session.refresh(app)
        assert app.status == "verifying"

    def test_decides_when_all_verifications_terminal(self, db_session: Session):
        orch = _orch(db_session)
        app_id = _drive_to_decision(db_session, orch, score=720)
        app = db_session.get(PlatformCreditApplication, app_id)
        db_session.refresh(app)
        assert app.status == "approved"
        ev = db_session.execute(
            text(
                "SELECT id FROM platform_events WHERE event_type='decision_made' "
                "AND application_id=:aid"
            ),
            {"aid": str(app_id)},
        ).first()
        assert ev is not None


# ---------------------------------------------------------------------------
# Decision paths (run_flow thresholds: <600 declined, 600-679 review, >=680 approved)
# ---------------------------------------------------------------------------


class TestDecisionPaths:
    def test_decline_below_floor(self, db_session: Session):
        orch = _orch(db_session)
        # All 4 required verifications complete at score 550. run_flow declines at
        # the soft pull (<600) and never calls hard_pull; the stored hard result is
        # simply unused.
        app_id = _drive_to_decision(db_session, orch, score=550)
        app = db_session.get(PlatformCreditApplication, app_id)
        db_session.refresh(app)
        assert app.status == "declined"

    def test_manual_review_band(self, db_session: Session):
        orch = _orch(db_session)
        app_id = _drive_to_decision(db_session, orch, score=640)
        app = db_session.get(PlatformCreditApplication, app_id)
        db_session.refresh(app)
        # run_flow's manual_review maps to the under_review status enum value.
        assert app.status == "under_review"
        assert app.decision["decision"] == "manual_review"

    def test_approve_above_band(self, db_session: Session):
        orch = _orch(db_session)
        app_id = _drive_to_decision(db_session, orch, score=720)
        app = db_session.get(PlatformCreditApplication, app_id)
        db_session.refresh(app)
        assert app.status == "approved"

    def test_didit_in_review_end_to_end_decides_under_review(self, db_session: Session):
        """P7.6 — Didit "In Review" landing as ``result="manual_review"`` on
        the identity verification pulls the application to ``under_review``
        even with an otherwise-clean bureau score, and surfaces
        ``identity_manual_review`` in the decision reasons.

        Walks the full pipeline: create app + grants + initiate all four
        verifications, then complete kyc_id with ``result="manual_review"``
        (the P7.5 vendor-webhook landing shape) and the other three with
        the existing clean-720 path. The orchestrator's ``_decide`` runs
        once everything is terminal; the engine's identity manual_review
        branch (added in P7.6) routes through ``_resolve_decision`` to
        ``decision = "manual_review"`` → ``next_state = "under_review"``.
        """
        orch = _orch(db_session)
        patient = _make_patient(db_session)
        app = _create_app(orch, patient.id, _seed_product_id(db_session))
        for p in _REQUIRED_PURPOSES:
            _grant(orch, app.id, p)
        _grant(orch, app.id, "automated_decision_making")  # decision-gate consent (finding #4)
        verifs = {p: orch.initiate_verification(app.id, p) for p in _REQUIRED_PURPOSES}
        for p, verif in verifs.items():
            if verif.verification_type == "kyc_id":
                # P7.5 webhook landing shape: result="manual_review" with the
                # rich_payload Didit's translator produces.
                orch.handle_verification_result(
                    app.id, verif.id,
                    vendor_event_id=f"evt-{verif.id}",
                    result="manual_review",
                    rich_payload={
                        "method": "document",
                        "result": "manual_review",
                        "confidence": 0.95,
                        "vendor": "didit",
                        "vendor_session_ref": f"didit-sess-{verif.id}",
                        "didit_status": "In Review",
                    },
                )
            else:
                orch.handle_verification_result(
                    app.id, verif.id,
                    vendor_event_id=f"evt-{verif.id}",
                    result="passed",
                    rich_payload=_rich(orch.dispatcher, verif.verification_type, score=720),
                )
        db_session.refresh(app)
        assert app.status == "under_review"
        assert app.decision is not None
        assert app.decision["decision"] == "manual_review"
        assert "identity_manual_review" in app.decision["decision_reasons"]


# ---------------------------------------------------------------------------
# submit_for_decision
# ---------------------------------------------------------------------------


class TestSubmitForDecision:
    def test_idempotent_when_already_decided(self, db_session: Session):
        orch = _orch(db_session)
        app_id = _drive_to_decision(db_session, orch, score=720)
        result = orch.submit_for_decision(app_id)
        assert result.already_decided is True
        assert result.status == "approved"

    def test_raises_still_pending_when_verifications_open(self, db_session: Session):
        orch = _orch(db_session)
        patient = _make_patient(db_session)
        app = _create_app(orch, patient.id, _seed_product_id(db_session))
        _grant(orch, app.id, "id_verification")
        orch.initiate_verification(app.id, "id_verification")  # left pending
        with pytest.raises(StillPendingError):
            orch.submit_for_decision(app.id)


# ---------------------------------------------------------------------------
# Concurrency: application row lock under racing webhooks
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_final_results_produce_one_decision(self, db_session: Session):
        """Two webhooks for the last two verifications arrive in parallel. The
        with_for_update lock must serialize them so exactly one decision_made
        event is written and no error is raised."""
        from tests.conftest import TestingSessionLocal

        orch = _orch(db_session)
        patient = _make_patient(db_session)
        app = _create_app(orch, patient.id, _seed_product_id(db_session))
        for p in _REQUIRED_PURPOSES:
            _grant(orch, app.id, p)
        _grant(orch, app.id, "automated_decision_making")  # decision-gate consent (finding #4)
        verifs = {p: orch.initiate_verification(app.id, p) for p in _REQUIRED_PURPOSES}

        # Complete two of four up front, leaving two to race.
        for p in ("id_verification", "bank_verification"):
            v = verifs[p]
            orch.handle_verification_result(
                app.id, v.id, f"evt-{v.id}", "passed",
                _rich(orch.dispatcher, v.verification_type, score=720),
            )
        db_session.commit()

        racing = [verifs["soft_bureau_pull"], verifs["hard_bureau_pull"]]
        barrier = threading.Barrier(len(racing))
        errors: list[Exception] = []

        def worker(verif):
            sess = TestingSessionLocal()
            try:
                w_orch = FlowOrchestrator(sess, consent_service, MockVerificationDispatcher())
                barrier.wait(timeout=10)
                w_orch.handle_verification_result(
                    app.id, verif.id, f"evt-{verif.id}", "passed",
                    _rich(MockVerificationDispatcher(), verif.verification_type, score=720),
                )
            except Exception as exc:  # noqa: BLE001 — recorded and asserted below
                errors.append(exc)
            finally:
                sess.close()

        threads = [threading.Thread(target=worker, args=(v,)) for v in racing]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"concurrent handlers raised: {errors}"

        decision_events = db_session.execute(
            text(
                "SELECT count(*) FROM platform_events WHERE event_type='decision_made' "
                "AND application_id=:aid"
            ),
            {"aid": str(app.id)},
        ).scalar()
        assert decision_events == 1  # exactly one decision despite the race
