import hashlib
import hmac
import json
import time

from app.core.config import settings


def _headers(body: bytes, secret: str, path: str = "/v1/alvero/applications") -> dict[str, str]:
    ts = str(int(time.time()))
    canonical = "\n".join(["POST", path, ts, hashlib.sha256(body).hexdigest()])
    sig = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Alvero-Tenant": "kdc",
        "X-Alvero-Timestamp": ts,
        "X-Alvero-Signature": sig,
        "Idempotency-Key": "idem-1",
    }


def _body() -> bytes:
    return json.dumps(
        {
            "alvero_application_id": "tc-handoff-kdc-42",
            "tenant_slug": "kdc",
            "provider_external_id": "kdc",
            "patient": {
                "first_name": "Test",
                "last_name": "Patient",
                "email": "test.patient@example.test",
                "phone": "+12505550100",
            },
            "requested_amount_cents": 350000,
            "requested_term_months": 24,
            "purpose": "Crown",
        },
        separators=(",", ":"),
    ).encode("utf-8")


def test_alvero_application_requires_signature(client):
    settings.ALVERO_TENANT_KEY_KDC = "secret"
    r = client.post("/v1/alvero/applications", data=_body(), headers={"X-Alvero-Tenant": "kdc"})
    assert r.status_code == 401


def test_alvero_application_create_is_idempotent(client):
    settings.ALVERO_TENANT_KEY_KDC = "secret"
    body = _body()
    headers = _headers(body, "secret")

    first = client.post("/v1/alvero/applications", data=body, headers=headers)
    assert first.status_code == 201, first.text
    first_json = first.json()
    assert first_json["status"] == "started"
    assert first_json["wizard_url"].endswith(f"/apply/{first_json['application_id']}")

    second = client.post("/v1/alvero/applications", data=body, headers=headers)
    assert second.status_code == 201, second.text
    assert second.json()["application_id"] == first_json["application_id"]
