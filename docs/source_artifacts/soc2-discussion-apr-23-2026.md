# SOC 2 — Full Discussion & Decision Framework
**Date of discussion:** Thursday, April 23, 2026
**Context:** Preparing for Monday Apr 27 call with Kevin Godfrey (Henry Schein ONE / Dentrix Developer Program). SOC 2 came up as a likely gating item for HS1's commercial-tier security review.

---

## 1. What SOC 2 actually is

SOC 2 is an **audit report** — not a certification, license, or government approval. It is a report written by an independent licensed CPA firm that attests to how the audited company handles customer data.

- **SOC** = Service Organization Control
- Created by the **AICPA** (American Institute of Certified Public Accountants)
- Designed specifically for SaaS / tech companies that hold other parties' data
- Works like a home inspection report — independent third party reviews against a checklist, writes a report, buyers decide whether to trust

**Why it exists:** Before SOC 2, every enterprise buyer sent every vendor their own bespoke security questionnaire. SOC 2 standardized that. One audit → one report → handed to every enterprise customer who asks.

**Why it matters for Alvero:**
- Henry Schein ONE's security review team will ask one of three questions:
  1. "Do you have SOC 2?" → yes/no
  2. "When will you have SOC 2?" → they want a date
  3. "What's your security posture without SOC 2?" → you stall
- Not technically legally required, but a market expectation for any SaaS touching healthcare data at scale

---

## 2. The Five Trust Service Criteria

SOC 2 audits you against 5 "Trust Service Criteria" — you pick which apply:

1. **Security** — Mandatory. Access controls, firewalls, vulnerability management, incident response. ~80% of the work.
2. **Availability** — Uptime monitoring, disaster recovery, capacity planning.
3. **Confidentiality** — Protecting confidential data (not just personal data). Encryption, classification, secure deletion.
4. **Processing Integrity** — Ensures system processes data correctly and completely. More relevant to PaySpyre than Alvero.
5. **Privacy** — Specifically about PII/PHI handling. GDPR/PIPEDA-aligned controls.

**For Alvero:** Security + Availability + Confidentiality + Privacy (patient data). Skip Processing Integrity (not a calc engine).

---

## 3. Type 1 vs Type 2 — the key distinction

### Type 1 = "Point-in-time" snapshot
- Auditor checks on a specific date: "Do the controls exist today?"
- Example: "As of April 30, 2026, controls were appropriately designed."
- **Proves:** Policies, tools, systems are set up.
- **Doesn't prove:** That they're followed consistently over time.
- **Timeline:** 8–12 weeks with a platform like Vanta
- **First-year cost:** $14–17K all-in

### Type 2 = "Over a period" audit
- Auditor observes 3–12 months then reports controls operated effectively during the window
- Example: "From Jan 1 to Dec 31, 2026, controls operated effectively."
- **Proves:** You actually *do* the security work consistently.
- **Timeline:** 3–12 month observation + 4–8 week audit = 6–15 months total
- **Ongoing cost:** $15–25K/year after year 1

### The strategic play
Get Type 1 first to unblock Dentrix commercial deal fast → Type 2 observation clock starts ticking the moment Type 1 is issued → 6 months later you have Type 2 (the gold standard) without delaying the HS1 relationship. This is the industry-standard pattern.

---

## 4. What the audit actually touches

Not just code. Whole-company operational review:

**People:** Background checks, annual security training, confidentiality agreements, onboarding/offboarding checklists

**Access controls:** MFA everywhere (Google Workspace, AWS, GitHub, Supabase, Notion, Slack), role-based permissions, quarterly access reviews, immediate revocation on departure

**Infrastructure:** Encryption at rest + in transit, firewalls, vulnerability scanning, patch management, prod/staging/dev separation

**Code & change management:** Code review before merge, CI/CD with automated testing, change log, rollback procedures

**Monitoring & incident response:** Centralized logging, alerting, documented IR playbook, post-mortem template

**Policies & documentation (~15–25 policies):** Info Security, Acceptable Use, Data Classification, Vendor Management, Business Continuity, DR Plan, Risk Assessment

**Vendor management:** List every third-party service touching your data, their SOC 2 reports on file, annual review of each

---

## 5. What Vanta (or equivalent platform) actually does

Manual SOC 2 would be 800–1,500 hours of spreadsheet/PDF work over a year. Platforms automate ~70%:

1. **Auto-collect evidence** — connects to AWS/GitHub/Google/Supabase/Slack and continuously pulls proof of controls
2. **Policy templates** — 25+ pre-written policies you customize
3. **Auditor portal** — evidence pre-organized; auditor logs in, runs tests, writes report
4. **Continuous monitoring** — alerts when something drifts (MFA turned off, encryption lapses)
5. **Auditor matchmaking** — partner auditors who know the platform and charge less

Without a platform: $50–100K/year + 3 FTE effort.
With Vanta: $14–17K/year + ~80–120 hours of your time in year 1.

---

## 6. Why SOC 2 recurs every year (not just year 1)

**Common misconception:** "SOC 2 is a one-time inspection that flags issues for me to fix."

**Reality:** SOC 2 is a **time-bounded attestation** with a built-in expiration:
- Type 1 snapshots become stale ~12 months after issuance in the eyes of enterprise buyers
- Type 2 reports cover a specific observation window; next year's customers want next year's report
- Companies change constantly (hiring, firing, new servers, new code, rotated keys) — old reports tell buyers nothing about current posture

**Annual renewal pays for:**
1. A fresh audit covering the new 12-month period ($5–15K)
2. Platform subscription that kept evidence audit-ready all year ($10–12K)
3. A new report dated within the last 12 months

**Upside:** Year 2+ is dramatically easier than year 1. Policies already exist, risk assessment carries over, auditor knows your environment. Expect 20–30 hours of your time in year 2 and smaller audit fee.

**Important distinction:** If you want a one-time "find my security weaknesses" exercise, that's a **penetration test** or **security posture assessment** ($10–20K one-time). Enterprise customers do not accept those in place of SOC 2. Different product, different purpose.

---

## 7. Can AI replace the platform?

### What AI can do (~60–70% of work)
- Draft all 25 policies, customized and well-written
- Write risk assessment, incident response playbook, BC/DR plans
- Write control narratives
- Help answer auditor Q&A
- Build vendor management spreadsheet
- Produce security training content

### Three hard walls AI cannot cross

**Wall 1: The auditor must be a licensed CPA (human).** AICPA legally prohibits AI-generated attestations. CPA's license is on the line. Audit fee ($5–15K) is unavoidable.

**Wall 2: Evidence must be continuously collected from production systems.** Auditors ask for things like: "Git log of every production deploy in March with PR approval links" / "15 random access grants from Q1 with ticket + approver + MFA enforcement date" / "AWS CloudTrail logs for Feb 14 showing every admin action." These must come from live systems with cryptographic provenance. AI can interpret evidence but cannot generate it.

**Wall 3: Continuous monitoring over the Type 2 observation window.** 200+ controls checked daily for 6–12 months. Building this yourself = building Vanta for a single customer. Unfavorable economics.

### DIY vs Platform math

**AI-assisted DIY path:**
- Draft 25 policies: 20 hrs
- Build evidence collection scripts/integrations: 80–120 hrs engineering
- Build continuous monitoring: 40–60 hrs + ongoing
- Evidence gathering during observation: 5–10 hrs/week for 6 months
- Higher auditor fee (evidence not pre-organized): $15–25K
- **Total:** ~$20–30K + 300–400 hours

**Vanta path:**
- Onboarding + policy customization: 30–40 hrs
- Platform subscription: $10–12K
- Lower auditor fee (evidence pre-organized): $3–7K
- **Total:** ~$14–17K + 80–120 hours

**DIY is more expensive AND costs more personal time.**

### Where AI does genuinely help
AI compresses time *inside* Vanta:
- Without AI: 120–150 hrs of your time in year 1
- With Claude as SOC 2 co-pilot: 40–60 hrs of your time in year 1

Platform + AI combo is ~3x better than either alone.

---

## 8. Who actually asks for SOC 2 (concrete list)

### Alvero
- **Henry Schein ONE** — immediate, known, dated
- **Patterson Dental (Eaglesoft PIC)** — when pursuing Eaglesoft integration
- **HS1 Ascend specifically** — cloud integration = stricter bar
- **Curve Dental, Denticon (Planet DDS), Tab32, Oryx** — future PMS integrations
- **DSOs as direct customers (the real commercial upside):**
  - Dentalcorp (Canada, ~550 practices, public company)
  - 123Dentist (Canada, ~400 practices, KKR-backed)
  - Dental Corp of Canada
  - Heartland Dental (US, 1,700+ offices)
  - Pacific Dental Services (US, 900+)
  - Aspen Dental, Smile Brands, MB2 Dental
- **Insurance carriers** (Sun Life, Manulife, Pacific Blue Cross, Delta Dental) — if Alvero handles insurance flows directly

### PaySpyre (arguably MORE SOC 2-dependent than Alvero)
- **Banking partners** — BMO, RBC, CIBC, Scotiabank (often SOC 1 too)
- **Payment processors** — Moneris, Global Payments, Stripe, Square, Helcim
- **Flinks** — upstream partners (TD, BMO, RBC) require SOC 2 flow-through
- **Rotessa** — same flow-through requirement
- **DocuSign** — enterprise tier / volume pricing
- **Multi-location practice owners (> 3 locations)**
- **Investor due diligence** — friction reduction + valuation protection during raise/exit

### 3D Studio
Near-term SOC 2 pressure is low. Relevant if scaling to DSO-scale labs, integrating with PMS, or adding payment flows.

### Alvero Reach
Low unless scaling to DSO marketing or handling large PHI volumes.

### Own practices (KDC, NTT, Brightside, UMD, Vantage, Wasylyk)
Don't need SOC 2 — they consume SOC 2'd platforms, not provide them.

---

## 9. Scope & reuse — "PaySpyre-reusable" meaning

SOC 2 is scoped to a **specific legal entity** covering **specific systems**. Reuse depends on corporate structure:

### Scenario A — Same legal entity, both products in scope
- Alvero + PaySpyre both under Dr. Michael J. Webster Inc.
- **Reuse: ~95%.** Same audit covers two products.
- **3-year total:** ~$60–70K for both

### Scenario B — Separate entities, shared infrastructure
- Alvero Inc. + PaySpyre Inc. separate (common for fintech/regulatory ring-fencing)
- **Reuse: ~60–70%.** Playbook carries over; two audits still required.
- **3-year total:** ~$85–95K for both

### Scenario C — Fully independent
- Separate companies, separate teams, separate infrastructure
- **Reuse: ~20%.** Just general SOC 2 knowledge.
- **3-year total:** ~$110–130K for both

**Strategic implication:** If SOC 2 needed for both within 18–24 months, cheapest path is single corporate roof for SOC 2 purposes, even if products are commercially separate. But payments regulatory/liability considerations may push toward separate incorporation for PaySpyre — trade-off to think through.

**Key question to resolve:** How are Alvero and PaySpyre currently structured legally?

---

## 10. The "HS1-only" minimum-scope strategy

A legitimate middle path:
- Do Type 1 for HS1-triggered need only
- Don't renew if nothing else materializes by month 10
- Treat as one-time $17K marketing/BD expense tied specifically to Dentrix commercial unlock
- Revisit at month 10 based on actual demand signal

**Caveat:** Letting a SOC 2 report lapse looks slightly worse than never having had one — suggests "we let controls go." So this is a one-time play, not a pattern.

---

## 11. Realistic timeline (Vanta path, Type 1)

- **Weeks 1–2 (May 1–14):** Demo call, contract, onboarding kickoff, connect integrations
- **Weeks 3–6 (May 15–Jun 12):** Address failing controls, customize policies, risk assessment, team training
- **Weeks 7–10 (Jun 13–Jul 10):** Engage auditor, pre-audit review, fix gaps, complete background checks
- **Weeks 11–12 (Jul 11–24):** Final audit fieldwork, Type 1 report issued
- **Post Type-1:** Type 2 observation window starts. Type 2 report 6–8 months later.

**Your time:** ~10–15 hrs/week weeks 1–4, 5–8 hrs/week weeks 5–10, 10 hrs in audit window. ~80–120 hrs over 3 months. 2–3 hrs/month ongoing after cert.

---

## 12. Honest downsides

1. **Busywork.** A lot is documenting existing practice, not improving security.
2. **Annual tax.** $15–25K/year forever + 40–60 hrs/year renewal work.
3. **Canadian context.** SOC 2 is American. PIPEDA / PIPA-BC / PHIPA still matter. SOC 2 covers ~80% of PIPEDA. May eventually also want ISO 27001 for European customers.
4. **Not a breach shield.** Companies with SOC 2 still get breached. Evidence of hygiene, not a guarantee.
5. **Scope creep.** Enterprise customers will ask next for HIPAA, PCI, Type 2, ISO 27001, HITRUST.

---

## 13. Decision framework

### Buy Vanta Type 1 if:
- HS1 deal is actively gated by security review (it will be)
- You expect ≥1 DSO conversation in next 12 months
- PaySpyre will need SOC 2 within 18 months (it will)
- $17K is not meaningful cash constraint

### Don't do it (or defer) if:
- You're 6–12 months from any enterprise customer conversation
- Cash runway where $17K meaningfully hurts
- Pivoting or selling Alvero in next 6 months

**Current recommendation:** Vanta Type 1, kick off week of May 4. Target cert mid-July 2026 (before HS1 commercial security review). Use Claude aggressively to compress personal time from 120 hrs to 40–60 hrs.

**Open questions to resolve before signing Vanta contract:**
1. Legal structure of Alvero vs PaySpyre (drives scope decision)
2. Whether to include PaySpyre in initial scope (probably yes if same entity)
3. Canadian auditor firm preference (ask Vanta for partner list for Canadian entities)
4. Single vs multi-product platform pricing negotiation

---

## 14. Action items (next 2 weeks)

1. **Apr 28–May 1:** Submit Vanta demo request using draft at `/home/user/workspace/vanta-demo-request-email.md`
2. **May 1–2:** 30-min Vanta discovery call — ask about Canadian auditor partners, multi-product scope pricing, Starter/Core tier pricing
3. **May 2–3:** Request Secureframe demo as negotiation leverage (10–15% off typical)
4. **May 4–6:** Decide single vs multi-entity structure for scope (Alvero only or Alvero + PaySpyre)
5. **May 6–9:** Sign contract, begin onboarding, connect integrations
6. **Ongoing:** Use Claude as SOC 2 co-pilot throughout — draft policies, answer Q&A, produce training content

---

**Source material:** Session conversation April 23, 2026 between Dr. Michael Webster and Perplexity Computer. Preceded finalization of Monday Apr 27 call-prep deliverables for Henry Schein ONE (architecture one-pager, API call volume estimate, walkthrough script).
