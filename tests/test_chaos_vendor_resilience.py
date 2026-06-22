"""L7 — chaos / resilience: vendor-failure injection.

Two layers of the resilience contract, exercised by injecting faults:

1. ADAPTER (the real HTTP boundary — the credit bureau): the FULL spectrum of
   network/HTTP failures a vendor outage produces must degrade to result="unknown"
   (transient) — never crash the process, never fabricate an auto-"failed". Permanent
   client errors (4xx) map to "failed". Extends the bureau adapter's spot-checks into
   a systematic sweep (timeouts, transport errors, protocol errors, 5xx, 429).

2. SYSTEM (the decision engine): the money-safety invariant — a vendor being DOWN
   (→ an "unknown" verification) must send the application to MANUAL_REVIEW, never an
   auto-approval and never an auto-decline. A *confirmed* hard disqualifier
   (bankruptcy / below-floor score) still declines: an unrelated outage must not
   rescue it. This is what stops a credit-bureau outage from silently approving (or
   wrongly declining) real loans.

respx mocks httpx — NO network, NO DB.
"""
import asyncio
import uuid

import httpx
import pytest
import respx

from app.services.adapters.base import PatientProfile
from app.services.flow_engine import _ApplicantOutcome, _resolve_decision
from app.services.verifications.bureau_adapter import BureauPullRequest, RealBureauAdapter

_EQUIFAX_URL = "https://api.equifax.ca/v1/credit/report"


class _SettingsRow:
    def __init__(self, secrets):
        self.secrets = secrets


def _adapter() -> RealBureauAdapter:
    return RealBureauAdapter(
        bureau="equifax",
        settings_row=_SettingsRow({"api_key": "eqfx-test-key"}),
        pull_request=BureauPullRequest(
            sin_last_3="789",
            date_of_birth="1990-04-15",
            postal_code="K1A0B1",
            first_name="Jane",
            last_name="Borrower",
        ),
        use_cache=False,
    )


def _patient() -> PatientProfile:
    return PatientProfile(patient_id=uuid.uuid4())


# --- 1. Adapter: the full vendor-outage failure spectrum -> "unknown" -------

# Every way a network/transport call can fail mid-flight. None may raise out of the
# adapter; all are transient and must become "unknown" (engine -> manual_review).
_TRANSIENT_EXCEPTIONS = [
    httpx.ConnectTimeout("connect timed out"),
    httpx.ReadTimeout("read timed out"),
    httpx.WriteTimeout("write timed out"),
    httpx.PoolTimeout("pool timed out"),
    httpx.ConnectError("connection refused"),
    httpx.ReadError("peer reset"),
    httpx.WriteError("broken pipe"),
    httpx.RemoteProtocolError("server disconnected without response"),
]


@pytest.mark.parametrize("exc", _TRANSIENT_EXCEPTIONS, ids=lambda e: type(e).__name__)
@respx.mock
def test_every_transient_network_failure_degrades_to_unknown(exc):
    respx.post(_EQUIFAX_URL).mock(side_effect=exc)
    result = asyncio.run(_adapter().soft_pull(_patient()))
    assert result.result == "unknown", f"{type(exc).__name__} should be transient"


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
@respx.mock
def test_transient_http_status_degrades_to_unknown(status):
    respx.post(_EQUIFAX_URL).mock(return_value=httpx.Response(status))
    result = asyncio.run(_adapter().soft_pull(_patient()))
    assert result.result == "unknown", f"HTTP {status} should be transient"


@pytest.mark.parametrize("status", [400, 401, 403, 404])
@respx.mock
def test_permanent_client_errors_are_failed_not_unknown(status):
    # A permanent 4xx (bad request / unauthorized) is NOT a transient outage — it must
    # map to "failed", so it isn't silently retried forever as manual_review.
    respx.post(_EQUIFAX_URL).mock(return_value=httpx.Response(status, json={"e": "x"}))
    result = asyncio.run(_adapter().soft_pull(_patient()))
    assert result.result == "failed", f"HTTP {status} should be permanent"


@respx.mock
def test_malformed_success_body_does_not_crash():
    # A 200 with a garbage body (vendor returning broken JSON) must not raise; it
    # degrades rather than taking the process down.
    respx.post(_EQUIFAX_URL).mock(return_value=httpx.Response(200, content=b"<<not json>>"))
    result = asyncio.run(_adapter().soft_pull(_patient()))
    assert result.result in ("unknown", "failed")


# --- 2. System: a vendor outage is SAFE at the decision (manual_review) ------


def _outcome(score):
    # band = the manual-review window [600, 679]; >=680 approves, <600 declines.
    return _ApplicantOutcome(
        decision="",
        effective_score=score,
        reasons=[],
        verifications=[],
        events=[],
        band={"min": 600, "max": 679},
    )


def _decide(score, **flags):
    base = dict(bankruptcy=False, fraud_review=False, has_unknown=False,
               has_failed_required=False, identity_manual_review=False)
    base.update(flags)
    return _resolve_decision(_outcome(score), **base)


def test_clean_applicant_with_good_score_approves_control():
    assert _decide(720) == "approved"


def test_vendor_outage_never_auto_approves():
    # Good score, but a verification came back "unknown" because a vendor was DOWN.
    # Must go to manual_review, NOT approved.
    assert _decide(720, has_unknown=True) == "manual_review"


def test_failed_required_verification_forces_manual_review():
    assert _decide(720, has_failed_required=True) == "manual_review"


def test_fraud_signal_forces_manual_review():
    assert _decide(720, fraud_review=True) == "manual_review"


def test_identity_review_forces_manual_review():
    assert _decide(720, identity_manual_review=True) == "manual_review"


def test_confirmed_bankruptcy_still_declines_despite_outage():
    # A real disqualifier is final — an unrelated vendor outage must NOT rescue it
    # up to manual_review.
    assert _decide(720, bankruptcy=True, has_unknown=True) == "declined"


def test_below_floor_score_still_declines_despite_outage():
    assert _decide(540, has_unknown=True) == "declined"
