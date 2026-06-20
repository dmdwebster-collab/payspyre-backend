"""Unit tests for RealBureauAdapter — respx mocks httpx (NO network, NO DB).

Covers:
* a sample Equifax response maps to the flow's BureauResult rich_payload
  (score, bankruptcy, fraud_signals) with result="passed";
* transient errors (timeout, 5xx, 429, rate-limit) -> result="unknown";
* permanent errors (4xx auth/bad-request) -> result="failed";
* no PII (SIN / DOB / postal / name / raw response) leaks into the result or
  raised error messages.
"""
import asyncio
import uuid

import httpx
import pytest
import respx

from app.services.adapters.base import PatientProfile
from app.services.verifications.bureau_adapter import (
    BureauCredentialsError,
    BureauPullRequest,
    RealBureauAdapter,
)

_EQUIFAX_URL = "https://api.equifax.ca/v1/credit/report"

# PII tokens that must never appear in results or error messages.
_SIN = "789"
_DOB = "1990-04-15"
_POSTAL = "K1A0B1"
_FIRST = "Jane"
_LAST = "Borrower"


class _SettingsRow:
    """Lightweight stand-in for a PlatformIntegrationSettings row."""

    def __init__(self, secrets):
        self.secrets = secrets


def _req() -> BureauPullRequest:
    return BureauPullRequest(
        sin_last_3=_SIN,
        date_of_birth=_DOB,
        postal_code=_POSTAL,
        first_name=_FIRST,
        last_name=_LAST,
    )


def _adapter(secrets=None, **kw) -> RealBureauAdapter:
    return RealBureauAdapter(
        bureau="equifax",
        settings_row=_SettingsRow(secrets or {"api_key": "eqfx-test-key"}),
        pull_request=_req(),
        use_cache=False,
        **kw,
    )


def _patient() -> PatientProfile:
    return PatientProfile(patient_id=uuid.uuid4())


def _sample_equifax_raw(score=712, bankruptcy=False, collections=False):
    """Shape matching credit_bureau.EquifaxClient._parse_equifax_response inputs."""
    trades = [
        {
            "account_type": "revolving",
            "balance": 500,
            "credit_limit": 5000,
            "payment_history": "111111",
            "date_opened": "2018-01-01",
            "status": "collection" if collections else "current",
        }
    ]
    public_records = (
        [{"public_record": {"type": "bankruptcy"}}] if bankruptcy else []
    )
    return {
        "score": {"value": score, "min": 300, "max": 900},
        "bureau_data": {
            "trades": trades,
            "inquiries": [],
            "public_records": public_records,
        },
    }


# --------------------------------------------------------------------------
# credential injection
# --------------------------------------------------------------------------


def test_missing_api_key_raises_credentials_error():
    with pytest.raises(BureauCredentialsError):
        RealBureauAdapter(
            bureau="equifax",
            settings_row=_SettingsRow({}),
            pull_request=_req(),
        )


def test_unsupported_bureau_raises():
    with pytest.raises(BureauCredentialsError):
        RealBureauAdapter(
            bureau="experian",
            settings_row=_SettingsRow({"api_key": "x"}),
            pull_request=_req(),
        )


# --------------------------------------------------------------------------
# happy path mapping
# --------------------------------------------------------------------------


class TestMapping:
    @respx.mock
    def test_maps_score_and_result_passed(self):
        respx.post(_EQUIFAX_URL).mock(
            return_value=httpx.Response(200, json=_sample_equifax_raw(score=712))
        )
        result = asyncio.run(_adapter().soft_pull(_patient()))
        assert result.result == "passed"
        assert result.score == 712
        assert result.pull_type == "soft"
        assert result.vendor == "equifax"
        assert result.confidence == 1.0

    @respx.mock
    def test_hard_pull_sets_pull_type(self):
        respx.post(_EQUIFAX_URL).mock(
            return_value=httpx.Response(200, json=_sample_equifax_raw())
        )
        result = asyncio.run(_adapter().hard_pull(_patient()))
        assert result.pull_type == "hard"

    @respx.mock
    def test_bankruptcy_flag_propagates(self):
        respx.post(_EQUIFAX_URL).mock(
            return_value=httpx.Response(
                200, json=_sample_equifax_raw(bankruptcy=True)
            )
        )
        result = asyncio.run(_adapter().soft_pull(_patient()))
        assert result.bankruptcy is True
        assert result.fraud_signals["identity_high_risk"] is True

    @respx.mock
    def test_collections_sets_identity_high_risk(self):
        respx.post(_EQUIFAX_URL).mock(
            return_value=httpx.Response(
                200, json=_sample_equifax_raw(collections=True)
            )
        )
        result = asyncio.run(_adapter().soft_pull(_patient()))
        assert result.fraud_signals["identity_high_risk"] is True
        assert result.fraud_signals["has_collections"] is True

    @respx.mock
    def test_no_score_is_unknown(self):
        # 2xx but the bureau returned no score value.
        respx.post(_EQUIFAX_URL).mock(
            return_value=httpx.Response(
                200, json={"score": {}, "bureau_data": {"trades": []}}
            )
        )
        result = asyncio.run(_adapter().soft_pull(_patient()))
        assert result.result == "unknown"

    @respx.mock
    def test_raw_response_not_in_result(self):
        # raw_response (full PII report) must be dropped from BureauResult.
        respx.post(_EQUIFAX_URL).mock(
            return_value=httpx.Response(200, json=_sample_equifax_raw())
        )
        result = asyncio.run(_adapter().soft_pull(_patient()))
        assert "raw_response" not in result.fraud_signals


# --------------------------------------------------------------------------
# error classification
# --------------------------------------------------------------------------


class TestErrorClassification:
    @respx.mock
    def test_5xx_is_transient_unknown(self):
        respx.post(_EQUIFAX_URL).mock(return_value=httpx.Response(500))
        result = asyncio.run(_adapter().soft_pull(_patient()))
        assert result.result == "unknown"
        assert result.score == 0

    @respx.mock
    def test_429_is_transient_unknown(self):
        respx.post(_EQUIFAX_URL).mock(return_value=httpx.Response(429))
        result = asyncio.run(_adapter().soft_pull(_patient()))
        assert result.result == "unknown"

    @respx.mock
    def test_401_is_permanent_failed(self):
        respx.post(_EQUIFAX_URL).mock(
            return_value=httpx.Response(401, json={"detail": "unauthorized"})
        )
        result = asyncio.run(_adapter().soft_pull(_patient()))
        assert result.result == "failed"

    @respx.mock
    def test_400_is_permanent_failed(self):
        respx.post(_EQUIFAX_URL).mock(return_value=httpx.Response(400))
        result = asyncio.run(_adapter().soft_pull(_patient()))
        assert result.result == "failed"

    @respx.mock
    def test_timeout_is_transient_unknown(self):
        respx.post(_EQUIFAX_URL).mock(
            side_effect=httpx.ConnectTimeout("timed out")
        )
        result = asyncio.run(_adapter().soft_pull(_patient()))
        assert result.result == "unknown"

    @respx.mock
    def test_transport_error_is_transient_unknown(self):
        respx.post(_EQUIFAX_URL).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        result = asyncio.run(_adapter().soft_pull(_patient()))
        assert result.result == "unknown"


# --------------------------------------------------------------------------
# no PII leakage
# --------------------------------------------------------------------------


class TestNoPII:
    def _assert_no_pii(self, blob: str):
        for token in (_SIN, _DOB, _POSTAL, _FIRST, _LAST):
            assert token not in blob

    @respx.mock
    def test_no_pii_in_successful_result(self):
        respx.post(_EQUIFAX_URL).mock(
            return_value=httpx.Response(200, json=_sample_equifax_raw())
        )
        result = asyncio.run(_adapter().soft_pull(_patient()))
        self._assert_no_pii(repr(result))

    @respx.mock
    def test_no_pii_in_error_result(self):
        respx.post(_EQUIFAX_URL).mock(return_value=httpx.Response(500))
        result = asyncio.run(_adapter().soft_pull(_patient()))
        self._assert_no_pii(repr(result))

    @respx.mock
    def test_no_pii_in_log_records(self, caplog):
        respx.post(_EQUIFAX_URL).mock(return_value=httpx.Response(401))
        with caplog.at_level("WARNING"):
            asyncio.run(_adapter().soft_pull(_patient()))
        self._assert_no_pii(caplog.text)
