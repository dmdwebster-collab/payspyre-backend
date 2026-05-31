"""Unit tests for vendor → orchestrator-handoff translators (P7.2b).

Pure function tests — no DB, no FastAPI. Synthetic vendor payloads in, a
``TranslateResult`` out. The integration tests
(``test_vendor_webhooks_didit.py`` / ``..._flinks.py``) cover end-to-end wiring;
these tests cover the mapping logic in isolation.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.api.webhooks.v1.schemas import DiditWebhookPayload, FlinksWebhookPayload
from app.services.webhooks.translators import (
    TranslatorError,
    translate_didit_payload,
    translate_flinks_payload,
)


_APP_ID = "11111111-2222-3333-4444-555555555555"


# ===========================================================================
# Didit
# ===========================================================================


def _didit_payload(**overrides) -> DiditWebhookPayload:
    base = {
        "event_id": "ev-1",
        "webhook_type": "status.updated",
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
        "session_id": "sess-1",
        "status": "Approved",
        "vendor_data": _APP_ID,
        "decision": {
            "face_matches": [{"score": 96.1}],
            "id_verifications": [{"document_type": "Passport"}],
            "warnings": [],
        },
    }
    base.update(overrides)
    return DiditWebhookPayload(**base)


class TestDiditTerminal:
    def test_approved_maps_to_passed(self):
        r = translate_didit_payload(_didit_payload(status="Approved"))
        assert r.result == "passed"
        assert r.verification_type == "kyc_id"
        assert r.application_id is not None
        assert str(r.application_id) == _APP_ID
        assert r.vendor_event_id == "ev-1"
        assert r.skip is False

    def test_declined_maps_to_failed(self):
        r = translate_didit_payload(_didit_payload(status="Declined"))
        assert r.result == "failed"

    @pytest.mark.parametrize("status", ["Expired", "KYC Expired", "Abandoned"])
    def test_terminal_failure_statuses(self, status):
        r = translate_didit_payload(_didit_payload(status=status))
        assert r.result == "failed"


class TestDiditNonTerminal:
    @pytest.mark.parametrize(
        "status", ["In Progress", "Not Started", "Resubmitted"]
    )
    def test_non_terminal_skips(self, status):
        # P7.5: "In Review" used to be in this set but is now terminal
        # (result="manual_review") — see TestDiditManualReview.
        r = translate_didit_payload(_didit_payload(status=status))
        assert r.skip is True
        assert r.result is None
        assert r.rich_payload is None
        # vendor_event_id still set so the endpoint can record receipt.
        assert r.vendor_event_id == "ev-1"


class TestDiditManualReview:
    """P7.5 — Didit "In Review" → result="manual_review" terminal."""

    def test_in_review_maps_to_manual_review_result(self):
        r = translate_didit_payload(_didit_payload(status="In Review"))
        assert r.skip is False
        assert r.result == "manual_review"
        assert r.verification_type == "kyc_id"
        assert r.vendor_event_id == "ev-1"

    def test_in_review_carries_rich_payload(self):
        # Same rich_payload extraction as the passed/failed paths so replay
        # adapters and ops dashboards see consistent fields.
        r = translate_didit_payload(_didit_payload(status="In Review"))
        assert r.rich_payload is not None
        assert r.rich_payload["result"] == "manual_review"
        assert r.rich_payload["vendor"] == "didit"
        assert r.rich_payload["didit_status"] == "In Review"


class TestDiditRichPayload:
    def test_face_match_score_normalized_to_unit_interval(self):
        r = translate_didit_payload(
            _didit_payload(decision={"face_matches": [{"score": 96.1}]})
        )
        assert r.rich_payload["confidence"] == pytest.approx(0.961)

    def test_missing_face_match_defaults_confidence_to_one(self):
        r = translate_didit_payload(_didit_payload(decision={}))
        assert r.rich_payload["confidence"] == 1.0

    def test_vendor_session_ref_is_session_id(self):
        r = translate_didit_payload(_didit_payload(session_id="sess-X"))
        assert r.rich_payload["vendor_session_ref"] == "sess-X"
        assert r.vendor_session_ref == "sess-X"

    def test_vendor_field_is_didit(self):
        r = translate_didit_payload(_didit_payload())
        assert r.rich_payload["vendor"] == "didit"

    def test_method_is_document(self):
        r = translate_didit_payload(_didit_payload())
        assert r.rich_payload["method"] == "document"

    def test_warnings_passed_through(self):
        warnings = [{"risk": "DOCUMENT_EXPIRED"}]
        r = translate_didit_payload(
            _didit_payload(decision={"warnings": warnings})
        )
        assert r.rich_payload["warnings"] == warnings

    def test_document_type_extracted(self):
        r = translate_didit_payload(
            _didit_payload(decision={"id_verifications": [{"document_type": "Passport"}]})
        )
        assert r.rich_payload["document_type"] == "Passport"


class TestDiditValidation:
    def test_missing_vendor_data_raises(self):
        with pytest.raises(TranslatorError, match="vendor_data"):
            translate_didit_payload(_didit_payload(vendor_data=None))

    def test_non_uuid_vendor_data_raises(self):
        with pytest.raises(TranslatorError, match="not a UUID"):
            translate_didit_payload(_didit_payload(vendor_data="not-a-uuid"))


# ===========================================================================
# Flinks
# ===========================================================================


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _txn(date_: date, *, credit: float = 0, debit: float = 0, description: str = "") -> dict:
    return {
        "Date": date_.strftime("%Y-%m-%d"),
        "Description": description,
        "Credit": credit,
        "Debit": debit,
        "Balance": 0,
    }


def _flinks_payload(**overrides) -> FlinksWebhookPayload:
    base = {
        "ResponseType": "GetAccountsDetail",
        "HttpStatusCode": 200,
        "Login": {"Id": "login-uuid-1"},
        "Tag": _APP_ID,
        "RequestId": "req-uuid-1",
        "Accounts": [
            {
                "Id": "acc-1",
                "Balance": {"Available": 5000.0, "Current": 5234.56, "Limit": 0},
                "Transactions": [],
            }
        ],
    }
    base.update(overrides)
    return FlinksWebhookPayload(**base)


class TestFlinksTerminal:
    def test_accounts_detail_200_passes(self):
        r = translate_flinks_payload(_flinks_payload())
        assert r.result == "passed"
        assert r.verification_type == "bank_link"
        assert r.application_id is not None
        assert str(r.application_id) == _APP_ID
        assert r.vendor_event_id == "flinks:login-uuid-1:GetAccountsDetail"

    def test_non_200_status_fails(self):
        r = translate_flinks_payload(_flinks_payload(HttpStatusCode=500))
        assert r.result == "failed"

    def test_empty_accounts_fails(self):
        r = translate_flinks_payload(_flinks_payload(Accounts=[]))
        assert r.result == "failed"


class TestFlinksNonTerminal:
    def test_kyc_response_type_skips(self):
        r = translate_flinks_payload(_flinks_payload(ResponseType="KYC"))
        assert r.skip is True
        assert r.vendor_event_id == "flinks:login-uuid-1:KYC"

    def test_skip_still_emits_vendor_session_ref(self):
        r = translate_flinks_payload(_flinks_payload(ResponseType="KYC"))
        # Even a KYC skip must let the endpoint upgrade vendor_session_ref —
        # it's the only way the placeholder Connect URL gets replaced.
        assert r.vendor_session_ref == "login-uuid-1"


class TestFlinksRichPayloadArithmetic:
    def test_avg_balance_sums_account_balance_current(self):
        accounts = [
            {"Id": "a", "Balance": {"Current": 100.0}, "Transactions": []},
            {"Id": "b", "Balance": {"Current": 250.50}, "Transactions": []},
        ]
        r = translate_flinks_payload(_flinks_payload(Accounts=accounts))
        # (100.00 + 250.50) * 100 = 35050 cents
        assert r.rich_payload["avg_balance_cents"] == 35050

    def test_monthly_income_sums_credits_in_last_30_days(self):
        today = _today()
        accounts = [
            {
                "Id": "a",
                "Balance": {"Current": 0},
                "Transactions": [
                    _txn(today - timedelta(days=2), credit=1000.00),
                    _txn(today - timedelta(days=15), credit=500.00),
                    _txn(today - timedelta(days=60), credit=9999.99),  # outside window
                ],
            }
        ]
        r = translate_flinks_payload(_flinks_payload(Accounts=accounts))
        assert r.rich_payload["monthly_income_cents"] == 150000  # 1500.00 in cents

    def test_nsf_counted_within_90_days(self):
        today = _today()
        accounts = [
            {
                "Id": "a",
                "Balance": {"Current": 0},
                "Transactions": [
                    _txn(today - timedelta(days=5), description="NSF FEE"),
                    _txn(today - timedelta(days=45), description="nsf fee retry"),
                    _txn(today - timedelta(days=120), description="NSF FEE"),  # outside
                ],
            }
        ]
        r = translate_flinks_payload(_flinks_payload(Accounts=accounts))
        assert r.rich_payload["nsf_count_90d"] == 2

    def test_account_age_from_earliest_transaction(self):
        today = _today()
        accounts = [
            {
                "Id": "a",
                "Balance": {"Current": 0},
                "Transactions": [
                    _txn(today - timedelta(days=400), credit=10),  # ~13 months old
                    _txn(today - timedelta(days=5), credit=10),
                ],
            }
        ]
        r = translate_flinks_payload(_flinks_payload(Accounts=accounts))
        assert r.rich_payload["account_age_months"] == 400 // 30  # 13

    def test_confidence_defaults_to_one(self):
        r = translate_flinks_payload(_flinks_payload())
        assert r.rich_payload["confidence"] == 1.0

    def test_vendor_field_is_flinks(self):
        r = translate_flinks_payload(_flinks_payload())
        assert r.rich_payload["vendor"] == "flinks"


class TestFlinksValidation:
    def test_missing_tag_raises(self):
        with pytest.raises(TranslatorError, match="missing Tag"):
            translate_flinks_payload(_flinks_payload(Tag=None))

    def test_non_uuid_tag_raises(self):
        with pytest.raises(TranslatorError, match="not a UUID"):
            translate_flinks_payload(_flinks_payload(Tag="not-a-uuid"))


class TestFlinksPIIStripping:
    def test_holder_section_stripped_from_raw_payload(self):
        accounts = [
            {
                "Id": "a",
                "Balance": {"Current": 100.0},
                "Holder": {"Name": "Jane Doe", "Email": "jane@example.com"},
                "Transactions": [],
            }
        ]
        r = translate_flinks_payload(_flinks_payload(Accounts=accounts))
        raw_accounts = r.rich_payload["raw_payload"]["Accounts"]
        assert "Holder" not in raw_accounts[0]
        # but everything else still there for replay
        assert raw_accounts[0]["Id"] == "a"
