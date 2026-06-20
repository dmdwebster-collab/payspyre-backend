"""The dev clinic-seed helper must produce a working clinic session (vendor +
user + membership) whose JWT the clinic API accepts."""
from fastapi.testclient import TestClient


def test_seed_clinic_returns_usable_jwt(client: TestClient):
    r = client.post("/api/clinic/v1/dev/seed-clinic", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jwt"] and body["vendor_id"] and body["user_id"]
    assert body["email"] and body["password"]

    # The returned JWT must authorize the clinic API (resolves to the vendor).
    headers = {"Authorization": f"Bearer {body['jwt']}"}
    leads = client.get("/api/clinic/v1/marketplace/leads", headers=headers)
    assert leads.status_code == 200, leads.text
    assert isinstance(leads.json(), list)

    # And the seeded credentials must log in via the staff auth endpoint.
    login = client.post(
        "/api/v1/auth/login",
        data={"username": body["email"], "password": body["password"]},
    )
    assert login.status_code == 200, login.text
    assert login.json().get("access_token")
