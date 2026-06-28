# The AI-CTO System — umbrella plan

PaySpyre is a Canadian consumer-lending platform built by a two-person founding
team with no full-time CTO. This document describes how we get **CTO-grade
engineering governance** — the rigor a regulated lender needs — out of an
**AI-does-the-work / human-owns-the-decision** operating model, and grounds that
choice in both empirical evidence and Canadian regulation.

---

## Thesis

> **AI does the work; a named human owns the decision.**

This is not a slogan of convenience — it is what both the evidence and the law point
to independently.

**The evidence says AI can't be left alone.**
- AI-only PR review catches materially fewer real defects than human review — roughly
  **~45% (AI-only) vs ~68% (human) merge-quality outcomes** in published comparisons —
  so an unsupervised AI reviewer is a *downgrade*, not a substitute.
- AI-generated code ships a meaningful vulnerability often: studies find AI-written
  code introduces an **OWASP Top-10 class vulnerability roughly 45% of the time**.
  Velocity without a gate ships exploitable code.

The conclusion isn't "don't use AI" — AI is by far the most productive way to do the
work. It's that the work needs **deterministic gates and a human owner**, which is
exactly what this system provides.

**The law says a named human must own the decision.** Independently of the evidence,
Canadian regulation already *requires* named human accountability for precisely the
decisions a lender makes:
- **OSFI Guideline E-23 (Model Risk Management), effective 2027-05-01** — requires a
  **named human Model Approver** for credit models, with *heightened* governance for
  AI/ML models (explainability, monitoring, human oversight).
- **PIPEDA Schedule 1, cl. 4.1** — an organization must designate a **named individual
  accountable** for its privacy compliance.
- **PIPEDA breach reporting** — a breach of safeguards posing a **"real risk of
  significant harm"** must be reported to the Privacy Commissioner and affected
  individuals; that risk assessment is a human judgement call.
- **Bank / sponsor partners** demand **SOC 2 Type II + an independent penetration
  test** before they'll move real money on your behalf.
- **Cyber-insurers** require **MFA** (and increasingly evidence of basic controls)
  as a condition of coverage.

So the "named human owns the decision" half of the thesis is not optional — it's the
regulatory and commercial floor. This system industrializes the "AI does the work"
half safely up to that floor.

---

## The 5-layer architecture

Defense in depth: each layer catches a different class of failure. A change must pass
through all five before it touches real customers or real money.

### Layer 1 — Cross-model adversarial review
Multiple models review each change *adversarially* (one writes, others try to break
it), instead of trusting a single model's self-assessment. Mitigates the ~45% AI-only
miss rate by making models check each other.
→ [`docs/ai_cto/adversarial_review.md`](./adversarial_review.md)

### Layer 2 — Deterministic gates
Non-negotiable, machine-checkable gates on every PR: **Semgrep SAST** (+ Bandit) for
insecure code patterns, **secret scanning** (gitleaks), **dependency CVE scanning**
(pip-audit), and **SBOM** generation for supply-chain provenance. These *fail the
build* — they don't ask an LLM's opinion. This is what stops the ~45% "AI ships an
OWASP vuln" problem from reaching main.
→ [`docs/security/sast.md`](../security/sast.md),
  [`docs/security/supply_chain.md`](../security/supply_chain.md)

### Layer 3 — Compliance-as-code + evidence
A **controls registry** maps each obligation (PIPEDA, OSFI, disclosure rules) to the
code/test that enforces it, and a **`PlatformEvent` WORM audit log**
([`app/models/platform/event.py`](../../app/models/platform/event.py)) records
money/decision/PII events immutably — the evidence trail a SOC 2 / regulator review
needs to exist *before* it's asked for.
→ [`docs/compliance_controls.md`](../compliance_controls.md)

### Layer 4 — Scheduled audits
A recurring sweep ([`.github/workflows/scheduled-audit.yml`](../../.github/workflows/scheduled-audit.yml))
re-runs pip-audit, gitleaks, and `pytest -m compliance` weekly and **opens/updates a
GitHub issue** on findings — catching newly-disclosed CVEs and regressions in code no
PR has touched. It never hard-fails; the issue *is* the escalation to a human.

### Layer 5 — The human-escalation boundary
The explicit list of decisions that **must stay human** — credit-model deploys, new
money flows, auth/PII architecture, regulated disclosures, breach-reporting calls,
and production go-live — enforced by [`.github/CODEOWNERS`](../../.github/CODEOWNERS)
on the high-risk paths and by named sign-off on the decisions.
→ [`docs/ai_cto/escalation_boundary.md`](./escalation_boundary.md)

---

## The named-human roles

The thesis needs *names*, not titles. Three roles cover the legal/partner floor:

| Role | Who | Why it's required |
|---|---|---|
| **Privacy / Compliance accountable person** | The domain-expert co-founder | PIPEDA Sch. 1 cl. 4.1 requires a *named* privacy-accountable individual; owns breach-reporting calls |
| **Technical owner of record** | The founder-engineer | Owns money-flow, auth/identity, and PII-storage architecture decisions and production go-live |
| **Model Approver (credit engine)** | **Both**, jointly | OSFI E-23 requires a named human Model Approver for credit models (with extra AI/ML governance) |

**No full-time CTO is required** by any law, bank/sponsor partner, or insurer we are
aware of. What they require is a *named accountable human* for each domain — which
these roles provide. The AI-CTO system is how two people credibly cover the
engineering-governance surface a CTO would otherwise own.

---

## Build vs. buy

**BUILD layers 1–4 now** (this system). They are continuous, code-resident, and only
valuable if they run on every change — that's an engineering asset, not a purchasable
report. We own them.

**BUY point-in-time independent assurance** — **SOC 2 Type II + an independent
penetration test + a PIPEDA/privacy review** — *before real money moves*, scoped to
exactly what bank/sponsor partners and cyber-insurers actually demand. This is an
**engagement, not a salary**: independent attestation is worthless if self-issued, so
it must be bought, but it's a bounded procurement, not a full-time hire.

> **Cost note.** Specific dollar figures for SOC 2 / pen test / privacy review were
> **not reliably verifiable** at the time of writing and vary widely by scope and
> firm. **Get real quotes** before budgeting; do not treat any single number as
> authoritative.

This split is the whole point: build the *continuous* governance, buy the
*point-in-time* attestation, and keep a *named human* on the decisions the law won't
let a machine make.

---

## Cross-references

- [`docs/ai_cto/adversarial_review.md`](./adversarial_review.md) — Layer 1
- [`docs/security/sast.md`](../security/sast.md) — Layer 2 (SAST)
- [`docs/security/supply_chain.md`](../security/supply_chain.md) — Layer 2 (deps/SBOM)
- [`docs/compliance_controls.md`](../compliance_controls.md) — Layer 3
- [`.github/workflows/scheduled-audit.yml`](../../.github/workflows/scheduled-audit.yml) — Layer 4
- [`docs/ai_cto/escalation_boundary.md`](./escalation_boundary.md) — Layer 5
- [`.github/CODEOWNERS`](../../.github/CODEOWNERS) — Layer 5 enforcement
