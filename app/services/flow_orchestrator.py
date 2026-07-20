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
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Literal, Optional, Protocol
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


class InvalidAmountError(OrchestratorError):
    """Requested amount is outside the product's [min, max] bounds (→ 422)."""


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


def mark_manual_review(application: PlatformCreditApplication) -> None:
    """Transition an application into manual review (status='under_review').

    The status-transition RULE for the manual-application path lives here, in the
    orchestrator module, because application status transitions are owned by the
    orchestrator (spec §4.3 — enforced by tests/test_application_status_writes.py).
    The caller (the manual-application endpoint) owns persisting the manually
    captured fields + the event; this owns only the status decision.
    """
    application.status = "under_review"


# ---------------------------------------------------------------------------
# Dave's named status workflow (pre-origination → origination → verification →
# underwriting → approved/rejected).
#
# Application status transitions are owned by THIS module (spec §4.3, enforced by
# tests/test_application_status_writes.py + the Semgrep money-path guardrail). So
# every named transition is a small function here; callers (endpoints/services)
# invoke these instead of ever assigning ``application.status`` themselves.
#
# The functions only decide the status value (and stamp status_updated_at). The
# caller owns the surrounding unit-of-work, event emission, and field persistence.
# The transitions are intentionally permissive about the *source* state (the
# scorecard/gating rules that decide when a move is legal are a business decision
# left to the calling layer / config, not hard-coded here) — they never move a
# terminal application, which is the one invariant we do enforce.
# ---------------------------------------------------------------------------

# Terminal states that must never be re-opened by a workflow transition.
_TERMINAL_STATUSES = ("approved", "declined", "withdrawn", "expired")


def _assert_not_terminal(application: PlatformCreditApplication, target: str) -> None:
    if application.status in _TERMINAL_STATUSES:
        raise InvalidStateTransition(
            f"Cannot move application to '{target}' from terminal status "
            f"'{application.status}'"
        )


def mark_origination(application: PlatformCreditApplication) -> None:
    """Move the application into the ORIGINATION state (being filled out)."""
    _assert_not_terminal(application, "origination")
    application.status = "origination"
    application.status_updated_at = datetime.now(timezone.utc)


def mark_verification(application: PlatformCreditApplication) -> None:
    """Move the application into the VERIFICATION state (status='verifying')."""
    _assert_not_terminal(application, "verifying")
    application.status = "verifying"
    application.status_updated_at = datetime.now(timezone.utc)


def mark_underwriting(application: PlatformCreditApplication) -> None:
    """Move the application into the UNDERWRITING state (human/automated adjudication)."""
    _assert_not_terminal(application, "underwriting")
    application.status = "underwriting"
    application.status_updated_at = datetime.now(timezone.utc)


def mark_vendor_reprocessing(application: PlatformCreditApplication) -> None:
    """Vendor "Request reprocessing" — the ONLY vendor underwriting action (WS-I).

    Dave (10__Vendor_Access.md): vendors get exactly one lever while a deal is
    with PaySpyre — a request to send it back. Valid while the deal is in
    adjudication (``under_review`` / ``underwriting``) and on a ``declined``
    file (an auto-decline must route to a human, never hard-reject to the
    vendor); the request re-opens the file into ``under_review`` for a human
    underwriter. It must never re-open ``approved``/``withdrawn``/``expired``
    (those stay behind the standard terminal guard).
    """
    reprocessable = ("declined", "under_review", "underwriting")
    if application.status not in reprocessable:
        raise InvalidStateTransition(
            f"Cannot request reprocessing from status '{application.status}' "
            f"(allowed: {', '.join(reprocessable)})"
        )
    application.status = "under_review"
    application.status_updated_at = datetime.now(timezone.utc)


def mark_cancelled(application: PlatformCreditApplication) -> None:
    """Terminal NON-CREDIT closure — the staff "Cancel" action (WS-E).

    Cancellation is an administrative termination (customer request, duplicate,
    vendor request, expired offer/verification), distinct from a credit decline:
    it maps onto the existing ``withdrawn`` terminal status and therefore never
    triggers the adverse-action path (which fires only on ``declined``). The
    caller owns the reason-code validation, audit event, and notification.
    """
    _assert_not_terminal(application, "withdrawn")
    application.status = "withdrawn"
    application.status_updated_at = datetime.now(timezone.utc)


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
        # Outbound side effects (e-sign invite, adverse-action email — any non-DB
        # vendor HTTP) registered by ``_decide`` to run AFTER the decision
        # transaction commits, so a slow/hanging vendor can never hold the
        # application row lock or roll back the persisted decision. Drained by
        # ``_run_pending_outbound`` once the unit-of-work has committed.
        self._pending_outbound: list[tuple[str, Callable[[], None]]] = []

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

    def _run_pending_outbound(self) -> None:
        """Run the outbound side effects ``_decide`` deferred, AFTER the decision
        transaction has committed and the application row lock is released.

        Each action is independent and defensive: a vendor failure here can NOT
        roll back the already-committed decision (we are outside the unit-of-work)
        and must NOT block the others — every action is wrapped in its own
        try/except, mirroring the inline ``except Exception`` the LMS/adverse-action
        hooks used to carry. The queue is drained even on failure so a retried
        public call does not re-fire a stale action.

        Some actions flush their own follow-up DB writes (the adverse-action audit
        event) without committing — previously the decision unit-of-work committed
        those. Since we now run outside that unit-of-work, we commit once at the end
        so those writes persist. ``send_agreement`` already commits internally; the
        trailing commit is a harmless no-op when there is nothing pending.
        """
        pending, self._pending_outbound = self._pending_outbound, []
        if not pending:
            return
        for label, action in pending:
            try:
                action()
            except Exception as exc:  # noqa: BLE001 — decision integrity over side effect
                logger.error(
                    "decision_outbound_failed",
                    outbound=label,
                    error=str(exc),
                )
        # Persist any follow-up writes the actions flushed (e.g. the adverse-action
        # audit event). The committed decision is untouched by this.
        try:
            self.db.commit()
        except Exception as exc:  # noqa: BLE001 — never surface to the decided caller
            logger.error("decision_outbound_commit_failed", error=str(exc))
            self.db.rollback()

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

    @staticmethod
    def _decision_product_view(
        application: PlatformCreditApplication, live_product: PlatformCreditProduct
    ) -> Any:
        """The product config the decision must run against.

        Security finding #6 / Hard Rule #7-8: the decision uses the
        ``verification_matrix`` snapshotted onto the application at creation
        (migration 026), NOT the live product row — products are edited in place
        (``update_credit_product`` mutates + bumps ``version``), so the live row may
        carry post-application thresholds. Returns a lightweight view exposing the
        single attribute the pure engine reads (``verification_matrix``) plus id /
        version. Falls back to the live product for legacy rows created before
        migration 026 (snapshot is NULL), logging that it did so.
        """
        snapshot = getattr(application, "product_config_snapshot", None)
        if snapshot is None:
            logger.warning(
                "decision_using_live_product_config_no_snapshot",
                application_id=str(application.id),
                credit_product_id=str(application.credit_product_id),
            )
            return live_product
        return SimpleNamespace(
            id=live_product.id,
            version=application.credit_product_version,
            verification_matrix=snapshot,
        )

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
        # P8.1 (M-7) — flush so the append-only bigint id is assigned BEFORE we mirror
        # to PostHog; otherwise event.id is None and every orchestrator-sourced analytics
        # event ships platform_event_id=null, breaking the Postgres<->analytics linkage.
        # The dispatcher/webhook capture paths already flush first; this aligns them.
        self.db.flush()
        # P8.0 — fire-and-forget mirror to PostHog. Local import keeps the
        # dependency optional + avoids any circular at startup.
        from app.services.observability.posthog_bridge import capture_event
        capture_event(event)
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
        # Enforce the product's amount bounds at the single creation chokepoint (covers
        # both the patient and clinic-financing-link entry points). Without this the
        # requested amount was only validated > 0, so an approved application could book
        # a real loan + schedule for any out-of-bounds principal (over/under-lending).
        if not (product.min_amount_cents <= requested_amount_cents <= product.max_amount_cents):
            raise InvalidAmountError(
                f"Requested amount {requested_amount_cents} is outside the product's "
                f"allowed range [{product.min_amount_cents}, {product.max_amount_cents}]."
            )
        with self._unit_of_work(_in_external_txn):
            application = PlatformCreditApplication(
                patient_id=patient_id,
                credit_product_id=credit_product_id,
                credit_product_version=product.version,  # snapshot (Hard Rule)
                # Snapshot the decisioning config too, not just the version int
                # (security finding #6 / Hard Rule #7-8): the decision must use the
                # matrix as of creation, because products are edited in place.
                product_config_snapshot=product.verification_matrix,
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
        from app.services.metrics.platform_metrics import record_application_started

        record_application_started(product.code, vendor_id=vendor_id, vertical=product.vertical)
        logger.info(
            "application_created",
            application_id=str(application.id),
            patient_id=str(patient_id),
            credit_product_version=product.version,
        )
        return application

    def get_required_consents(self, application_id: UUID) -> list[str]:
        """Return the consent purposes required to complete the application.

        Read-only. Derived from the product's verification_matrix:
        - identity.required               → id_verification
        - income with 'bank_link' method  → bank_verification
        - bureau.soft_pull_required       → soft_bureau_pull
        - bureau.hard_pull_required       → hard_bureau_pull

        Policy (Dave, 2026-07): running an automated lending decision does NOT
        require a discrete borrower consent — that is the lender's call, not the
        applicant's. ``automated_decision_making`` is therefore no longer surfaced
        here and ``_decide`` no longer gates on it. The identity / bank / bureau
        verification consents above are unaffected and remain required.
        NOTE: compliance sign-off required before merge (PIPEDA automated-decisioning).
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
        from app.services.metrics.platform_metrics import record_consent

        record_consent(purpose, granted=True)
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

        # If the application is already decided, a late vendor result (new
        # vendor_event_id, so the cache above missed) must NOT proceed: processing it
        # would flip the verification, then _decide would raise InvalidStateTransition
        # and the whole unit-of-work rolls back (losing the verification write) with a
        # 422 → the vendor retries forever. Treat it as an idempotent no-op instead.
        application = self._get_application(application_id)
        if application.status in _DECISION_STATUSES:
            return HandleResult(
                verification_id=verification_id,
                application_status=application.status,
                decided=True,
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
            from app.services.metrics.platform_metrics import record_verification_completed

            record_verification_completed(
                type=verification.verification_type,
                vendor=verification.vendor or "mock",
                status=new_status,
                cost_cents=rich_payload.get("cost_cents"),
            )

            # Maintain the patient-level marketplace denorm fields (lead_state +
            # verification_depth) as verifications pass — the marketplace tiers +
            # prices leads off these. Forward-only; safe to run on any result.
            from app.services import lead_metrics

            patient = (
                self.db.query(PlatformPatient)
                .filter(PlatformPatient.id == application.patient_id)
                .first()
            )
            if patient is not None:
                lead_metrics.refresh_from_verifications(self.db, patient)

            if self._ready_to_decide(application):
                decision_dict = self._decide(application)
        if not _in_external_txn:
            # Transaction is committed and the row lock is released; only now run the
            # vendor-facing side effects ``_decide`` deferred (e-sign / adverse-action).
            # When _in_external_txn, the caller owns the commit and must drain.
            self._run_pending_outbound()
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
            # Post-commit, lock released: fire the deferred outbound side effects.
            self._run_pending_outbound()
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

        # Policy (Dave, 2026-07): an automated lending decision is NOT gated behind a
        # discrete borrower "consent to automated decision" grant — whether to run an
        # automated decision is the lender's call, not the applicant's. The prior
        # finding-#4 gate is intentionally removed here. The identity/bank/bureau
        # verification consents remain enforced upstream in ``initiate_verification``.
        # NOTE: compliance/human sign-off required before merge (PIPEDA
        # automated-decisioning safeguards / adverse-action notice still apply).

        product = self._get_product(application.credit_product_id)
        decision_product = self._decision_product_view(application, product)
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
        # WS-E: a vendor-override flag on the application routes the
        # recently-discharged-bankruptcy case to manual_review instead of decline
        # (never an auto-approve). Derived here so the engine stays pure
        # (flag in, decision out). Set by the vendor-origination path (WS-I).
        vendor_override = bool((application.flow_state or {}).get("vendor_override"))
        # WS-D: resolve the governing 5-band scorecard (vendor assignment →
        # platform default → None). None = the legacy 680/600 path, unchanged —
        # a fresh install has no scorecard rows, so default decisions are
        # identical to before. Inputs come from the SAME stored verification
        # results the replay adapters use (verified data only, never
        # self-reported). A malformed scorecard row must never block a decision:
        # fall back to the legacy path and log.
        from app.services import scorecards as scorecards_svc

        scorecard_ref = None
        scorecard_inputs = None
        try:
            scorecard_ref = scorecards_svc.resolve_for_application(self.db, application)
        except Exception as exc:  # noqa: BLE001 — decision integrity over scorecard config
            logger.error(
                "scorecard_resolution_failed",
                application_id=str(application.id),
                error=str(exc),
            )
        if scorecard_ref is not None:
            scorecard_inputs = scorecards_svc.build_verified_inputs(stored)
        flow_decision: FlowDecision = asyncio.run(
            run_flow(
                application, decision_product, profile, adapters,
                vendor_override=vendor_override,
                scorecard=scorecard_ref,
                scorecard_inputs=scorecard_inputs,
            )
        )

        before_status = application.status
        decision_summary = {
            "decision": flow_decision.decision,
            "decision_reasons": flow_decision.decision_reasons,
            "verifications_performed": flow_decision.verifications_performed,
            "next_state": flow_decision.next_state,
        }
        # WS-D: persist the scorecard band + per-band limit/rate on the decision
        # record so underwriters see WHY (and offer creation can honor the
        # band's limit/rate). Absent on the legacy path — payload unchanged.
        if flow_decision.scorecard_result is not None:
            decision_summary["scorecard"] = flow_decision.scorecard_result
        # next_state is the valid status enum value (manual_review → under_review).
        application.status = flow_decision.next_state
        application.status_updated_at = datetime.now(timezone.utc)
        application.decision = decision_summary
        application.decision_at = datetime.now(timezone.utc)
        application.decision_by = "auto"

        # Stamp the terminal marketplace lead_state (approved/declined) + capture
        # any verification depth gained in this flow. Non-terminal (under_review)
        # is left on the forward ladder maintained at verification time.
        if patient is not None:
            from app.services import lead_metrics

            lead_metrics.apply_decision(self.db, patient, application.status)

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
        from app.services.metrics.platform_metrics import record_decision

        record_decision(product.code, flow_decision.decision)
        # P9.x — LMS hand-off: when approved, book the loan now (DB, in-txn) but DEFER
        # the e-sign invite. book_loan persists the loan + amortization schedule with
        # the decision, so it stays inside this unit-of-work; it never makes a vendor
        # call. send_agreement DOES make a SignNow HTTP call, so it must run only AFTER
        # this transaction commits and the application row lock is released — a hanging
        # SignNow must not pin the lock or roll back the decision. Registered as a
        # post-commit action drained by ``_run_pending_outbound`` (book_loan is
        # idempotent; send_agreement is forward-only + gracefully no-ops if SignNow is
        # unconfigured). loan-booking must never fail the decision, so it is guarded.
        if application.status == "approved":
            try:
                from app.services import loan_lifecycle

                loan = loan_lifecycle.book_loan(self.db, application)
            except Exception as exc:  # noqa: BLE001 — decision integrity over LMS hand-off
                logger.error(
                    "loan_booking_failed",
                    application_id=str(application.id),
                    error=str(exc),
                )
            else:
                # Post-commit: SignNow invite. The loan row is committed by the
                # unit-of-work before this closure runs.
                def _send_agreement(loan=loan) -> None:
                    from app.services import loan_lifecycle

                    loan_lifecycle.send_agreement(self.db, loan)

                self._pending_outbound.append(("send_agreement", _send_agreement))
        # P9.x — on a decline, send the Canadian notice of decision (audit §7).
        # The notice is an OUTBOUND email (SendGrid HTTP) + its own DB event write, so
        # it is deferred to post-commit for the same lock/latency reason as the e-sign
        # invite above. It is internally DEFENSIVE + idempotent (dedupes on its own
        # event) and never raises; the post-commit runner also guards it.
        if application.status == "declined":
            def _send_adverse_action(reasons=flow_decision.decision_reasons) -> None:
                from app.services import adverse_action

                adverse_action.send_adverse_action_notice(self.db, application, reasons)

            self._pending_outbound.append(("adverse_action_notice", _send_adverse_action))
        logger.info(
            "decision_made",
            application_id=str(application.id),
            decision=flow_decision.decision,
            status=application.status,
        )
        return decision_summary
