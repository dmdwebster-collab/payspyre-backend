"""Real-vendor webhook → orchestrator-handoff translators (P7.2b).

Each translator turns a parsed vendor payload into the four things the
``FlowOrchestrator.handle_verification_result`` call needs:

- the **application_id** (UUID) — extracted from the vendor's correlation field
  (Didit's ``vendor_data``, Flinks's ``Tag``)
- the **vendor_event_id** (str) — the idempotency nonce written into
  ``platform_events.payload.vendor_event_id``
- the **result** (``"passed"`` / ``"failed"``) — derived from vendor status
- the **rich_payload** (dict) — the exact keys the replay adapters consume,
  documented in ``app/services/verifications/replay_adapters.py``

Translators are pure functions (no DB, no HTTP, no logging side effects) and
the only place that knows what real Didit/Flinks payloads look like.

Special "no-op" return (``TranslateResult.skip == True``) signals the endpoint
to acknowledge the delivery with 202 ``status=ignored`` without calling the
orchestrator — used for non-terminal Didit statuses ("In Progress", "Not
Started", "In Review", "Resubmitted") where Didit fires the webhook for
status transitions we don't care about.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID

from app.api.webhooks.v1.schemas import DiditWebhookPayload, FlinksWebhookPayload


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
    result: Optional[Literal["passed", "failed"]]
    rich_payload: Optional[dict[str, Any]]
    vendor_session_ref: Optional[str] = None       # used by Flinks to upgrade the placeholder
    skip: bool = False


# ---------------------------------------------------------------------------
# Didit (kyc_id)
# ---------------------------------------------------------------------------

# Status → terminal result. Anything not in either set is "skip" (non-terminal).
_DIDIT_PASSED_STATUSES = {"Approved"}
_DIDIT_FAILED_STATUSES = {"Declined", "Expired", "KYC Expired", "Abandoned"}
# Non-terminal (acknowledge + wait for a later transition):
#   "In Progress" / "Not Started" / "Resubmitted" / "In Review"
# Note: "In Review" stays non-terminal because the platform_verification_status
# enum has no "manual_review" value (see P7.2b scope §7.4). The verification
# remains ``pending`` until Didit delivers a final Approved / Declined.


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

    if payload.status in _DIDIT_PASSED_STATUSES:
        result: Optional[Literal["passed", "failed"]] = "passed"
    elif payload.status in _DIDIT_FAILED_STATUSES:
        result = "failed"
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
        # Audit-only fields, ignored by the replay adapter but preserved in the
        # verification_completed event for ops / future analysis.
        "document_type": document_type,
        "warnings": warnings,
        "didit_status": payload.status,
        "raw_decision": decision,
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
        # Audit fields. TODO(p7.2c): replace in-translator arithmetic with a
        # follow-up Flinks Enrich/Attributes API call for proper income / NSF
        # derivation (logged in backlog).
        "flinks_response_type": payload.ResponseType,
        "flinks_request_id": payload.RequestId,
        "raw_payload": _strip_pii(payload.model_dump()),
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
    """Walk Flinks ``Accounts[].Transactions[]`` to approximate underwriting metrics.

    Notes on the approximation (matched to ReplayBankAdapter contract):
    - ``monthly_income_cents``: sum of Credit amounts in the trailing 30 days.
      A single account; if multiple, sum across.
    - ``nsf_count_90d``: count of transactions whose Description contains
      "NSF" or " OD" (overdraft) in the trailing 90 days.
    - ``account_age_months``: (now - earliest Transaction.Date) // 30 days.
    - ``avg_balance_cents``: sum of ``Accounts[].Balance.Current`` across accounts
      (proxy for "what's in the account right now").

    Dollars → cents: Flinks reports amounts as decimal dollars
    (e.g. ``"Credit": 1234.56``). We round to int cents.
    """
    now = datetime.now(timezone.utc).date()
    income_30d = 0
    nsf_90d = 0
    earliest_txn = None
    balance_sum_cents = 0

    for account in accounts:
        balance = account.get("Balance") or {}
        current = balance.get("Current")
        if isinstance(current, (int, float)):
            balance_sum_cents += int(round(float(current) * 100))

        for txn in account.get("Transactions") or []:
            txn_date = _parse_flinks_date(txn.get("Date"))
            if txn_date is None:
                continue
            if earliest_txn is None or txn_date < earliest_txn:
                earliest_txn = txn_date

            days_ago = (now - txn_date).days
            description = (txn.get("Description") or "").upper()

            if 0 <= days_ago <= 30:
                credit = txn.get("Credit")
                if isinstance(credit, (int, float)) and credit > 0:
                    income_30d += int(round(float(credit) * 100))

            if 0 <= days_ago <= 90:
                if "NSF" in description or " OD " in f" {description} ":
                    nsf_90d += 1

    if earliest_txn is None:
        account_age_months = 0
    else:
        account_age_months = max(0, (now - earliest_txn).days // 30)

    return {
        "monthly_income_cents": income_30d,
        "nsf_count_90d": nsf_90d,
        "account_age_months": account_age_months,
        "avg_balance_cents": balance_sum_cents,
    }


def _parse_flinks_date(value: Any) -> Optional[Any]:
    """Flinks emits ``"YYYY-MM-DD"``; some endpoints emit ISO-8601 with time."""
    if not isinstance(value, str):
        return None
    try:
        # date-only fast path
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _strip_pii(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop Holder PII (name / address / email / phone) before persisting.

    Hard Rule #6 (no-PII-in-logs/events): the ``rich_payload`` lands in the
    ``verification_completed`` event row, which is queryable across ops; we
    keep balance / transactions for replay but never the holder identity.
    """
    cleaned = dict(payload)
    accounts_raw = cleaned.get("Accounts") or []
    cleaned_accounts: list[dict[str, Any]] = []
    for account in accounts_raw:
        if isinstance(account, dict):
            account_copy = {k: v for k, v in account.items() if k != "Holder"}
            cleaned_accounts.append(account_copy)
    if cleaned_accounts:
        cleaned["Accounts"] = cleaned_accounts
    return cleaned
