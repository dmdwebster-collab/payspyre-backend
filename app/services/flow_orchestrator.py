"""Flow orchestrator — PR P6 (stateful orchestration layer, spec §4.3/§4.4).

Wraps the pure P4 batch core ``flow_engine.run_flow()`` in DB transactions,
``platform_events`` emission, consent gating, and a mock verification
dispatcher. The orchestrator holds NO business/decision logic — every "if score
< X" branch lives in the pure core or in ``verification_matrix.bureau.*`` on the
product row (Hard Rule: no business logic here).

Design notes (see kickoff v3 §0):
- The shipped pure core is **batch**: ``run_flow()`` runs all verifications via
  adapters and returns a finished ``FlowDecision``. There is no incremental
  stepper. The orchestrator persists what ``run_flow`` returns.
- Verifications happen *before* the decision. Their rich vendor data lives only
  in the ``verification_completed`` event payload (``platform_verifications`` has
  no result/payload column). When every verification is terminal, the
  orchestrator builds **replay adapters** from those payloads and calls
  ``run_flow`` for the decision.
- ``run_flow`` is **authoritative** for decisioning. No external decision-rule
  file is loaded (that idea was dropped in kickoff v3).

Transaction convention: this file follows the repo's established
``db.commit()``-at-end unit-of-work (as in ``credit_products.py`` /
``consent_service.py``) rather than a literal ``with db.begin():`` — SQLAlchemy
2.0 autobegin (the test fixtures call ``db.refresh()``, which opens a
transaction) makes ``self.db.begin()`` raise "a transaction is already begun".
The semantics are identical: one transaction per public method, all-or-nothing,
no SAVEPOINTs. ``_in_external_txn=True`` skips the commit so a caller's
transaction owns the boundary.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Literal, Optional, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.platform.consent import PlatformConsent
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.event import PlatformEvent
from app.models.platform.patient import PlatformPatient
from app.models.platform.verification import PlatformVerification
from app.services.adapters.base import FlowAdapters, PatientProfile
from app.services.flow_engine import FlowDecision, run_flow
from app.services.verifications.mock_dispatcher import MockVerificationDispatcher
from app.services.verifications.replay_adapters import (
    ReplayBankAdapter,
    ReplayBureauAdapter,
    ReplayVerificationAdapter,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors (orchestrator → HTTP status mapping happens in P6.5 endpoints)
# ---------------------------------------------------------------------------


class OrchestratorError(Exception):
    """Base class for orchestrator errors."""


class ApplicationNotFoundError(OrchestratorError):
    """The application id does not exist."""


class ConsentMissingError(OrchestratorError):
    """No non-revoked, granted consent exists for the required purpose (→ 422)."""


class UnknownVerificationType(OrchestratorError):
    """A verification purpose has no mapping to a platform_verification_type (→ 400)."""


class InvalidStateTransition(OrchestratorError):
    """The application's current status forbids the requested action (→ 409)."""


class DuplicateVerificationError(OrchestratorError):
    """A pending/in_progress verification of this type already exists (→ 409)."""


class StillPendingError(OrchestratorError):
    """submit_for_decision called while verifications are still pending (→ 409)."""

    def __init__(self, pending: list[str]) -> None:
        self.pending = pending
        super().__init__(f"Verifications still pending: {pending}")


# ---------------------------------------------------------------------------
# Result value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HandleResult:
    verification_id: UUID
    application_status: str
    decided: bool                # True when this call produced/returned a decision
    idempotent_replay: bool      # True when the vendor_event_id was already processed
    decision: Optional[dict] = None


@dataclass(frozen=True)
class SubmitResult:
    application_id: UUID
    status: str
    decision: Optional[dict]
    already_decided: bool


# ---------------------------------------------------------------------------
# Injected-dependency protocol
# ---------------------------------------------------------------------------


class ConsentServiceProtocol(Protocol):
    def record_consent(
        self,
        db: Session,
        patient_id: UUID,
        purpose: str,
        granted: bool,
        application_id: UUID | None = ...,
        ip_address: str | None = ...,
        user_agent: str | None = ...,
    ) -> PlatformConsent: ...

    def get_active_consents_for_patient(
        self, db: Session, patient_id: UUID
    ) -> list[PlatformConsent]: ...


# ---------------------------------------------------------------------------
# Mappings / constants
# ---------------------------------------------------------------------------

# consent purpose (external API) → platform_verification_type enum value (DB)
CONSENT_TO_VERIFICATION_TYPE: dict[str, str] = {
    "id_verification": "kyc_id",
    "bank_verification": "bank_link",
    "soft_bureau_pull": "bureau_soft",
    "hard_bureau_pull": "bureau_hard",
}

# Deterministic ordering for get_required_consents output.
_CONSENT_ORDER = ["id_verification", "soft_bureau_pull", "bank_verification", "hard_bureau_pull"]

_PENDING_STATUSES = ("pending", "in_progress")
# P7.5: ``manual_review`` is terminal — a Didit "In Review" payload lands
# verification.status = "manual_review" and stops the per-verification flow.
# The orchestrator's ``_ready_to_decide`` treats it as terminal (not in
# ``_PENDING_STATUSES``); whether ``_decide()`` then runs depends on whether
# every required vtype has a row outside ``_PENDING_STATUSES``.
_TERMINAL_RESULT_TO_STATUS = {
    "passed": "passed",
    "failed": "failed",
    "manual_review": "manual_review",
}
_PRE_DECISION_STATUSES = ("started", "verifying")
_DECISION_STATUSES = ("approved", "declined", "under_review")


class FlowOrchestrator:
    def __init__(
        self,
        db: Session,
        consent_service: ConsentServiceProtocol,
        verification_dispatcher: MockVerificationDispatcher,
    ) -> None:
        self.db = db
        self.consent_service = consent_service
        self.dispatcher = verification_dispatcher

    # -- transaction helper -------------------------------------------------

    @contextmanager
    def _unit_of_work(self, in_external_txn: bool) -> Iterator[None]:
        """One transaction per public method, all-or-nothing. No SAVEPOINTs."""
        if in_external_txn:
            yield
            return
        try:
            yield
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    # -- loaders ------------------------------------------------------------

    def _get_application(self, application_id: UUID, *, lock: bool = False) -> PlatformCreditApplication:
        query = self.db.query(PlatformCreditApplication).filter(
            PlatformCreditApplication.id == application_id
        )
        if lock:
            query = query.with_for_update()
        app = query.first()
        if app is None:
            raise ApplicationNotFoundError(f"Application {application_id} not found")
        return app

    def _get_product(self, product_id: UUID) -> PlatformCreditProduct:
        product = (
            self.db.query(PlatformCreditProduct)
            .filter(PlatformCreditProduct.id == product_id)
            .first()
        )
        if product is None:
            raise OrchestratorError(f"Credit product {product_id} not found")
        return product

    # -- event emission -----------------------------------------------------

    def _emit_event(
        self,
        *,
        event_type: str,
        actor_type: str,
        actor_id: str,
        application: PlatformCreditApplication,
        before: dict | None = None,
        after: dict | None = None,
        vendor_event_id: str | None = None,
        verification_type: str | None = None,
        rich_payload: dict | None = None,
        metadata: dict | None = None,
    ) -> PlatformEvent:
        """Append one platform_events row in the §6 payload shape (no PII)."""
        payload: dict[str, Any] = {
            "v": 1,
            "actor": {"type": actor_type, "id": actor_id},
            "application_id": str(application.id),
            "patient_id": str(application.patient_id),
            "before": before or {},
            "after": after or {},
        }
        if vendor_event_id is not None:
            payload["vendor_event_id"] = vendor_event_id
        if verification_type is not None:
            payload["verification_type"] = verification_type
        if rich_payload is not None:
            payload["rich_payload"] = rich_payload
        if metadata is not None:
            payload["metadata"] = metadata

        event = PlatformEvent(
            event_type=event_type,
            actor=actor_type,
            patient_id=application.patient_id,
            application_id=application.id,
            payload=payload,
        )
        self.db.add(event)
        return event

    # -- public API ---------------------------------------------------------

    def create_application(
        self,
        patient_id: UUID,
        credit_product_id: UUID,
        requested_amount_cents: int,
        requested_amount_source: Literal["clinic", "patient", "clinic_then_patient_adjusted"],
        clinic_proposed_amount_cents: int | None = None,
        patient_proposed_amount_cents: int | None = None,
        vendor_id: UUID | None = None,
        treatment_plan_ref: str | None = None,
        _in_external_txn: bool = False,
    ) -> PlatformCreditApplication:
        """Create an application, snapshot the product version, emit application_created."""
        product = self._get_product(credit_product_id)
        with self._unit_of_work(_in_external_txn):
            application = PlatformCreditApplication(
                patient_id=patient_id,
                credit_product_id=credit_product_id,
                credit_product_version=product.version,  # snapshot (Hard Rule)
                requested_amount_cents=requested_amount_cents,
                requested_amount_source=requested_amount_source,
                clinic_proposed_amount_cents=clinic_proposed_amount_cents,
                patient_proposed_amount_cents=patient_proposed_amount_cents,
                vendor_id=vendor_id,
                treatment_plan_ref=treatment_plan_ref,
                status="started",
            )
            self.db.add(application)
            self.db.flush()  # assign id before the event references it
            self._emit_event(
                event_type="application_created",
                actor_type="patient",
                actor_id=str(patient_id),
                application=application,
                after={"status": "started", "credit_product_version": product.version},
            )
        self.db.refresh(application)
        logger.info(
            "application_created",
            application_id=str(application.id),
            patient_id=str(patient_id),
            credit_product_version=product.version,
        )
        return application

    def get_required_consents(self, application_id: UUID) -> list[str]:
        """Return the consent purposes required before any verification can start.

        Read-only. Derived from the product's verification_matrix:
        - identity.required               → id_verification
        - income with 'bank_link' method  → bank_verification
        - bureau.soft_pull_required       → soft_bureau_pull
        - bureau.hard_pull_required       → hard_bureau_pull
        """
        application = self._get_application(application_id)
        product = self._get_product(application.credit_product_id)
        required = self._required_purposes_from_matrix(product.verification_matrix)
        return [p for p in _CONSENT_ORDER if p in required]

    @staticmethod
    def _required_purposes_from_matrix(matrix: Any) -> set[str]:
        matrix = matrix if isinstance(matrix, dict) else {}
        required: set[str] = set()
        identity = matrix.get("identity") or {}
        if identity.get("required"):
            required.add("id_verification")
        income = matrix.get("income") or {}
        if "bank_link" in (income.get("methods") or []):
            required.add("bank_verification")
        bureau = matrix.get("bureau") or {}
        if bureau.get("soft_pull_required"):
            required.add("soft_bureau_pull")
        if bureau.get("hard_pull_required"):
            required.add("hard_bureau_pull")
        return required

    def record_consent_grant(
        self,
        application_id: UUID,
        purpose: str,
        ip_address: str,
        user_agent: str,
        _in_external_txn: bool = False,
    ) -> PlatformConsent:
        """Record a consent grant (P5 service) + emit consent_granted in one txn."""
        application = self._get_application(application_id)
        with self._unit_of_work(_in_external_txn):
            consent = self.consent_service.record_consent(
                self.db,
                patient_id=application.patient_id,
                purpose=purpose,
                granted=True,
                application_id=application.id,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            self._emit_event(
                event_type="consent_granted",
                actor_type="patient",
                actor_id=str(application.patient_id),
                application=application,
                after={"purpose": purpose, "consent_id": str(consent.id),
                       "version": consent.consent_text_version},
                metadata={"ip_address": ip_address, "user_agent": user_agent},
            )
        self.db.refresh(consent)
        return consent

    def initiate_verification(
        self,
        application_id: UUID,
        verification_type: str,  # consent-purpose name
        _in_external_txn: bool = False,
    ) -> PlatformVerification:
        """Gate on consent + state, create a pending verification, dispatch, emit event."""
        mapped = CONSENT_TO_VERIFICATION_TYPE.get(verification_type)
        if mapped is None:
            raise UnknownVerificationType(
                f"No platform_verification_type mapping for purpose '{verification_type}'"
            )

        with self._unit_of_work(_in_external_txn):
            application = self._get_application(application_id, lock=True)
            if application.status not in _PRE_DECISION_STATUSES:
                raise InvalidStateTransition(
                    f"Cannot initiate verification in status '{application.status}'"
                )

            consent = self._find_active_consent(application.patient_id, verification_type)
            if consent is None:
                raise ConsentMissingError(
                    f"Consent required for purpose '{verification_type}'"
                )

            existing = (
                self.db.query(PlatformVerification)
                .filter(
                    PlatformVerification.application_id == application_id,
                    PlatformVerification.verification_type == mapped,
                    PlatformVerification.status.in_(_PENDING_STATUSES),
                )
                .first()
            )
            if existing is not None:
                raise DuplicateVerificationError(
                    f"A pending {mapped} verification already exists for this application"
                )

            dispatch = self.dispatcher.initiate(
                verification_type=mapped,
                application_id=application_id,
                patient_id=application.patient_id,
                payload={},
            )
            verification = PlatformVerification(
                patient_id=application.patient_id,
                application_id=application_id,
                verification_type=mapped,
                status="pending",
                vendor=dispatch.vendor,
                vendor_session_ref=dispatch.vendor_session_ref,
                consent_id=consent.id,
            )
            self.db.add(verification)

            before_status = application.status
            if application.status == "started":
                application.status = "verifying"
                application.status_updated_at = datetime.now(timezone.utc)
            self.db.flush()

            self._emit_event(
                event_type="verification_initiated",
                actor_type="system",
                actor_id="system",
                application=application,
                verification_type=mapped,
                before={"status": before_status},
                after={"status": application.status, "verification_id": str(verification.id)},
            )
        self.db.refresh(verification)
        return verification

    def handle_verification_result(
        self,
        application_id: UUID,
        verification_id: UUID,
        vendor_event_id: str,
        result: str,  # 'passed' | 'failed' | 'manual_review' (P7.5)
        rich_payload: dict,
        _in_external_txn: bool = False,
    ) -> HandleResult:
        """Persist a verification result; decide when all verifications are terminal."""
        # Idempotency (decision #3): dedupe on vendor_event_id in the event log.
        cached = self._find_completed_event(vendor_event_id)
        if cached is not None:
            application = self._get_application(application_id)
            return HandleResult(
                verification_id=verification_id,
                application_status=application.status,
                decided=application.status in _DECISION_STATUSES,
                idempotent_replay=True,
                decision=application.decision,
            )

        new_status = _TERMINAL_RESULT_TO_STATUS.get(result)
        if new_status is None:
            raise OrchestratorError(f"Unsupported verification result '{result}'")

        decision_dict: Optional[dict] = None
        with self._unit_of_work(_in_external_txn):
            application = self._get_application(application_id, lock=True)  # row lock (decision #7)
            verification = (
                self.db.query(PlatformVerification)
                .filter(
                    PlatformVerification.id == verification_id,
                    PlatformVerification.application_id == application_id,
                )
                .first()
            )
            if verification is None:
                raise OrchestratorError(
                    f"Verification {verification_id} not found for application {application_id}"
                )

            verification.status = new_status
            verification.completed_at = datetime.now(timezone.utc)
            if "cost_cents" in rich_payload:
                verification.cost_cents = rich_payload["cost_cents"]

            self._emit_event(
                event_type="verification_completed",
                actor_type="vendor",
                actor_id=verification.vendor or "mock",
                application=application,
                vendor_event_id=vendor_event_id,
                verification_type=verification.verification_type,
                rich_payload=rich_payload,
                after={"verification_id": str(verification.id), "result": result},
            )
            self.db.flush()

            if self._ready_to_decide(application):
                decision_dict = self._decide(application)
        if not _in_external_txn:
            self.db.refresh(application)
        return HandleResult(
            verification_id=verification_id,
            application_status=application.status,
            decided=decision_dict is not None,
            idempotent_replay=False,
            decision=decision_dict,
        )

    def submit_for_decision(
        self, application_id: UUID, _in_external_txn: bool = False
    ) -> SubmitResult:
        """Patient 'I'm done'. Idempotent if decided; 409 if verifications pending."""
        application = self._get_application(application_id)
        if application.status in _DECISION_STATUSES:
            return SubmitResult(
                application_id=application_id,
                status=application.status,
                decision=application.decision,
                already_decided=True,
            )

        pending = self._pending_verification_types(application_id)
        if pending:
            raise StillPendingError(pending)

        with self._unit_of_work(_in_external_txn):
            application = self._get_application(application_id, lock=True)
            decision_dict = self._decide(application)
        if not _in_external_txn:
            self.db.refresh(application)
        return SubmitResult(
            application_id=application_id,
            status=application.status,
            decision=decision_dict,
            already_decided=False,
        )

    # -- internals ----------------------------------------------------------

    def _find_active_consent(self, patient_id: UUID, purpose: str) -> Optional[PlatformConsent]:
        for consent in self.consent_service.get_active_consents_for_patient(self.db, patient_id):
            if consent.purpose == purpose and consent.consent_granted:
                return consent
        return None

    def _find_completed_event(self, vendor_event_id: str) -> Optional[Any]:
        return self.db.execute(
            text(
                """
                SELECT id FROM platform_events
                WHERE event_type = 'verification_completed'
                  AND payload @> :key
                LIMIT 1
                """
            ),
            {"key": json.dumps({"vendor_event_id": vendor_event_id})},
        ).first()

    def _has_pending_verifications(self, application_id: UUID) -> bool:
        return (
            self.db.query(PlatformVerification.id)
            .filter(
                PlatformVerification.application_id == application_id,
                PlatformVerification.status.in_(_PENDING_STATUSES),
            )
            .first()
            is not None
        )

    def _ready_to_decide(self, application: PlatformCreditApplication) -> bool:
        """Decide only once every matrix-required verification is terminal and
        nothing is still pending. run_flow always requests identity + bank + soft
        (and hard when soft passes), so a subset is not enough."""
        if self._has_pending_verifications(application.id):
            return False
        product = self._get_product(application.credit_product_id)
        required_purposes = self._required_purposes_from_matrix(product.verification_matrix)
        required_vtypes = {CONSENT_TO_VERIFICATION_TYPE[p] for p in required_purposes}
        terminal_rows = (
            self.db.query(PlatformVerification.verification_type)
            .filter(
                PlatformVerification.application_id == application.id,
                PlatformVerification.status.notin_(_PENDING_STATUSES),
            )
            .all()
        )
        terminal_vtypes = {r[0] for r in terminal_rows}
        return required_vtypes.issubset(terminal_vtypes)

    def _pending_verification_types(self, application_id: UUID) -> list[str]:
        rows = (
            self.db.query(PlatformVerification.verification_type)
            .filter(
                PlatformVerification.application_id == application_id,
                PlatformVerification.status.in_(_PENDING_STATUSES),
            )
            .all()
        )
        return [r[0] for r in rows]

    def _build_stored_results(self, application_id: UUID) -> dict[str, dict]:
        """Reconstruct {verification_type: {result, **rich_payload}} from the event log."""
        rows = self.db.execute(
            text(
                """
                SELECT payload FROM platform_events
                WHERE event_type = 'verification_completed'
                  AND application_id = :app_id
                ORDER BY occurred_at ASC
                """
            ),
            {"app_id": str(application_id)},
        ).fetchall()
        stored: dict[str, dict] = {}
        for (payload,) in rows:
            vtype = payload.get("verification_type")
            if not vtype:
                continue
            merged = dict(payload.get("rich_payload") or {})
            merged["result"] = payload.get("after", {}).get("result") or merged.get("result")
            stored[vtype] = merged
        return stored

    def _decide(self, application: PlatformCreditApplication) -> dict:
        """Run the pure core over collected results and persist the decision."""
        if application.status not in _PRE_DECISION_STATUSES:
            raise InvalidStateTransition(
                f"Cannot decide an application in status '{application.status}'"
            )

        product = self._get_product(application.credit_product_id)
        patient = (
            self.db.query(PlatformPatient)
            .filter(PlatformPatient.id == application.patient_id)
            .first()
        )
        profile = PatientProfile(
            patient_id=application.patient_id,
            province=None,  # province lives in platform_patient_fields; None = no QC block in P6
            email=patient.email if patient else None,
        )
        stored = self._build_stored_results(application.id)
        adapters = FlowAdapters(
            verification=ReplayVerificationAdapter(stored),
            bureau=ReplayBureauAdapter(stored),
            bank=ReplayBankAdapter(stored),
        )
        flow_decision: FlowDecision = asyncio.run(
            run_flow(application, product, profile, adapters)
        )

        before_status = application.status
        decision_summary = {
            "decision": flow_decision.decision,
            "decision_reasons": flow_decision.decision_reasons,
            "verifications_performed": flow_decision.verifications_performed,
            "next_state": flow_decision.next_state,
        }
        # next_state is the valid status enum value (manual_review → under_review).
        application.status = flow_decision.next_state
        application.status_updated_at = datetime.now(timezone.utc)
        application.decision = decision_summary
        application.decision_at = datetime.now(timezone.utc)
        application.decision_by = "auto"

        # One decision_made event; run_flow's events_to_emit are folded in as content (decision #10).
        self._emit_event(
            event_type="decision_made",
            actor_type="system",
            actor_id="system",
            application=application,
            before={"status": before_status},
            after={"status": application.status, "decision": flow_decision.decision},
            rich_payload={"flow_decision": flow_decision.to_dict()},
        )
        self.db.flush()
        logger.info(
            "decision_made",
            application_id=str(application.id),
            decision=flow_decision.decision,
            status=application.status,
        )
        return decision_summary
