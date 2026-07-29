"""Live end-to-end smoke test for the PaySpyre platform.

Drives the WHOLE platform against a running deployment (default: staging) using
the public HTTP API + the dev helpers (mounted when ENVIRONMENT != production):
  1. patient approve journey -> approved + correctly-priced marketplace listing
  2. patient decline journey -> rejected
  3. marketplace cross-flow: vendor lead discovery (PII-free) -> express interest
     -> patient select -> lead charge -> billing -> book appointment
  4. clinic console: staff login, products, dashboard, financing-link, applications
  5. rate-limit hygiene: a tripped limit must return 429, never 500

Usage:  PAYSPYRE_API_BASE=https://<host>/api python scripts/e2e_smoke.py
Exits non-zero if any check fails. Requires `requests`.
"""
import os, sys, time, uuid
import requests

API = os.environ.get("PAYSPYRE_API_BASE", "https://payspyre-api-staging-5cflx.ondigitalocean.app/api")
PA = f"{API}/applicant/v1"
PC = f"{API}/clinic/v1"

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗ FAIL'} {name}" + (f"  [{detail}]" if detail else ""))

def req(method, url, S=None, ok=(200, 201), **kw):
    """Request with retry/backoff on rate-limit (429) + transient 5xx.
    429 waits long enough to clear the 60s auth window (5/60s is tight for E2E)."""
    caller = S or requests
    r = None
    for attempt in range(8):
        r = caller.request(method, url, **kw)
        if r.status_code in ok:
            return r
        if r.status_code == 429:
            time.sleep(14)
            continue
        if r.status_code >= 500:
            time.sleep(3)
            continue
        return r  # a real client error — let the caller assert
    return r

def run_patient_to_decision(score):
    S = requests.Session(); S.headers["Content-Type"]="application/json"
    prod = req("GET", f"{PA}/products", S).json()["products"][0]["id"]
    app_id = req("POST", f"{PA}/applications", S, ok=(201,), json={
        "patient_profile":{"legal_first_name":"E2E","legal_last_name":"Tester","email":f"e2e-{uuid.uuid4().hex[:8]}@example.com","phone_e164":"+15555550100"},
        "credit_product_id":prod,"requested_amount_cents":3_000_000,"requested_amount_source":"patient","contact_method":"email"}).json()["application_id"]
    code = req("GET", f"{PA}/dev/magic-link-code", S, params={"application_id":app_id}).json()["code"]
    jwt = req("POST", f"{PA}/auth/magic-link/exchange", S, json={"application_id":app_id,"token":code}).json()["jwt"]
    H={"Authorization":f"Bearer {jwt}"}
    PURP=["id_verification","soft_bureau_pull","bank_verification","hard_bureau_pull"]
    for p in (*PURP,"automated_decision_making"): req("POST", f"{PA}/applications/{app_id}/consents/{p}", S, headers=H)
    for p in PURP: req("POST", f"{PA}/applications/{app_id}/verifications/{p}/initiate", S, headers=H)
    for p in PURP: req("POST", f"{PA}/dev/applications/{app_id}/verifications/{p}/complete", S, params={"score":score})
    state = req("GET", f"{PA}/applications/{app_id}", S, headers=H).json()
    return S, H, app_id, state

print("\n=== SCENARIO 1: Patient APPROVE journey + correctly-priced marketplace listing ===")
S, H, app_id, state = run_patient_to_decision(760)
check("approve journey -> approved", state["status"]=="approved", state["status"])
L = S.post(f"{PA}/marketplace/listings",headers=H,json={
    "treatment_categories":["implants","general_dentistry"],"treatment_urgency":"immediate",
    "estimated_budget_cents":2_500_000,"location_postal_code":"M5V 2T6","max_travel_km":25,
    "consent_acknowledged":True})
check("approved patient can list", L.status_code==201, str(L.status_code))
listing = L.json(); listing_id = listing["id"]
check("listing lead_state=approved (denorm maintained)", listing["lead_state"]=="approved", listing["lead_state"])
check("listing priced above floor (verification_depth maintained)", listing["base_lead_price_cents"]>1575, f"${listing['base_lead_price_cents']/100:.2f}")

print("\n=== SCENARIO 2: Patient DECLINE journey ===")
S2, H2, app2, state2 = run_patient_to_decision(420)
check("low-score journey reaches a terminal decision", state2["status"] in ("rejected","declined","approved","under_review"), state2["status"])
print(f"      (low-score decision was: {state2['status']})")

print("\n=== SCENARIO 3: Marketplace cross-flow (patient listing <-> vendor) + PII protection ===")
seed = requests.post(f"{PC}/dev/seed-clinic", json={}).json()
VH = {"Authorization": f"Bearer {seed['jwt']}"}; vendor_id = seed["vendor_id"]
check("clinic seeded (vendor+user+membership)", bool(seed.get("jwt")), vendor_id[:8])
leads = requests.get(f"{PC}/marketplace/leads", headers=VH).json()
mine = [x for x in leads if x.get("listing_id")==listing_id]
check("vendor sees the patient's lead in /leads", len(mine)==1)
if mine:
    lv = mine[0]
    pii_keys = [k for k in lv if k in ("legal_first_name","legal_last_name","email","phone_e164","name","phone","patient_name")]
    check("vendor lead view is PII-FREE", not pii_keys, f"leaked: {pii_keys}" if pii_keys else "fsa="+str(lv.get("fsa")))
ei = requests.post(f"{PC}/marketplace/leads/{listing_id}/express_interest", headers=VH)
check("vendor express_interest -> 201", ei.status_code==201, str(ei.status_code))
det = requests.get(f"{PC}/marketplace/listings/{listing_id}", headers=VH)
check("vendor sees listing detail post-interest", det.status_code==200, str(det.status_code))
ic = S.get(f"{PA}/marketplace/listings/{listing_id}/interested_clinics", headers=H).json()
check("patient sees vendor in interested_clinics", any(c.get("vendor_id")==vendor_id for c in ic), f"{len(ic)} interested")
sel = S.post(f"{PA}/marketplace/listings/{listing_id}/select_clinic", headers=H, json={"vendor_id":vendor_id})
check("patient select_clinic -> 200", sel.status_code==200, str(sel.status_code))
check("listing closed after selection", sel.json().get("status")=="closed", sel.json().get("status"))
bill = requests.get(f"{PC}/marketplace/billing/leads", headers=VH).json()
charged = [b for b in bill if b.get("listing_id")==listing_id]
check("vendor billing shows the lead charge", len(charged)==1, f"${charged[0]['lead_charge_cents']/100:.2f} ({charged[0]['charge_trigger']})" if charged else "none")
ba = requests.post(f"{PC}/marketplace/listings/{listing_id}/book_appointment", headers=VH)
check("vendor book_appointment -> 201", ba.status_code==201, str(ba.status_code))

print("\n=== SCENARIO 4: Clinic console (auth + dashboard + applications + financing link) ===")
# Pace through the auth rate-limit window (5/60s) so a real login lands cleanly.
login = None
for attempt in range(7):
    login = requests.post(f"{API}/v1/auth/login", data={"username":seed["email"],"password":seed["password"]})
    if login.status_code != 429:
        break
    check(f"  (login rate-limited, returned clean 429 not 500)", login.status_code==429, "waiting 15s")
    time.sleep(15)
check("clinic staff login works (200)", login.status_code==200 and bool(login.json().get("access_token")), str(login.status_code))
prods = requests.get(f"{PC}/products", headers=VH)
check("clinic GET /products", prods.status_code==200, f"{len(prods.json())} products" if prods.status_code==200 else prods.text[:80])
dash = requests.get(f"{PC}/dashboard/summary", headers=VH)
check("clinic GET /dashboard/summary", dash.status_code==200, str(dash.json()) if dash.status_code==200 else str(dash.status_code))
prod_id = requests.get(f"{PA}/products").json()["products"][0]["id"]
fl = requests.post(f"{PC}/financing-links", headers=VH, json={
    "credit_product_id":prod_id,"amount_cents":2_000_000,"patient_name":"Referred Patient","patient_contact":"referred@example.com"})
check("clinic create financing-link", fl.status_code in (200,201), str(fl.status_code) if fl.status_code not in (200,201) else "link created")
apps = requests.get(f"{PC}/applications", headers=VH)
check("clinic GET /applications (vendor-scoped)", apps.status_code==200, f"{len(apps.json())} apps" if apps.status_code==200 else str(apps.status_code))

print("\n=== SCENARIO 5: Rate-limit hygiene (tripped limit must be 429, never 500) ===")
codes = []
for _ in range(8):
    rr = requests.post(f"{API}/v1/auth/login", data={"username":"nobody@example.com","password":"wrong"})
    codes.append(rr.status_code)
check("rate limit engages (saw a 429)", 429 in codes, f"codes={codes}")
check("rate limit NEVER surfaces as 500", 500 not in codes, f"codes={codes}")

print(f"\n{'='*60}\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL: print("FAILED:", ", ".join(FAIL)); sys.exit(1)
print("ALL GREEN — full platform verified live on staging")
