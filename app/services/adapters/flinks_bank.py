"""Real Flinks bank-link adapter — P7.2 (outbound / initiate only).

Flinks uses a Connect-first model: the patient authenticates their bank in the
Flinks Connect iframe, which redirects back with a ``LoginId``. ``initiate()``
therefore makes **no outbound HTTP call** — it just generates the Connect URL
for the patient. The ``LoginId`` and subsequent ``GetAccountsDetail`` payload
arrive via the Flinks webhook; receiving + normalizing them is **P7.2b**.

The ABC method ``link_account()`` raises ``NotImplementedError`` for the same
reason ``DiditVerificationAdapter.verify_identity`` does — replay adapters
reconstruct the ``BankAccountSummary`` from the stored event payload.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx  # imported for the future /Authorize HTTP path (P7.2b); not used at initiate

from app.services.adapters.base import (
    BankAccountSummary,
    BankAdapter,
    PatientProfile,
)


class FlinksAPIError(Exception):
    """Reserved for non-2xx responses from Flinks HTTP calls in P7.2b."""


# The iframe origin is separate from the JSON API origin (FLINKS_API_BASE_URL).
_IFRAME_BASE = "https://toolbox-iframe.private.fin.ag"

# Placeholder applicant-facing callback that receives the post-Connect redirect.
# P7.2b will resolve this against the real frontend URL; for P7.2 (initiate-only)
# the application_id is encoded in the query so post-redirect can correlate.
_REDIRECT_URL_BASE = "https://app.payspyre.com/flinks/callback"


@dataclass(frozen=True)
class FlinksInitiationResult:
    login_id: Optional[str]   # None at initiate — populated post-redirect (P7.2b)
    connect_url: str          # iframe URL; stored as vendor_session_ref for correlation
    cost_cents: int = 0


class FlinksBankAdapter(BankAdapter):
    """Generates the Flinks Connect iframe URL. The /Authorize HTTP path is P7.2b."""

    def __init__(self, api_key: str, api_base_url: str, customer_id: str) -> None:
        self._api_key = api_key
        self._api_base_url = api_base_url.rstrip("/")
        self._customer_id = customer_id

    def initiate(
        self,
        application_id: str,
        patient: PatientProfile,
        cost_cents: int = 0,
    ) -> FlinksInitiationResult:
        """Build the Flinks Connect URL the patient is redirected to. No HTTP at this step."""
        # Encode application_id in both the redirect (for the browser callback) and
        # the Flinks ``tag`` parameter (P7.2b correlation bridge). Per Flinks docs,
        # the Tag is echoed back verbatim in the webhook body, letting us look up
        # PlatformVerification by application_id when the webhook arrives — the
        # equivalent of Didit's ``vendor_data`` echo.
        # Ref: https://help.flinks.com/.../43000436150-using-tag-and-webhooks-with-flinks-connect
        redirect = f"{_REDIRECT_URL_BASE}?{urlencode({'application_id': application_id})}"
        query = urlencode({
            "customerId": self._customer_id,
            "redirectUrl": redirect,
            "tag": application_id,
        })
        connect_url = f"{_IFRAME_BASE}/v2/?{query}"
        return FlinksInitiationResult(
            login_id=None,           # populated only after Connect completes
            connect_url=connect_url,
            cost_cents=cost_cents,
        )

    async def link_account(self, patient: PatientProfile) -> BankAccountSummary:
        # The real path is webhook-delivered. Same pattern as DiditVerificationAdapter.
        raise NotImplementedError(
            "FlinksBankAdapter.link_account is not called in the real path; "
            "results arrive via the Flinks webhook (P7.2b)."
        )
