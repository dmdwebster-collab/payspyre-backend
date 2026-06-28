# Supply-chain & secret scanning

This document describes the **advisory** supply-chain visibility layer wired into
CI by [`.github/workflows/security-supplychain.yml`](../../.github/workflows/security-supplychain.yml)
and the dependency-update automation in
[`.github/dependabot.yml`](../../.github/dependabot.yml).

## What runs

The `Supply-chain + secret scanning (advisory)` workflow runs on every
pull request, on push to `main`, and weekly (Mondays 08:00 UTC). The weekly
schedule re-scans **unchanged** code against newly-disclosed CVEs.

| Job             | Tool         | What it does |
| --------------- | ------------ | ------------ |
| `secret-scan`   | gitleaks     | Scans the full git history for committed credentials (API keys, tokens, private keys — 140+ types). |
| `dep-audit`     | pip-audit    | Audits installed Python dependencies against the PyPI Advisory / OSV databases for known CVEs. |
| `sbom`          | cyclonedx-bom | Generates a CycloneDX SBOM (Software Bill of Materials) of the dependency graph and uploads it as the `cyclonedx-sbom` build artifact (90-day retention). |

[Dependabot](../../.github/dependabot.yml) opens weekly PRs for two ecosystems:
- **pip** — Python dependencies declared in `pyproject.toml`.
- **github-actions** — action versions pinned across `.github/workflows/*.yml`.

## Advisory today (non-blocking)

**Every job in this workflow is non-blocking.** Each uses
`continue-on-error: true`, and the install/scan steps tolerate tool-not-found and
no-findings conditions without failing. A finding here will **report** (annotation
/ artifact / red-but-not-required check) but will **never gate a merge**.

This is deliberate: it gives the team supply-chain visibility — an SBOM, a CVE
feed, a secret tripwire, and dependency-update PRs — without blocking delivery
while the signal is tuned and the existing baseline is triaged.

> Note: the separate **`Security scan (L0)`** workflow
> ([`security-scan.yml`](../../.github/workflows/security-scan.yml)) is the
> *blocking* gate (bandit + pip-audit + gitleaks fail the build). This advisory
> workflow is the broader, tolerant companion that adds SBOM generation, a weekly
> cadence, and Dependabot — none of which should ever block a merge.

## Making a check blocking later

When a check's signal is clean and trusted, promote it to a required gate:

1. **Remove `continue-on-error: true`** from the job (and from its scan step) in
   `security-supplychain.yml` so a non-zero exit fails the workflow.
2. For `dep-audit`, drop the `|| echo "::warning::..."` fallbacks on the actual
   `pip-audit` step so findings produce a non-zero exit.
3. In **GitHub → Settings → Branches → Branch protection rules** for `main`, add
   the job (e.g. `pip-audit (dependency CVEs)`) to **Require status checks to
   pass before merging**.

Promote one check at a time, starting with `secret-scan` (lowest false-positive
rate), then `dep-audit` once the dependency baseline is CVE-clean.
