# PaySpyre — Claude Code Handoff

**Owner:** Dr. Michael Webster
**Partner / Operator:** David Wilson (PaySpyre Financial)
**Packaged:** 2026-05-14 (Perplexity Computer session)
**Purpose:** Full PaySpyre context dump so work can continue in Claude Code without losing thread.

---

## 1. What PaySpyre is

PaySpyre Financial is a **direct dentist-to-patient lending platform** (a.k.a. PaySpyre 2.0 / "Pacefire" internally). It originates and services patient financing loans for dental treatment, currently piloted through KDC and NTT.

- **Today (legacy):** ~87 active loans, ~20 applications/month, serviced on **TurnKey Lender** (third-party platform, ~$32,400 CAD/yr).
- **Target (PaySpyre 2.0):** Replace TurnKey with an in-house, AI-built platform. Originate, underwrite, document, fund, and collect loans end-to-end.
- **Track record on TurnKey:** $5.5M originated, 94.6% payment success, $174K/yr revenue.

### Cancellation / migration plan (TurnKey exit)

| Phase | When | What |
|---|---|---|
| 1. Data export | NOW (already done / in progress) | Export everything from TurnKey while access is live — borrowers, loans, payments, vendors, collections, decision rules. Triple backup. |
| 2. Parallel ops | May–Aug 2026 | New apps go through PaySpyre 2.0. Existing ~87 loans stay on TurnKey. |
| 3. Data migration | Aug–Sep 2026 | Migrate live loans, verify every balance to the cent (DSI validator), notify borrowers, transition PAD. |
| 4. Cancellation | Oct 1, 2026 | Written non-renewal notice (registered mail + email to Austin, TX). Draft letter exists. |
| 5. Post-TurnKey | Nov 1, 2026+ | All ops on PaySpyre. Saves $32,400 CAD/yr. |

**Legal watch-out:** TurnKey contract has a non-compete-ish clause ("product that competes with or includes features substantially similar"). Defensible — platform built independently with AI, no source access, no benchmarking — but **MMS Law should review before sending cancellation notice**.

---

## 2. Target launch timeline (David Wilson's directive)

- **End of June 2026:** Fully functional product.
- **July – September 2026:** Testing / breaking the system.
- **Fall 2026:** Launch PaySpyre 2.0 in production.

David's three top concerns, in order:
1. Security
2. Accuracy
3. Continuity of business (bugs, downtime)

He has stated that **all legals must be in place before launch** — specifically, determining whether PaySpyre requires **MSB or PSP registration**. See `source_artifacts/payspyre-msb-psp-research.md`.

---

## 3. Current architecture (built, not yet deployed end-to-end)

### Code state

- **Backend:** FastAPI (Python). Core lending logic, underwriting engine, decisioning, collections state machine.
- **Frontend:** React (static, CDN-hostable). Three portals + admin:
  - **Consumer (Patient) Portal** — loan application, status, payment management.
  - **Vendor (Clinic) Portal** — clinic-initiated loans, disbursements, dashboard.
  - **Operator / Admin Panel** — internal ops, manual review, configuration. VPN/IP-restricted.
  - **Dashboard** — analytics / KPIs.
- **Database:** PostgreSQL — 10+ tables (borrowers, vendors, loans, payments, applications, documents, audit logs, etc.). SQLAlchemy models + Alembic migrations. **Not yet deployed to a hosted instance** — runs locally via Docker Compose. Seed data: 2 vendors (KDC, NTT), 10 borrowers, 15 loans.
- **Underwriting:** Internal Python service. Includes **CashScore** (proprietary alternative-credit signal — the competitive moat).
- **Repo size:** ~380K LOC, **1,471 automated unit tests (all passing)**.

### Testing done

| Test | Result |
|---|---|
| 1,471 unit tests | All passing |
| 1,056 simulated applicants (1,000 + 56 adversarial) | 0 errors |
| 219 DSI math validations | Every cent verified |
| Edge cases (zero income, 100 NSFs, leap year, negative balances, $35K max) | All handled |
| PTI/DTI/red flag auto-approve gates | Hardened |

### Integration gaps (all human-action blockers, not code)

| Integration | Status | Needed |
|---|---|---|
| **Flinks** (bank verification) | Simulated only | API key + test with real bank account |
| **Rotessa** (PAD payments) | Not connected | Account + test PAD auth + collection |
| **DocuSign** (loan docs) | Templates exist | Account + David reviews output |
| **Twilio** (SMS/email notifications) | Templates only | Twilio account |
| **End-to-end with real data** | Never run | Requires all of the above |
| **Identity verification (KYC)** | See §4 — decision pending | Persona vs Didit POC |
| **Production hosting** | Not deployed | DB + API + frontends → payspyre.com |
| **Load / stress test** | Not done | After deployment |
| **Pen test** | Not done | $8–40K firm engagement, pre-launch |
| **David's workflow validation** | Not done | Critical — his domain knowledge gates launch |

### Database hosting decision

- **Now (testing):** Local Docker on dev machine.
- **Launch (Jun–Sep):** Railway or AWS RDS, $50–100/mo. Auto-backups, no 2 AM crashes.
- **Scale ($1M+ portfolio):** AWS RDS `ca-central-1` (Montreal) — **PIPEDA data residency**, Multi-AZ.

**Recommendation:** Go managed. Self-hosting financial data without a dedicated DevOps person is too much risk for $50–100/mo of savings.

---

## 4. KYC / Identity verification — decision in progress

David ran POCs with three vendors. Summary:

| Vendor | Pricing | Verdict |
|---|---|---|
| **Trulioo** | $20K USD/yr minimum (~$83/app at 240 apps/yr) | **No** — minimum way too high for current stage |
| **Persona** | $250 USD/mo Essentials = 500 verifications/mo; KYB add-on $1,250/mo | Viable. Good API, well-regarded, Canada-capable. KYC-only. |
| **Didit** (didit.me, YC W26) | **500 free verifications/mo**, then $0.15 each. Face match, liveness, IP analysis also have 500/mo free tiers. | **Strongest fit** — free at current volume, FINTRAC-aware, ISO 30107-3 PAD Level 2 liveness, ISO 27001, dedicated Canada KYC/AML page. |

### Recommendation (logged with David)

- **Do not build KYC in-house.** It's a regulated-data + ML problem, not a code problem. PaySpyre is a FINTRAC reporting entity — "we built it ourselves" is the worst audit answer.
- **Run a Didit POC this week.** Free tier covers current volume forever; cost ceiling ~$0.33/verification even at 10× growth.
- **Persona is the safe Plan B** if Didit risks (newer company, EU HQ → PIPEDA data residency question, no public AML pricing) don't shake out.
- **KYB stays manual** for now — vendors/business borrowers onboard 1–3/month, not worth $15K/yr KYB add-on. Build a clean internal admin tool: structured intake, doc upload to Supabase storage, beneficial-owner KYC via Persona/Didit links, review queue with audit log.

### What PaySpyre **should** build around the KYC vendor

1. **Orchestration layer** — webhook receiver + state machine. Persona/Didit result → Supabase → tie to loan app → route through underwriting.
2. **Risk rules engine** — proprietary logic on top of pass/fail (e.g., "verified but address mismatch + thin credit file → manual review"). **This is PaySpyre's IP.**
3. **Manual KYB workflow** — internal admin tool as described above.
4. **Co-borrower flow** — linking two verifications to one loan application.
5. **Audit/reporting dashboard** — pull KYC events into PaySpyre DB for FINTRAC, internal audit, analytics.

**Open questions to nail before signing:**
- Didit AML check pricing (premium, prepaid credits, not public).
- Didit data residency for Canadian PII (PIPEDA — can data be pinned EU/US/Canada?).
- Persona Canadian ID doc support (provincial IDs, PR cards).

---

## 5. Legal / regulatory

### MSB vs PSP registration — **launch blocker per David**

Full analysis lives in `source_artifacts/payspyre-msb-psp-research.md`. High-level:

- **MSB (Money Services Business)** — FINTRAC registration. Required if PaySpyre transmits money / handles foreign exchange / cashes cheques / issues prepaid. **Lending-only** historically does **not** require MSB, but PaySpyre's PAD collection + clinic disbursement flow needs a clean legal read.
- **PSP (Payment Service Provider)** — Bank of Canada's new Retail Payment Activities Act (RPAA) framework. If PaySpyre is deemed to perform "payment functions" (e.g., holding funds, payment initiation), registration with Bank of Canada is required.
- **FINTRAC reporting entity** — PaySpyre is a lender → AML/ATF obligations apply regardless of MSB status. KYC, suspicious transaction reports, record-keeping, compliance officer.

**Action:** MMS Law to confirm MSB/PSP applicability. Cannot launch until this is determined.

### Other compliance posture

- **PIPEDA** — Canadian data residency (`ca-central-1`), encryption at rest + in transit, retention policy, breach notification.
- **SOC 2** — Type 1 in progress (Vanta-tracked). Scoped as "Alvero SOC 2 Type 1 — HS1-triggered, **PaySpyre-reusable**." PaySpyre will need its own SOC 2 within 12–18 months for:
  - Banking partners (BMO, RBC, CIBC, Scotiabank merchant services)
  - Payment processors (Moneris, Global Payments, Stripe, Square, Helcim partner programs)
  - Flinks + Rotessa upstream bank flow-through requirements
  - DSO-scale enterprise sales
  - Investor due diligence
- Full SOC 2 framing in `source_artifacts/soc2-discussion-apr-23-2026.md`.

---

## 6. Capital structure / financing

### Funding decision (April 2026)

David and Mike evaluated:
1. **SAFE round, $350K @ $4M cap** — investor gives capital now, converts at next round.
2. **Bootstrap** — fund from dental practice revenue + PaySpyre's $174K/yr.
3. **BMO Business LOC** — borrow at ~7.45%, deploy as PaySpyre loans at 14.5% APR avg → ~7% net spread.

**Current direction:** Bootstrap-leaning. BMO **declined** the lending-specific credit line via Gord; would need to reframe as operational LOC against dental practice cash flow. PaySpyre's $174K/yr + first-payment recycling (2 weeks post-origination) makes self-funding viable at 10–20 loans/month pace.

### Why this matters for the build

- Operating cost target: **~$10K/month**.
- David specifically does **not** want a full-time CTO at $150–200K. He wants someone technical who can deploy, plug in integrations, maintain, iterate, and own security/backups/monitoring. The build-to-launch is doable with the existing AI-built codebase + integration work.

---

## 7. Revenue model (under exploration)

Beyond core loan interest, PaySpyre is exploring:

- Credit monitoring services
- Credit insurance fees
- Lead claim fees (vendor pays to claim a borrower lead)
- Loan origination fees
- Advertisements
- Vendor tiered perks (early lead access)
- Non-industry alternative lenders (mindful of self-competition)

Focus is **scaling through automation** — willing to accept higher integration costs for full automation of onboarding (KYC/KYB), underwriting, documentation, and servicing.

---

## 8. People / org

| Person | Role |
|---|---|
| **Mike Webster** | Owner / technical lead. Builds the platform with AI. |
| **David Wilson** | Operating partner, PaySpyre Financial. Owns lending domain expertise, legal/compliance, partnerships (banking, processors, KYC vendors). Hosts the "Operator Portal." |
| **Jade** | KDC — owns PaySpyre process at clinic level (alongside Keely). |
| **Keely** | KDC — co-owns PaySpyre at clinic level. |
| **Hannah** | NTT — handles NTT PaySpyre application overflow. |
| **Yolanda** | KDC reactivation — does **not** handle PaySpyre finance applications. |
| **MMS Law (Vanessa / Leanne)** | External counsel — TurnKey cancellation review, MSB/PSP determination. |

---

## 9. Critical files in this bundle

### `source_artifacts/payspyre-security-documentation.md` (~88 KB, 1,528 lines)

Comprehensive security documentation — investor due-diligence-grade. Sections:
1. Security Architecture Overview (multi-tier, defense-in-depth)
2. Data Classification & Protection
3. Authentication & Access Control
4. Encryption Standards
5. Infrastructure Security
6. PIPEDA Compliance
7. FINTRAC Security Requirements
8. Incident Response Plan
9. Monitoring & Alerting
10. Backup & Disaster Recovery
11. Security Audit Checklist (Pre-Launch)
12. Penetration Testing Plan

**Use this as the canonical security spec.** Has not been independently audited yet — pen test pending.

### `source_artifacts/payspyre-msb-psp-research.md` (~45 KB, 650 lines)

MSB vs PSP registration deep-dive. FINTRAC + Bank of Canada RPAA analysis. **Required reading before MMS Law conversation.**

### `source_artifacts/soc2-discussion-apr-23-2026.md` (~15 KB, 292 lines)

SOC 2 framing — Type 1 vs Type 2, the five Trust Service Criteria, what's in scope for Alvero vs PaySpyre, banking/processor flow-through requirements, cost ($14–17K range), Vanta tooling.

---

## 10. Immediate next steps for Claude Code

In priority order (David's launch timeline is end of June 2026):

1. **Stand up the database** — Docker Compose locally, then provision Railway or AWS RDS (`ca-central-1`). Run Alembic migrations. Load seed data.
2. **KYC POC** — Wire Didit into a sandbox loan application flow. Validate webhook → Supabase → state machine. Get the AML pricing quote in parallel.
3. **Flinks integration** — Connect API key, run a real bank verification with Mike's account.
4. **Rotessa integration** — Real PAD auth + test collection.
5. **DocuSign** — Generate one real loan agreement, David reviews every merge field.
6. **Twilio** — Real SMS to Mike's phone, verify templates render.
7. **Deploy** — `payspyre.com` via Cloudflare Pages (frontends) + Railway/AWS (API + DB). Production-ready, monitored, backed up.
8. **End-to-end test** — Mike or David as test borrower. Application → underwriting → approval → DocuSign → Rotessa first payment. Compare to TurnKey output line by line.
9. **MSB/PSP determination** — MMS Law engagement (Mike + David action, not Claude).
10. **Pen test** — Schedule with vendor of choice, $8–40K range.
11. **Vanta SOC 2 Type 1** — Continue tracking (Alvero-led, PaySpyre-reusable).
12. **Launch controlled pilot** — First real KDC patient through PaySpyre 2.0, parallel-run with TurnKey, scale to 5 → 10 → 20.

---

## 11. Stack summary (for Claude Code to load context fast)

| Layer | Tech |
|---|---|
| Frontend | React (static, CDN-hosted) |
| Backend API | FastAPI (Python) |
| Underwriting engine | Python (internal service, includes CashScore) |
| Database | PostgreSQL 16 (Docker Compose locally, Railway/AWS RDS for prod) |
| Auth | TBD — likely JWT + bcrypt + MFA for admin |
| KYC | Didit (recommended) / Persona (Plan B) |
| Bank verification | Flinks |
| Payments / PAD | Rotessa |
| Loan documents | DocuSign |
| Notifications | Twilio (SMS) + email provider TBD (Resend is used in Alvero/3D Studio) |
| Hosting (frontends) | Cloudflare Pages |
| Hosting (API/DB) | Railway → AWS `ca-central-1` at scale |
| Compliance tooling | Vanta (SOC 2) |
| Audit/observability | TBD — needs to satisfy FINTRAC examination |

---

## 12. Open decisions / things to confirm with Mike or David before changing

- **KYC vendor final pick** (Didit vs Persona) — POC in flight.
- **MSB/PSP registration** — MMS Law output.
- **Production DB host** (Railway vs AWS RDS at launch).
- **Auth provider** for admin / operator portal — currently TBD.
- **Email provider** — Resend (consistent with Alvero) vs Postmark vs SendGrid.
- **AML provider** — bundled with KYC choice or separate (e.g., ComplyAdvantage).
- **Cancellation letter to TurnKey** — drafted, awaiting MMS Law sign-off before sending Oct 1.

---

**End of handoff. All referenced documents are in `source_artifacts/`.**
