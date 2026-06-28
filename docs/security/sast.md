# Static Analysis (SAST) — Semgrep guardrails

This repo runs [Semgrep](https://semgrep.dev) in CI as part of the automated
"CTO system" to catch money-path, PII, and general-security regressions in the
backend (where the money path, PII, and compliance code live).

**Status: ADVISORY / NON-BLOCKING.** The `Security SAST (Semgrep, advisory)`
workflow (`.github/workflows/security-sast.yml`) runs on every pull request and
on pushes to `main`, but it **never fails the build** — every scan step uses
`continue-on-error: true` (and `|| true`). Findings are surfaced in the job
summary and uploaded as a SARIF artifact (`semgrep-sarif`) for review; they do
not gate merges today.

## What runs

| Source | What it is |
| --- | --- |
| `.semgrep/payspyre.yml` | Custom PaySpyre guardrail rules (below) |
| `p/python` | Public OSS Semgrep pack: Python correctness/security |
| `p/security-audit` | Public OSS Semgrep security-audit pack |

All of this uses the community Semgrep OSS engine — no Semgrep account or token
is required.

## Custom rules (`.semgrep/payspyre.yml`)

| Rule id | Severity | What it enforces |
| --- | --- | --- |
| `application-status-write-outside-orchestrator` | ERROR | `application.status` / `credit_application.status` / `credit_app.status` may only be assigned in `app/services/flow_orchestrator.py`. The scan step excludes that file, so the rule fires anywhere else. Mirrors `tests/test_application_status_writes.py`. |
| `raw-sin-into-jsonb-dict` | ERROR | Flags assigning a raw value into a dict under a `social_insurance_number` / `sin` key without routing it through `app.core.sin_crypto.encrypt_sin`. The full SIN must never be persisted in plaintext. |
| `raw-sin-in-dict-literal` | WARNING | Flags a `social_insurance_number` / `sin` key in a dict literal whose value didn't go through `encrypt_sin` — manual verification cue. |
| `no-eval` | ERROR | `eval(...)` — arbitrary code execution / RCE risk. |
| `no-exec` | ERROR | `exec(...)` — arbitrary code execution / RCE risk. |
| `subprocess-shell-true` | ERROR | `subprocess.*(..., shell=True, ...)` — command-injection risk. |
| `sql-string-built-with-fstring` | ERROR | SQL passed to `execute()` / `text()` built from an f-string containing a SQL verb — SQL-injection risk. Use parameterized queries. |
| `sql-string-built-with-format-or-percent` | ERROR | SQL built with `.format()` or `%` concatenation — SQL-injection risk. |
| `hardcoded-secret-literal` | WARNING | A secret-named variable (`*password*`, `*secret*`, `*api_key*`, `*token*`, `*client_secret*`, …) assigned a non-trivial (8+ char, non-placeholder) string literal. Secrets must come from `app.core.config.settings` / env. |

The rules are deliberately **conservative** to minimize false positives:

- The SIN rules explicitly `pattern-not` values produced by `encrypt_sin(...)`,
  and never match the masked `sin_last3` (a different key).
- The SQL rules require a SQL verb (`select`/`insert`/`where`/…) to be present,
  so non-SQL f-strings passed to other `execute()` methods aren't flagged.
- The secret rule excludes name-like fields (`*_name`, `*_field`, `*_header`,
  `*_url`, `*_id`, …) and placeholder literals (`changeme`, `example`, `your_`,
  format strings), and requires an 8+ char literal.

## Running locally

```bash
pip install semgrep    # or: brew install semgrep

# Custom guardrails only (matches the CI step):
semgrep scan --config .semgrep/payspyre.yml \
  --exclude app/services/flow_orchestrator.py app/

# Everything CI runs:
semgrep scan \
  --config .semgrep/payspyre.yml \
  --config p/python \
  --config p/security-audit \
  --exclude app/services/flow_orchestrator.py app/
```

## Making it blocking later

When the team is ready to gate merges on Semgrep:

1. In `.github/workflows/security-sast.yml`, remove `continue-on-error: true`
   from the scan step(s) you want to enforce, and drop the trailing `|| true`
   so a non-zero Semgrep exit code fails the job. (Semgrep exits non-zero when
   it finds matches at the configured severity. You can scope this with
   `--severity ERROR` to block only on ERROR-level findings while keeping
   WARNING advisory.)
2. Optionally split the job: keep the public OSS packs advisory and make only
   the custom `.semgrep/payspyre.yml` guardrails blocking (or vice-versa).
3. Mark the `Semgrep SAST` check as **required** in the branch-protection rules
   for `main` (see `BRANCH_PROTECTION.md`).

Until then this is informational only — it raises visibility without slowing
delivery.
