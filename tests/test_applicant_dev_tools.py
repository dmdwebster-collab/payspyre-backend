"""The patient journey driven entirely through the public HTTP API + the dev
helpers (no internal orchestrator calls) — proves a browser/demo can complete the
flow end-to-end in mock mode via:
  - GET  /dev/magic-link-code            (surfaces the mock sign-in code)
  - POST /dev/.../verifications/{p}/complete  (simulates a passed vendor result)
"""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.platform.credit_product import PlatformCreditProduct

_BASE = "/api/applicant/v1"
_PURPOSES = ["id_verification", "soft_bureau_pull", "bank_verification", "hard_bureau_pull"]


def _product_id(db: Session) -> uuid.UUID:
    return (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
        .id
    )


class TestApplicantDevTools:
    def test_products_endpoint_lists_seed(self, client: TestClient, db_session: Session):
        resp = client.get(f"{_BASE}/products")
        assert resp.status_code == 200, resp.text
        codes = {p["code"] for p in resp.json()["products"]}
        assert "dental_full_arch_v1" in codes

    def test_full_journey_via_dev_endpoints(self, client: TestClient, db_session: Session):
        # create application
        resp = client.post(
            f"{_BASE}/applications",
            json={
                "patient_profile": {
                    "legal_first_name": "Dev",
                    "email": f"dev-{uuid.uuid4().hex[:8]}@example.com",
                },
                "credit_product_id": str(_product_id(db_session)),
                "requested_amount_cents": 3_000_000,
                "requested_amount_source": "patient",
                "contact_method": "email",
            },
        )
        assert resp.status_code == 201, resp.text
        app_id = resp.json()["application_id"]

        # dev: surface the mock sign-in code, then exchange it for a JWT
        code_resp = client.get(f"{_BASE}/dev/magic-link-code", params={"application_id": app_id})
        assert code_resp.status_code == 200, code_resp.text
        code = code_resp.json()["code"]

        ex = client.post(
            f"{_BASE}/auth/magic-link/exchange", json={"application_id": app_id, "token": code}
        )
        assert ex.status_code == 200, ex.text
        headers = {"Authorization": f"Bearer {ex.json()['jwt']}"}

        # consents (the 4 verification purposes only) + initiate. Policy (Dave,
        # 2026-07): the automated decision is NOT gated behind a discrete consent.
        for p in _PURPOSES:
            assert (
                client.post(f"{_BASE}/applications/{app_id}/consents/{p}", headers=headers).status_code
                == 200
            )
        for p in _PURPOSES:
            assert (
                client.post(
                    f"{_BASE}/applications/{app_id}/verifications/{p}/initiate", headers=headers
                ).status_code
                == 200
            )

        # dev: complete each verification with a passing (720) result
        for p in _PURPOSES:
            c = client.post(
                f"{_BASE}/dev/applications/{app_id}/verifications/{p}/complete",
                params={"score": 720},
            )
            assert c.status_code == 200, c.text

        r = client.get(f"{_BASE}/applications/{app_id}", headers=headers)
        assert r.status_code == 200
        assert r.json()["status"] == "approved", r.text

    def test_complete_without_decision_consent_still_decides(
        self, client: TestClient, db_session: Session
    ):
        """Policy (Dave, 2026-07): the automated decision is NOT gated behind a
        discrete automated_decision_making consent. Completing the FINAL verification
        with ONLY the verification consents granted must run the decision cleanly
        (previously this raised ConsentMissingError -> 422; that gate is removed)."""
        resp = client.post(
            f"{_BASE}/applications",
            json={
                "patient_profile": {
                    "legal_first_name": "NoConsent",
                    "email": f"noadm-{uuid.uuid4().hex[:8]}@example.com",
                },
                "credit_product_id": str(_product_id(db_session)),
                "requested_amount_cents": 3_000_000,
                "requested_amount_source": "patient",
                "contact_method": "email",
            },
        )
        app_id = resp.json()["application_id"]
        code = client.get(f"{_BASE}/dev/magic-link-code", params={"application_id": app_id}).json()[
            "code"
        ]
        ex = client.post(
            f"{_BASE}/auth/magic-link/exchange", json={"application_id": app_id, "token": code}
        )
        headers = {"Authorization": f"Bearer {ex.json()['jwt']}"}

        # Grant ONLY the verification consents — deliberately NOT automated_decision_making.
        # Under the new policy the decision runs anyway (no ADM gate).
        for p in _PURPOSES:
            assert (
                client.post(f"{_BASE}/applications/{app_id}/consents/{p}", headers=headers).status_code
                == 200
            )
            assert (
                client.post(
                    f"{_BASE}/applications/{app_id}/verifications/{p}/initiate", headers=headers
                ).status_code
                == 200
            )

        # First three complete cleanly; the final one triggers the decision, which now
        # runs WITHOUT an automated_decision_making consent (200, not 422).
        for p in _PURPOSES[:-1]:
            assert (
                client.post(
                    f"{_BASE}/dev/applications/{app_id}/verifications/{p}/complete",
                    params={"score": 720},
                ).status_code
                == 200
            )
        final = client.post(
            f"{_BASE}/dev/applications/{app_id}/verifications/{_PURPOSES[-1]}/complete",
            params={"score": 720},
        )
        assert final.status_code == 200, final.text
        # The decision ran and reached a terminal state despite no ADM consent.
        r = client.get(f"{_BASE}/applications/{app_id}", headers=headers)
        assert r.json()["status"] == "approved", r.text

    def test_magic_link_code_missing_returns_404(self, client: TestClient, db_session: Session):
        resp = client.get(
            f"{_BASE}/dev/magic-link-code", params={"application_id": str(uuid.uuid4())}
        )
        assert resp.status_code == 404
