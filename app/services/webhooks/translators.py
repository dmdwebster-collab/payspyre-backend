"""Real-vendor webhook → orchestrator-handoff translators (P7.2b).

Each translator turns a parsed vendor payload into the four things the
``FlowOrchestrator.handle_verification_result`` call needs:

- the **application_id** (UUID) — extracted from the vendor's correlation field
  (Didit's ``vendor_data``, Flinks's ``Tag``)
- the **vendor_event_id** (str) — the idempotency nonce written into
  ``platform_events.payload.vendor_event_id``
- the **result** (``"passed"`` / ``"failed"`` / ``"manual_review"``) — derived
  from vendor status. ``"manual_review"`` (added in P7.5) carries Didit's
  "In Review" verdict; the orchestrator persists it as
  ``verification.status = "manual_review"``.
- the **rich_payload** (dict) — the exact keys the replay adapters consume,
  documented in ``app/services/verifications/replay_adapters.py``

Translators are pure functions (no DB, no HTTP, no logging side effects) and
the only place that knows what real Didit/Flinks payloads look like.

Special "no-op" return (``TranslateResult.skip == True``) signals the endpoint
to acknowledge the delivery with 202 ``status=ignored`` without calling the
orchestrator — used for non-terminal Didit statuses ("In Progress", "Not
Started", "Resubmitted") where Didit fires the webhook for status transitions
we don't care about.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID

from app.api.webhooks.v1.schemas import DiditWebhookPayload, FlinksWebhookPayload
from app.services.bank.transaction_analysis import analyze_accounts


class TranslatorError(Exception):
    """Raised when a payload is well-signed but unusable (missing correlation, bad
    vendor_data shape, etc.). The endpoint maps this to HTTP 400."""


@dataclass(frozen=True)
class TranslateResult:
    """Outcome of a translation.

    ``skip=True`` signals the endpoint to 202-ack without calling the orchestrator
    (non-terminal status updates). In that case ``application_id`` / ``result`` /
    ``rich_payload`` / ``verification_type`` are not used.
    """
    application_id: Optional[UUID]
    verification_type: Optional[str]               # platform_verification_type enum value
    vendor_event_id: Optional[str]
    result: Optional[Literal["passed", "failed", "manual_review"]]
    rich_payload: Optional[dict[str, Any]]
    vendor_session_ref: Optional[str] = None       # used by Flinks to upgrade the placeholder
    skip: bool = False


# ---------------------------------------------------------------------------
# Didit (kyc_id)
# ---------------------------------------------------------------------------

# Status → terminal result. Anything not in any of these three sets is "skip"
# (non-terminal — Didit fires the webhook for non-terminal transitions too).
_DIDIT_PASSED_STATUSES = {"Approved"}
_DIDIT_FAILED_STATUSES = {"Declined", "Expired", "KYC Expired", "Abandoned"}
_DIDIT_MANUAL_REVIEW_STATUSES = {"In Review"}
# Non-terminal (acknowledge + skip orchestrator call):
#   "In Progress" / "Not Started" / "Resubmitted"
# P7.5: "In Review" is now terminal — routes to result="manual_review", which
# the orchestrator persists as verification.status = "manual_review" via the
# platform_verification_status enum value added in migration 024.


def translate_didit_payload(payload: DiditWebhookPayload) -> TranslateResult:
    """Map a parsed Didit ``status.updated`` webhook to the orchestrator handoff."""
    if payload.vendor_data is None:
        raise TranslatorError(
            "Didit webhook missing vendor_data — cannot correlate to application"
        )
    try:
        application_id = UUID(payload.vendor_data)
    except (ValueError, TypeError) as exc:
        raise TranslatorError(
            f"Didit vendor_data is not a UUID: {payload.vendor_data!r}"
        ) from exc

    result: Optional[Literal["passed", "failed", "manual_review"]]
    if payload.status in _DIDIT_PASSED_STATUSES:
        result = "passed"
    elif payload.status in _DIDIT_FAILED_STATUSES:
        result = "failed"
    elif payload.status in _DIDIT_MANUAL_REVIEW_STATUSES:
        result = "manual_review"
    else:
        # Non-terminal — acknowledge + skip orchestrator call.
        return TranslateResult(
            application_id=application_id,
            verification_type="kyc_id",
            vendor_event_id=payload.event_id,
            result=None,
            rich_payload=None,
            skip=True,
        )

    decision: dict[str, Any] = payload.decision or {}
    face_matches = decision.get("face_matches") or []
    first_face = face_matches[0] if face_matches else {}
    # Didit ``face_matches[].score`` is 0–100; replay adapters expect 0.0–1.0.
    raw_score = first_face.get("score")
    confidence = float(raw_score) / 100.0 if isinstance(raw_score, (int, float)) else 1.0

    id_verifications = decision.get("id_verifications") or []
    first_id = id_verifications[0] if id_verifications else {}
    document_type = first_id.get("document_type")

    warnings = decision.get("warnings") or []

    rich_payload: dict[str, Any] = {
        "method": "document",
        "result": result,
        "confidence": confidence,
        "vendor": "didit",
        "vendor_session_ref": payload.session_id,
        # Audit-only non-PII metadata, ignored by the replay adapter but kept in
        # the verification_completed event for ops. The full Didit ``decision``
        # (legal name / DOB / document number / face-image URLs) is deliberately
        # NOT stored here: platform_events is WORM (migration 021), so any PII
        # written into it can never be redacted — Hard Rule #6. A raw vendor blob,
        # if ever needed, belongs in an encrypted at-rest store keyed by
        # verification_id (spec §6), never in the event log.
        "document_type": document_type,
        "warnings": warnings,
        "didit_status": payload.status,
    }

    return TranslateResult(
        application_id=application_id,
        verification_type="kyc_id",
        vendor_event_id=payload.event_id,
        result=result,
        rich_payload=rich_payload,
        vendor_session_ref=payload.session_id,
    )


# ---------------------------------------------------------------------------
# Flinks (bank_link)
# ---------------------------------------------------------------------------


def translate_flinks_payload(payload: FlinksWebhookPayload) -> TranslateResult:
    """Map a parsed Flinks Connect webhook to the orchestrator handoff.

    Only ``ResponseType == "GetAccountsDetail"`` produces a terminal result.
    The ``"KYC"`` payload (delivered first, holders-only) is acknowledged and
    skipped — bank_link is decided once the account data arrives. Other
    response types are skipped the same way.
    """
    if payload.Tag is None:
        raise TranslatorError(
            "Flinks webhook missing Tag — cannot correlate to application "
            "(initiate must include tag=<application_id>)"
        )
    try:
        application_id = UUID(payload.Tag)
    except (ValueError, TypeError) as exc:
        raise TranslatorError(
            f"Flinks Tag is not a UUID: {payload.Tag!r}"
        ) from exc

    login_id = payload.Login.Id
    # Composite nonce: each (LoginId, ResponseType) is delivered once per session;
    # Flinks retries reuse the same key, which the orchestrator-side nonce check
    # then sees as an idempotent replay.
    vendor_event_id = f"flinks:{login_id}:{payload.ResponseType}"

    if payload.ResponseType != "GetAccountsDetail":
        # KYC, Investments, etc. — acknowledge but don't finalize bank_link yet.
        return TranslateResult(
            application_id=application_id,
            verification_type="bank_link",
            vendor_event_id=vendor_event_id,
            result=None,
            rich_payload=None,
            vendor_session_ref=login_id,
            skip=True,
        )

    accounts = payload.Accounts or []
    is_ok = payload.HttpStatusCode == 200 and len(accounts) > 0
    result: Literal["passed", "failed"] = "passed" if is_ok else "failed"

    derived = _derive_bank_metrics(accounts) if is_ok else _empty_bank_metrics()

    rich_payload: dict[str, Any] = {
        "result": result,
        # Keys consumed by ReplayBankAdapter (see replay_adapters.py:90–104).
        "monthly_income_cents": derived["monthly_income_cents"],
        "nsf_count_90d": derived["nsf_count_90d"],
        "account_age_months": derived["account_age_months"],
        "avg_balance_cents": derived["avg_balance_cents"],
        "confidence": 1.0,
        "vendor": "flinks",
        "vendor_session_ref": login_id,
        # Audit-only non-PII metadata. The raw Flinks payload is deliberately NOT
        # stored: it carries transaction descriptions (counterparty names) and the
        # Login object, and platform_events is WORM (migration 021) so any PII
        # written into it can never be redacted — Hard Rule #6. A raw vendor blob,
        # if ever needed, belongs in an encrypted at-rest store keyed by
        # verification_id (spec §6), not in the event log.
        # TODO(p7.2c): replace in-translator arithmetic with a follow-up Flinks
        # Enrich/Attributes API call for proper income / NSF derivation.
        "flinks_response_type": payload.ResponseType,
        "flinks_request_id": payload.RequestId,
    }

    return TranslateResult(
        application_id=application_id,
        verification_type="bank_link",
        vendor_event_id=vendor_event_id,
        result=result,
        rich_payload=rich_payload,
        vendor_session_ref=login_id,
    )


# ---------------------------------------------------------------------------
# Flinks arithmetic helpers (P7.2b — approximation, replaced by Enrich later)
# ---------------------------------------------------------------------------


def _empty_bank_metrics() -> dict[str, int]:
    return {
        "monthly_income_cents": 0,
        "nsf_count_90d": 0,
        "account_age_months": 0,
        "avg_balance_cents": 0,
    }


def _derive_bank_metrics(accounts: list[dict[str, Any]]) -> dict[str, int]:
    """Derive underwriting metrics from raw Flinks ``Accounts[].Transactions[]``.

    Delegates to the transaction-analysis engine (P8.x), which detects RECURRING
    income streams (not a naive sum of credits), word-boundary NSF events, account
    age, and current balance — processing the raw 90-day data ourselves rather than
    paying for Flinks' Enrich/Attributes API. Output keys match the ReplayBankAdapter
    contract: ``monthly_income_cents``, ``nsf_count_90d``, ``account_age_months``,
    ``avg_balance_cents``.
    """
    return analyze_accounts(accounts)


def _parse_flinks_date(value: Any) -> Optional[Any]:
    """Flinks emits ``"YYYY-MM-DD"``; some endpoints emit ISO-8601 with time."""
    if not isinstance(value, str):
        return None
    try:
        # date-only fast path
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
