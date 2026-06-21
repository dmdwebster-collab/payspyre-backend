# PaySpyre — Mock-Mode Beta Testing Guide

Staging runs the **full platform on mock integrations** — no real ID checks, bank
links, credit pulls, e-signatures, payouts, emails, or SMS happen. You drive the
whole flow yourself and simulate the vendor results in-app. This lets us test the
product end-to-end now, before the real vendor accounts (Equifax, Flinks, etc.)
are live.

## URLs

| | |
|---|---|
| **Patient app** | https://payspyre-frontend-staging-dmak6.ondigitalocean.app |
| **Clinic console** | same site → `/clinic/login` |
| Backend API (health) | https://payspyre-api-staging-5cflx.ondigitalocean.app/health |

> A **clinic login** (email + password) is provided separately — it isn't in this repo.

## What "mock mode" means for a tester
- **Sign-in code:** not really emailed/texted. On the "Enter your sign-in code"
  screen, click **"Auto-fill it"** (shown under the form in mock mode).
- **Verifications** (ID, bank, credit): click **"Complete (mock)"** on each to
  simulate a passing vendor result. The credit score used for the decision is the
  mock value — a high score approves, a low score declines.
- **SIN:** you can enter a test SIN or click decline — it's never sent anywhere real.
- **E-sign / disbursement / notifications:** stubbed; no documents, money, or
  messages move.

## Patient walkthrough (the financing flow)
1. Open the patient app → **Start an application**. Pick a product, enter a name +
   email, choose a financing amount **within the product's range** (out-of-range is
   now rejected).
2. **Sign-in code** screen → **Auto-fill it** → continue.
3. **Consents** → check each purpose → continue.
4. **SIN** → enter a test SIN *or* decline.
5. **Verifications** → for each (ID / bank / credit), **Start** then **Complete (mock)**.
6. **Decision** → approved or declined based on the mock credit score.
7. *(Optional)* From the decision screen, **explore the marketplace** → publish a
   de-identified listing (you must check the consent box).

## Clinic console walkthrough
1. Go to `/clinic/login`, sign in with the clinic credentials.
2. **Dashboard** — application counts for your clinic.
3. **New financing link** — create a pre-filled application + shareable patient link.
4. **Applications** — every application from your clinic (scoped to you only).
5. **Marketplace leads** — browse de-identified patient leads (no name/contact until
   the patient selects you), **express interest**, **book appointment**; **Billing**
   shows lead charges.

## What to look for / report
- Anything confusing, broken, or that reads wrong (copy, amounts, flow).
- Anywhere the flow dead-ends or an error isn't explained.
- Money/amounts displayed incorrectly.
- The marketplace: confirm a vendor never sees patient PII before selection.

## Known mock-mode caveats (not bugs)
- Auth is rate-limited (5 sign-in attempts / minute / IP) — wait a minute if you hit it.
- Real emails/SMS/credit pulls don't happen — that's the point of mock mode.
- The clinic-seed + dev helpers exist only on non-production builds.
