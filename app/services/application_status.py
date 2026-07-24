"""Dave's canonical **Application Status Flow v1.00** — status registry.

Source of truth: ``docs/dave_review_2026-07-21/PaySpyre - Application Status Flow
v1,00.pdf`` (+ ``GAP_ANALYSIS_ORIGINATIONS.md`` §A). Dave: *"I had previously
provided detailed information on the application flow and the different status of
an application up to activation. The present iteration does not accurately
reflect the previously provided documentation."* This module is that correction.

The flow::

    Pre-Origination -> Origination -> [ Credit Report
                                      ∥ Bank Account Verification
                                      ∥ Application Verification ]
                    -> Credit Underwriting -> Offer Acceptance
                    -> Agreement Signature -> Approved -> Active

with the six closed states hanging off ``Active``: Repaid, Renewed, Refinanced,
Transferred, Settlement, Write off.

ACTIVATION-REWORK LIFECYCLE (Wave 3 registry alignment)
-------------------------------------------------------
The loan is now CREATED at activation, not at approval. In the new lifecycle the
Offer -> Accept -> Agreement -> Sign -> Activate ladder runs entirely pre-loan::

    Offer Acceptance -> Agreement Signature -> [Activate] -> Active

``Activate Loan`` therefore resolves PRIMARILY from ``agreement_signature`` (the
signed-agreement, pre-loan state where ``application.agreement_status == 'signed'``
but no loan row exists yet). It stays resolvable from ``approved`` too for the
flag-OFF / grandfathered world, where the loan is still booked at approval and the
loan surface's disburse action governs it. The linear ``order`` below is left on
the legacy ladder (... -> Agreement Signature -> Approved -> Active) so the flag-OFF
path stays coherent and the golden order test holds; the per-status *actions* and
*notes* are what carry the new lifecycle (activation-rework Wave 2 behaviour,
flag ``ACTIVATION_BOOKS_LOAN``). The registry only describes the actions available
per status — it never flips behaviour.

TWO LAYERS, DELIBERATELY
------------------------
1. **Engine status** — the value persisted in
   ``platform_credit_applications.status`` (the ``platform_application_status``
   PG enum). This is what the decision engine, the queues and ~2500 tests read
   and write. It is a *superset* of Dave's model: it keeps three legacy values
   that carry engine meaning Dave's model does not name (``started``,
   ``verifying``, ``under_review``) and three terminal outcomes his forward flow
   has no slot for (``rejected``, ``withdrawn``, ``expired``).
2. **Canonical status** (this module) — Dave's named status, with its
   preconditions, owning workplace(s), permitted actions and external API. Every
   engine value maps onto exactly one canonical status (``LEGACY_TO_CANONICAL``);
   several engine values may share one canonical status.

The UI renders *off this registry* — ``actions_for(engine_status)`` returns the
buttons a workplace may show, so nothing hard-codes a button list per screen.

WHY NOT JUST RENAME THE ENGINE VALUES?
--------------------------------------
Two of the collapses are many-to-one and merging them would be a behavioural
regression, not a rename:

* ``underwriting`` (pre-decision, adjudication in progress) and ``under_review``
  (post-decision, referred to a human by the automated core) both map to Dave's
  **Credit Underwriting**. But the orchestrator treats ``under_review`` as
  *decided* (``_DECISION_STATUSES``) and ``underwriting`` as *not decided* — a
  late verification webhook on a merged value would be silently swallowed.
* ``verifying`` is the engine's *band-level* "verifications in flight" value; it
  does not know which of Dave's three parallel gates is open (they are parallel,
  and a single column cannot hold three states). The three gate values exist for
  the underwriting workplace's explicit, per-gate transitions; ``verifying``
  remains the automated path's band-level value and maps to the same band here.

Nothing is dropped: ``rejected`` / ``withdrawn`` / ``expired`` are preserved as
first-class terminal canonical statuses ALONGSIDE Dave's model (see
``OFF_MODEL_TERMINALS``), because a real lending system must record a credit
rejection, an administrative cancellation and a lapsed offer, and Dave's forward
flow only describes the happy path plus per-status "Cancel"/"Reject" actions.

Dave (2026-07-22, stated twice): the credit-decision status is **Rejected**, NOT
"Declined". ``Rejected`` and ``Cancelled`` are independent statuses with their
own reason lists (``platform_decision_reasons`` kinds ``reject`` / ``cancel``).
The engine enum value was renamed ``declined`` -> ``rejected`` in migration 076.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class Workplace(str, Enum):
    """The workplace (top-nav destination) that OWNS an application in a status."""

    APPLICATION_PAGE = "application_page"
    ORIGINATION = "origination"
    UNDERWRITING = "underwriting"
    SERVICING = "servicing"
    COLLECTIONS = "collections"
    ARCHIVE = "archive"


class Action(str, Enum):
    """A permitted action, verbatim from Dave's Actions column."""

    COMPLETE_APPLICATION = "complete_application"
    CANCEL = "cancel"
    SUBMIT_FOR_CREDIT_UNDERWRITING = "submit_for_credit_underwriting"
    REQUEST_CREDIT_REPORT_AUTHORIZATION = "request_credit_report_authorization"
    REQUEST_BANK_VERIFICATION = "request_bank_verification"
    REQUEST_ADDITIONAL_INFORMATION = "request_additional_information"
    RETURN_FOR_REPROCESSING = "return_for_reprocessing"
    APPROVE = "approve"
    REJECT = "reject"
    CONTACT_APPLICANT = "contact_applicant"
    REGISTER_OFFER_ACCEPTANCE = "register_offer_acceptance"
    ACTIVATE_LOAN = "activate_loan"
    MANUAL_PAYMENT = "manual_payment"
    CHARGE_PAYMENT = "charge_payment"
    RESTRUCTURE_HARDSHIP = "restructure_hardship"
    CHANGE_PAYMENT_SCHEDULE = "change_payment_schedule"
    SET_CLOSED = "set_closed"


class ExternalApi(str, Enum):
    """The external API a status depends on (Dave's API column)."""

    EQUIFAX_CANADA = "equifax_canada"
    FLINKS_CAPITAL = "flinks_capital"


class CanonicalStatus(str, Enum):
    """Dave's named statuses + the three off-model terminals we preserve."""

    # --- forward flow ---
    PRE_ORIGINATION = "pre_origination"
    ORIGINATION = "origination"
    CREDIT_REPORT = "credit_report"
    BANK_ACCOUNT_VERIFICATION = "bank_verification"
    APPLICATION_VERIFICATION = "application_verification"
    CREDIT_UNDERWRITING = "credit_underwriting"
    OFFER_ACCEPTANCE = "offer_acceptance"
    AGREEMENT_SIGNATURE = "agreement_signature"
    APPROVED = "approved"
    ACTIVE = "active"
    # --- closed states off Active ---
    REPAID = "repaid"
    RENEWED = "renewed"
    REFINANCED = "refinanced"
    TRANSFERRED = "transferred"
    SETTLEMENT = "settlement"
    WRITTEN_OFF = "written_off"
    # --- off-model terminals (ours, preserved — see module docstring) ---
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


#: The parallel verification band (Dave's three side-by-side gates).
VERIFICATION_GATES: tuple[CanonicalStatus, ...] = (
    CanonicalStatus.CREDIT_REPORT,
    CanonicalStatus.BANK_ACCOUNT_VERIFICATION,
    CanonicalStatus.APPLICATION_VERIFICATION,
)

#: The six closed states reachable from Active.
CLOSED_STATUSES: tuple[CanonicalStatus, ...] = (
    CanonicalStatus.REPAID,
    CanonicalStatus.RENEWED,
    CanonicalStatus.REFINANCED,
    CanonicalStatus.TRANSFERRED,
    CanonicalStatus.SETTLEMENT,
    CanonicalStatus.WRITTEN_OFF,
)

#: Terminal outcomes Dave's forward flow does not name; preserved, not dropped.
OFF_MODEL_TERMINALS: tuple[CanonicalStatus, ...] = (
    CanonicalStatus.REJECTED,
    CanonicalStatus.CANCELLED,
    CanonicalStatus.EXPIRED,
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusSpec:
    """One row of Dave's table, machine-readable."""

    status: CanonicalStatus
    label: str
    order: int                      # position in the linear flow (gates share one)
    preconditions: tuple[str, ...]
    description: tuple[str, ...]
    workplaces: tuple[Workplace, ...]
    actions: tuple[Action, ...]
    apis: tuple[ExternalApi, ...] = ()
    #: engine (DB) status values that resolve to this canonical status
    engine_statuses: tuple[str, ...] = ()
    #: canonical statuses reachable next on the happy path
    next_statuses: tuple[CanonicalStatus, ...] = ()
    is_terminal: bool = False
    #: statuses that run in parallel with this one ("verification" band)
    parallel_group: Optional[str] = None
    #: honest note where our implementation differs from the table
    note: Optional[str] = None
    closed_options: tuple[CanonicalStatus, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "label": self.label,
            "order": self.order,
            "preconditions": list(self.preconditions),
            "description": list(self.description),
            "workplaces": [w.value for w in self.workplaces],
            "actions": [a.value for a in self.actions],
            "apis": [a.value for a in self.apis],
            "engine_statuses": list(self.engine_statuses),
            "next_statuses": [s.value for s in self.next_statuses],
            "is_terminal": self.is_terminal,
            "parallel_group": self.parallel_group,
            "closed_options": [s.value for s in self.closed_options],
            "note": self.note,
        }


_SPECS: tuple[StatusSpec, ...] = (
    StatusSpec(
        status=CanonicalStatus.PRE_ORIGINATION,
        label="Pre-Origination",
        order=1,
        preconditions=(
            "A user has started the creation of an application",
            "AND the application has not been fully completed",
        ),
        description=(
            "A loan application is being created",
            "PaySpyre account is created for the customer",
        ),
        workplaces=(Workplace.APPLICATION_PAGE, Workplace.ORIGINATION),
        actions=(Action.COMPLETE_APPLICATION, Action.CANCEL),
        engine_statuses=("started",),
        next_statuses=(CanonicalStatus.ORIGINATION,),
    ),
    StatusSpec(
        status=CanonicalStatus.ORIGINATION,
        label="Origination",
        order=2,
        preconditions=("The application is created in the system",),
        description=(
            "Basic applicant and loan details are collected and entered in the system",
            "Pre-Qualification scoring - initial limited application scoring completed",
            "Decision to submit application for credit underwriting is made",
        ),
        workplaces=(Workplace.ORIGINATION,),
        actions=(Action.SUBMIT_FOR_CREDIT_UNDERWRITING, Action.CANCEL),
        engine_statuses=("origination", "pre_qualified"),
        next_statuses=VERIFICATION_GATES,
    ),
    StatusSpec(
        status=CanonicalStatus.CREDIT_REPORT,
        label="Credit Report",
        order=3,
        preconditions=(
            "The application has been submitted for credit underwriting",
            "AND the credit report(s) required for the application is not received",
            "(This check may be performed at different steps of the application "
            "process, before approval)",
        ),
        description=("A credit report for the applicant(s) is required",),
        workplaces=(Workplace.UNDERWRITING,),
        actions=(
            Action.REQUEST_CREDIT_REPORT_AUTHORIZATION,
            Action.RETURN_FOR_REPROCESSING,
            Action.CANCEL,
        ),
        apis=(ExternalApi.EQUIFAX_CANADA,),
        engine_statuses=("credit_report", "awaiting_hard_pull"),
        next_statuses=(CanonicalStatus.CREDIT_UNDERWRITING,),
        parallel_group="verification",
    ),
    StatusSpec(
        status=CanonicalStatus.BANK_ACCOUNT_VERIFICATION,
        label="Bank Account Verification",
        order=3,
        preconditions=(
            "The application has been submitted for credit underwriting",
            "AND the bank account verification required for the application has "
            "not been completed",
            "(This check may be performed at different steps of the application "
            "process, before approval)",
        ),
        description=("A bank account verification process is required",),
        workplaces=(Workplace.UNDERWRITING,),
        actions=(
            Action.REQUEST_BANK_VERIFICATION,
            Action.RETURN_FOR_REPROCESSING,
            Action.CANCEL,
        ),
        apis=(ExternalApi.FLINKS_CAPITAL,),
        engine_statuses=("bank_verification",),
        next_statuses=(CanonicalStatus.CREDIT_UNDERWRITING,),
        parallel_group="verification",
    ),
    StatusSpec(
        status=CanonicalStatus.APPLICATION_VERIFICATION,
        label="Application Verification",
        order=3,
        preconditions=(
            "The application has been submitted for credit underwriting",
            "AND the application information verification has not been completed",
            "(This check may be performed at different steps of the loan "
            "application assessment, before approval)",
        ),
        description=(
            "Verification of the loan information is required",
            "Know Your Customer (KYC) guidelines to be followed",
            "Manual verification if required",
            "Required information is verified per set procedures & standards",
        ),
        workplaces=(Workplace.UNDERWRITING,),
        actions=(
            Action.REQUEST_ADDITIONAL_INFORMATION,
            Action.RETURN_FOR_REPROCESSING,
            Action.CANCEL,
        ),
        engine_statuses=("application_verification", "verifying"),
        next_statuses=(CanonicalStatus.CREDIT_UNDERWRITING,),
        parallel_group="verification",
        note=(
            "The legacy band-level engine value 'verifying' (verifications in "
            "flight, specific gate unknown) resolves here."
        ),
    ),
    StatusSpec(
        status=CanonicalStatus.CREDIT_UNDERWRITING,
        label="Credit Underwriting",
        order=4,
        preconditions=(
            "Credit underwriting is required for the application",
            "AND the application satisfies all the previous conditions and checks",
        ),
        description=(
            "System generates an application risk score based on set credit "
            "underwriting guidelines",
            "The application is being auto-decisioned OR referred to an "
            "authorized underwriter",
            "An authorized underwriter is reviewing the application",
        ),
        workplaces=(Workplace.UNDERWRITING,),
        actions=(
            Action.APPROVE,
            Action.REJECT,
            Action.CANCEL,
            Action.RETURN_FOR_REPROCESSING,
        ),
        engine_statuses=("underwriting", "under_review"),
        next_statuses=(CanonicalStatus.OFFER_ACCEPTANCE, CanonicalStatus.REJECTED),
        note=(
            "Two engine values map here and must stay distinct: 'underwriting' is "
            "pre-decision, 'under_review' is the automated core's manual-review "
            "sink (already decided). See module docstring."
        ),
    ),
    StatusSpec(
        status=CanonicalStatus.OFFER_ACCEPTANCE,
        label="Offer Acceptance",
        order=5,
        preconditions=(
            "The application has been approved by the system or an authorized underwriter",
            "Loan offer(s) issued to the applicant(s)",
            "AND the offer has not yet been accepted by the applicant(s)",
        ),
        description=("Waiting for the applicant(s) to review and accept an offer",),
        workplaces=(Workplace.UNDERWRITING,),
        actions=(
            Action.CONTACT_APPLICANT,
            Action.REGISTER_OFFER_ACCEPTANCE,
            Action.CANCEL,
            Action.RETURN_FOR_REPROCESSING,
        ),
        engine_statuses=("offer_acceptance",),
        next_statuses=(CanonicalStatus.AGREEMENT_SIGNATURE,),
    ),
    StatusSpec(
        status=CanonicalStatus.AGREEMENT_SIGNATURE,
        label="Agreement Signature",
        order=6,
        preconditions=(
            "The application satisfies all the previous conditions and checks",
            "A loan offer has been accepted",
            "AND the loan agreement has been sent to the applicant(s) for review "
            "and signature",
        ),
        description=(
            "Waiting for the applicant(s) to review and sign the loan agreement",
            "Once every applicant has signed, the file is ready to ACTIVATE: "
            "activation books the loan and virtually disburses it — no loan row "
            "exists before this step in the activation-rework lifecycle",
        ),
        workplaces=(Workplace.UNDERWRITING, Workplace.SERVICING),
        actions=(Action.CONTACT_APPLICANT, Action.ACTIVATE_LOAN, Action.CANCEL),
        engine_statuses=("agreement_signature",),
        next_statuses=(CanonicalStatus.ACTIVE,),
        note=(
            "Activation-rework: the loan is CREATED at activation, not approval, so "
            "this signed-agreement / pre-loan state is where 'Activate Loan' lives "
            "in the new lifecycle. The action is offered here, but the maker-checker "
            "'activate' only fires once application.agreement_status == 'signed' "
            "(loan_lifecycle.activate_loan then books the loan and advances the file "
            "straight to 'active'). Cancel here simply closes the application — no "
            "loan has been booked yet, so there is nothing to unwind. In the flag-OFF "
            "/ grandfathered world the file instead advances to 'approved' (loan "
            "already booked) and activation governs from there."
        ),
    ),
    StatusSpec(
        status=CanonicalStatus.APPROVED,
        label="Approved",
        order=7,
        preconditions=(
            "The application satisfies all the previous conditions and checks",
            "The loan agreement has been signed by all applicant(s)",
            "AND the loan has not been activated",
        ),
        description=(
            "The loan is awaiting activation",
            "BUT it can still be cancelled by an authorized user",
        ),
        workplaces=(Workplace.SERVICING, Workplace.UNDERWRITING),
        actions=(Action.ACTIVATE_LOAN, Action.REJECT, Action.CANCEL),
        engine_statuses=("approved",),
        next_statuses=(CanonicalStatus.ACTIVE,),
        note=(
            "Activation-rework: the PRIMARY 'Activate Loan' step now lives at "
            "'agreement_signature' (the loan is booked at activation, not approval). "
            "'approved' is retained as the flag-OFF / grandfathered pre-activation "
            "state, so 'Activate Loan' stays resolvable here too. Resolved open "
            "question: because a pre-activation file has no booked loan, Cancel/Reject "
            "here just closes the application — there is no loan to unwind. When the "
            "flag is OFF the loan IS booked at approval and the loan surface's "
            "disburse action governs that grandfathered loan."
        ),
    ),
    StatusSpec(
        status=CanonicalStatus.ACTIVE,
        label="Active",
        order=8,
        preconditions=("The loan has been successfully activated and payments scheduled",),
        description=(
            "Loan is being serviced: schedule is updated",
            "Payments are processed",
            "Any payment issues are registered",
        ),
        workplaces=(Workplace.SERVICING, Workplace.COLLECTIONS),
        actions=(
            Action.MANUAL_PAYMENT,
            Action.CHARGE_PAYMENT,
            Action.RESTRUCTURE_HARDSHIP,
            Action.CHANGE_PAYMENT_SCHEDULE,
            Action.SET_CLOSED,
        ),
        engine_statuses=("active",),
        next_statuses=CLOSED_STATUSES,
        closed_options=CLOSED_STATUSES,
        note="Collections owns this status only while the loan is past due.",
    ),
)


def _closed_spec(status: CanonicalStatus, label: str, engine_value: str, why: str) -> StatusSpec:
    return StatusSpec(
        status=status,
        label=label,
        order=9,
        preconditions=("The loan was active and has been set to closed: " + label,),
        description=(why,),
        workplaces=(Workplace.SERVICING, Workplace.ARCHIVE),
        actions=(),
        engine_statuses=(engine_value,),
        is_terminal=True,
    )


_CLOSED_SPECS: tuple[StatusSpec, ...] = (
    _closed_spec(CanonicalStatus.REPAID, "Repaid", "repaid",
                 "The loan was paid in full per the agreement."),
    _closed_spec(CanonicalStatus.RENEWED, "Renewed", "renewed",
                 "The loan was closed and renewed into a new loan."),
    _closed_spec(CanonicalStatus.REFINANCED, "Refinanced", "refinanced",
                 "The loan was closed and refinanced into a new loan."),
    _closed_spec(CanonicalStatus.TRANSFERRED, "Transferred", "transferred",
                 "The loan was transferred to another holder/servicer."),
    _closed_spec(CanonicalStatus.SETTLEMENT, "Settlement", "settlement",
                 "The loan was closed under a negotiated settlement."),
    _closed_spec(CanonicalStatus.WRITTEN_OFF, "Write off", "written_off",
                 "The loan balance was written off."),
)


_OFF_MODEL_SPECS: tuple[StatusSpec, ...] = (
    StatusSpec(
        status=CanonicalStatus.REJECTED,
        label="Rejected",
        order=10,
        preconditions=("The application was rejected by the system or an underwriter",),
        description=(
            "A credit decision was made to reject the application. The principal "
            "reasons are captured from the reject reason directory.",
        ),
        workplaces=(Workplace.UNDERWRITING, Workplace.ARCHIVE),
        actions=(Action.RETURN_FOR_REPROCESSING,),
        engine_statuses=("rejected",),
        is_terminal=True,
        note=(
            "Not in Dave's forward flow — it is the outcome of his 'Reject the "
            "application' action. Preserved as a first-class terminal status: a "
            "credit rejection is distinct from a cancellation. FLAGGED FOR "
            "COUNSEL/DAVE: what notice (if any) a rejected Canadian applicant "
            "receives is an open legal question — no notice is auto-sent."
        ),
    ),
    StatusSpec(
        status=CanonicalStatus.CANCELLED,
        label="Cancelled",
        order=11,
        preconditions=("An authorized user cancelled the application",),
        description=(
            "Administrative termination (customer request, duplicate, vendor "
            "request). NOT a credit decision — its own cancel reason list applies.",
        ),
        workplaces=(Workplace.ARCHIVE,),
        actions=(),
        engine_statuses=("withdrawn",),
        is_terminal=True,
        note=(
            "Outcome of Dave's 'Cancel the application' action, which he lists on "
            "nearly every status but never names as a status. The engine value "
            "stays 'withdrawn' (pre-existing; renaming it buys nothing)."
        ),
    ),
    StatusSpec(
        status=CanonicalStatus.EXPIRED,
        label="Expired",
        order=12,
        preconditions=("All outstanding offers lapsed before acceptance",),
        description=("The application expired without an accepted offer.",),
        workplaces=(Workplace.ARCHIVE,),
        actions=(),
        engine_statuses=("expired",),
        is_terminal=True,
        note=(
            "Not in Dave's flow. Preserved: the 30-day offer-expiry sweep must "
            "record WHY a file closed, and 'expired' is not 'cancelled'."
        ),
    ),
)


#: canonical status -> spec
STATUS_REGISTRY: dict[CanonicalStatus, StatusSpec] = {
    spec.status: spec for spec in (*_SPECS, *_CLOSED_SPECS, *_OFF_MODEL_SPECS)
}


# ---------------------------------------------------------------------------
# Legacy (engine) -> canonical mapping
# ---------------------------------------------------------------------------

#: Every value of the ``platform_application_status`` PG enum -> canonical status.
#: Derived from the registry so the two can never drift.
LEGACY_TO_CANONICAL: dict[str, CanonicalStatus] = {
    engine: spec.status
    for spec in STATUS_REGISTRY.values()
    for engine in spec.engine_statuses
}

#: The canonical status -> the engine value NEW transitions write for it.
#: (First engine value of the spec: the canonical one; later entries are legacy
#: aliases that still resolve inbound.)
CANONICAL_TO_ENGINE: dict[CanonicalStatus, str] = {
    spec.status: spec.engine_statuses[0]
    for spec in STATUS_REGISTRY.values()
    if spec.engine_statuses
}


def canonical_for(engine_status: str) -> Optional[CanonicalStatus]:
    """Map a persisted ``platform_application_status`` value to Dave's status.

    Returns ``None`` for an unknown value rather than raising: an enum addition
    must never 500 a queue or a dashboard.
    """
    return LEGACY_TO_CANONICAL.get(engine_status)


def spec_for(engine_status: str) -> Optional[StatusSpec]:
    """The full registry row for a persisted status value (``None`` if unknown)."""
    canonical = canonical_for(engine_status)
    return STATUS_REGISTRY[canonical] if canonical is not None else None


def actions_for(engine_status: str) -> tuple[Action, ...]:
    """Permitted actions for a persisted status value — what the UI may render."""
    spec = spec_for(engine_status)
    return spec.actions if spec is not None else ()


def workplaces_for(engine_status: str) -> tuple[Workplace, ...]:
    """The workplace(s) that own an application in this status."""
    spec = spec_for(engine_status)
    return spec.workplaces if spec is not None else ()


def is_action_permitted(engine_status: str, action: Action) -> bool:
    """True when ``action`` is listed for this status in Dave's table."""
    return action in actions_for(engine_status)


def is_terminal(engine_status: str) -> bool:
    """True when the status is a closed/terminal state (no forward transition)."""
    spec = spec_for(engine_status)
    return bool(spec is not None and spec.is_terminal)


def registry_payload() -> list[dict]:
    """The whole registry, JSON-serializable, ordered by flow position."""
    return [
        spec.to_dict()
        for spec in sorted(STATUS_REGISTRY.values(), key=lambda s: (s.order, s.label))
    ]
