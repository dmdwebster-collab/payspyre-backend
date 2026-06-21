# PaySpyre Continuous Test System

The goal: test **every system, continuously, until it's provably correct** — money
math, APIs, data, concurrency, the frontend, security, and resilience — and keep
testing forever via an always-on loop gated by an SRE error budget.

This design is grounded in a verified research pass on how fintech/lending SaaS
platforms test (sources cited inline). It maps each layer to the industry-standard
**open-source** tool and to PaySpyre's stack (FastAPI + SQLAlchemy + Postgres /
React + TS + Vite / DigitalOcean App Platform).

## The pyramid + the loop

```
                    ┌─────────────────────────────────────────┐
   ALWAYS-ON  ◄──── │  L8 Synthetic monitoring (k6, scheduled) │  prod/staging, every N min
   (cron)           │  L7 Chaos / fault injection (Toxiproxy)  │
                    │  L6 Load / soak / spike (k6, Locust)     │  SLO thresholds
                    ├─────────────────────────────────────────┤
   ON DEPLOY  ◄──── │  L5 Live E2E + concurrency smoke         │  e2e_smoke / concurrency_smoke ✅
                    ├─────────────────────────────────────────┤
   EVERY PUSH ◄──── │  L4 Frontend: component + E2E + a11y     │  Vitest + Playwright + axe
   (CI gates)       │  L3 API contract / schema fuzz           │  Schemathesis (OpenAPI)
                    │  L2 Integration + DB/migration           │  pytest + real Postgres ✅
                    │  L1 Unit + PROPERTY-BASED (money core)   │  pytest ✅ + Hypothesis
                    │  L0 Static + supply chain                │  ruff/mypy/eslint + bandit + pip-audit + gitleaks
                    └─────────────────────────────────────────┘
              GOVERNANCE: SRE error-budget policy (budget = 1 − SLO) gates release velocity
```

## Layer-by-layer (tool → why → status)

| L | Concern | Tool (verified) | Why this tool | PaySpyre status |
|---|---|---|---|---|
| L0 | Lint/type | ruff, mypy, eslint, tsc | fast feedback | partial |
| L0 | SAST | **bandit** | Python security linter | ❌ → add to CI |
| L0 | SCA (deps) | **pip-audit**, **OWASP Dependency-Check** | known-CVE deps | ❌ → add to CI |
| L0 | Secrets | **Gitleaks** (140+ types), TruffleHog | prevent leaked keys | ❌ → add to CI |
| L1 | Unit | pytest | — | ✅ 775 tests |
| L1 | **Property-based money invariants** | **Hypothesis** (incl. rule-based stateful) | "generates entire test sequences… finding sequences that result in a failure" — the right layer for amortization/balance/idempotency invariants ([hypothesis.works](https://hypothesis.works/articles/rule-based-stateful-testing/)) | ❌ → **build first** |
| L1 | Mutation ("test the tests") | **mutmut** / Cosmic Ray | score the suite on the money modules | ❌ → add |
| L2 | Integration | pytest | — | ✅ |
| L2 | DB/migration | **pytest-postgresql** (real PG) | catch schema drift/constraints/up-down ([repo](https://github.com/dbfixtures/pytest-postgresql)) | ✅ (local PG + round-trip tests) |
| L3 | **API contract / schema fuzz** | **Schemathesis** | auto-derives semantics-aware fuzzers from FastAPI `/openapi.json`; strongest OSS API fuzzer; catches 500s, schema violations, **info-disclosure (PII)**, malformed-money acceptance ([repo](https://github.com/schemathesis/schemathesis), [ICSE'22](https://arxiv.org/pdf/2112.10328)) | ❌ → **build** |
| L4 | Frontend component/interaction/a11y/visual | **Storybook 9** (Vitest + Playwright + **axe-core**) or Vitest+Testing Library + Playwright | unified runner; axe catches ~57% WCAG ([SB9](https://storybook.js.org/blog/storybook-9/)) | ❌ **zero FE tests** → build |
| L4 | Frontend E2E | **Playwright** | real-browser flows | ❌ → build |
| L5 | Live E2E + concurrency | `scripts/e2e_smoke.py`, `scripts/concurrency_smoke.py` | full-flow + money-race | ✅ (caught a real double-charge) |
| L6 | Load/soak/spike | **k6**, **Locust** | SLO validation; Locust = Python-native ([k6](https://grafana.com/docs/k6/latest/), [locust](https://github.com/locustio/locust)) | ❌ → build |
| L7 | Chaos/resilience | **Toxiproxy** / LitmusChaos | vendor-failure injection (Equifax/Flinks/Zumrails down) | ❌ → add |
| L8 | Synthetic monitoring | **k6 scheduled** | "schedule k6 smoke tests for continuous production monitoring; alerts via Grafana" ([k6 synthetics](https://grafana.com/docs/k6/latest/testing-guides/synthetic-monitoring/)) | ❌ → **build the always-on runner** |
| — | Governance | **SRE error-budget policy** | budget = 1 − SLO; exhausted → halt non-critical releases ([Google SRE](https://sre.google/workbook/error-budget-policy/)) | ❌ → define SLOs |

## Build order (highest value first)
1. **L1 Hypothesis money-invariant suite** — amortization sums, balance conservation, payoff, marketplace pricing, idempotency. *(Most likely to find real bugs.)*
2. **L3 Schemathesis** contract/fuzz over the live FastAPI schema.
3. **L0 CI security**: bandit + pip-audit + gitleaks workflow.
4. **L8 Always-on runner**: scheduled GitHub Actions cron running e2e_smoke + concurrency_smoke + a k6 synthetic check against staging.
5. **L4 Frontend**: Vitest + Testing Library component tests + a Playwright E2E.
6. **L6 k6 load** + **L1 mutation** + **L7 chaos** as hardening rounds.

## Continuous "always-on" loop
A scheduled runner executes L5 + L8 against staging on a cron, records pass/fail +
latency, and (per the error-budget policy) alerts when the SLO is breached. Combined
with CI gating every push and L5 on every deploy, **every system is exercised
repeatedly and indefinitely** — the "test over and over until perfect" requirement.

> Caveats from the research: Pact consumer-driven contracts, golden-master snapshots,
> and PCI-DSS/SOC-2 control mappings had no verified sources in this pass and are
> applied from first principles; the Schemathesis benchmark is vendor-co-authored
> (2021) and the chaos claim rests on a vendor page — both attributed accordingly.
