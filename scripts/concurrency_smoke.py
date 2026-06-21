"""Concurrency smoke test for PaySpyre — validates the race-protected money paths.

Fires genuinely-parallel requests at the paths guarded by row locks / unique
indexes and asserts no double-charge / duplicate / 500 under concurrency:
  1. N concurrent express_interest (same vendor + listing) -> exactly 1 interest
  2. 2 concurrent select_clinic (different vendors)         -> 1 wins, 1 charged

This caught a real double-charge that the sequential E2E + unit tests missed
(two simultaneous select_clinic calls both billed before the row lock landed).
Run it after a deploy alongside e2e_smoke.py.

Usage:  PAYSPYRE_API_BASE=https://<host>/api python scripts/concurrency_smoke.py
Exits non-zero on any failure. Requires `requests`. Uses dev helpers (mounted when
ENABLE_DEV_TOOLS / a dev env), so point it at staging, not production.
"""
import concurrent.futures as cf
import os
import time
import uuid

import requests

API = os.environ.get("PAYSPYRE_API_BASE", "https://payspyre-api-staging-5cflx.ondigitalocean.app/api")
PA = f"{API}/applicant/v1"
PC = f"{API}/clinic/v1"
PASS, FAIL = [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK ' if cond else 'XX '}{name}" + (f"  [{detail}]" if detail else ""))


def approved_patient_jwt():
    S = requests.Session(); S.headers["Content-Type"] = "application/json"
    prod = S.get(f"{PA}/products").json()["products"][0]["id"]
    app_id = S.post(f"{PA}/applications", json={
        "patient_profile": {"legal_first_name": "Conc", "email": f"c-{uuid.uuid4().hex[:8]}@example.com"},
        "credit_product_id": prod, "requested_amount_cents": 3_000_000,
        "requested_amount_source": "patient", "contact_method": "email"}).json()["application_id"]
    code = S.get(f"{PA}/dev/magic-link-code", params={"application_id": app_id}).json()["code"]
    jwt = None
    for _ in range(8):
        r = S.post(f"{PA}/auth/magic-link/exchange", json={"application_id": app_id, "token": code})
        if r.status_code == 200:
            jwt = r.json()["jwt"]; break
        time.sleep(14)
    H = {"Authorization": f"Bearer {jwt}"}
    PURP = ["id_verification", "soft_bureau_pull", "bank_verification", "hard_bureau_pull"]
    for p in (*PURP, "automated_decision_making"): S.post(f"{PA}/applications/{app_id}/consents/{p}", headers=H)
    for p in PURP: S.post(f"{PA}/applications/{app_id}/verifications/{p}/initiate", headers=H)
    for p in PURP: S.post(f"{PA}/dev/applications/{app_id}/verifications/{p}/complete", params={"score": 760})
    return H


def make_listing(H):
    return requests.post(f"{PA}/marketplace/listings", headers=H, json={
        "treatment_categories": ["implants"], "treatment_urgency": "immediate",
        "estimated_budget_cents": 2_500_000, "location_postal_code": "M5V 2T6",
        "max_travel_km": 25, "consent_acknowledged": True}).json()["id"]


def seed_clinic():
    d = requests.post(f"{PC}/dev/seed-clinic", json={}).json()
    return {"Authorization": f"Bearer {d['jwt']}"}, d["vendor_id"]


print("Setting up (1 patient JWT, 2 listings)...")
H = approved_patient_jwt()
listing_dup = make_listing(H)
listing_sel = make_listing(H)

print("\n=== RACE 1: 8 concurrent express_interest, SAME vendor + listing -> exactly 1, no 500 ===")
VH, _ = seed_clinic()
with cf.ThreadPoolExecutor(max_workers=8) as ex:
    codes = list(ex.map(lambda _: requests.post(
        f"{PC}/marketplace/leads/{listing_dup}/express_interest", headers=VH).status_code, range(8)))
chk("no 500s on concurrent express_interest", 500 not in codes, f"codes={codes}")
interested = requests.get(f"{PA}/marketplace/listings/{listing_dup}/interested_clinics", headers=H).json()
chk("exactly ONE interest row (no duplicate)", len(interested) == 1, f"{len(interested)} rows")

print("\n=== RACE 2: 2 concurrent select_clinic (different vendors) -> 1 wins, 1 charged ===")
V1H, v1 = seed_clinic()
V2H, v2 = seed_clinic()
requests.post(f"{PC}/marketplace/leads/{listing_sel}/express_interest", headers=V1H)
requests.post(f"{PC}/marketplace/leads/{listing_sel}/express_interest", headers=V2H)
def select(v):
    return requests.post(f"{PA}/marketplace/listings/{listing_sel}/select_clinic",
                         headers=H, json={"vendor_id": v}).status_code
with cf.ThreadPoolExecutor(max_workers=2) as ex:
    f1, f2 = ex.submit(select, v1), ex.submit(select, v2)
    sc = sorted([f1.result(), f2.result()])
chk("exactly one select succeeded", sc.count(200) == 1, f"codes={sc}")
chk("no 500 on the losing select", 500 not in sc, f"codes={sc}")
b1 = requests.get(f"{PC}/marketplace/billing/leads", headers=V1H).json()
b2 = requests.get(f"{PC}/marketplace/billing/leads", headers=V2H).json()
charges = sum(1 for b in (b1 + b2) if b.get("listing_id") == listing_sel)
chk("exactly ONE vendor charged (no double-charge under concurrency)", charges == 1, f"{charges} charges")

print(f"\n{'='*60}\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    raise SystemExit(f"FAILED: {FAIL}")
print("ALL GREEN — race-protected money paths verified under concurrency")
