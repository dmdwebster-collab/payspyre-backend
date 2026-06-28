# Cross-model adversarial AI review

Part of PaySpyre's automated "CTO system". This is an **advisory** PR-review layer: one
vendor's model writes the code, a **different** vendor's model critiques it. It is
**inert until an API key is configured** and it can **never fail a build**.

- Script: [`scripts/ai_review.py`](../../scripts/ai_review.py)
- Workflow: [`.github/workflows/ai-review.yml`](../../.github/workflows/ai-review.yml)

## Why cross-model independence matters

The code on this repo is built by **Claude (Anthropic)**. If the reviewer were also
Claude, the review would be an **echo chamber** — a model is systematically blind to the
same classes of mistakes in its own output that it made writing it, and it tends to
rationalize rather than reject. Independence comes from using a **different vendor's
model** as the reviewer. So:

> **Builder = Claude ⇒ Reviewer should be OpenAI / GPT.**

The harness encodes this preference: if `OPENAI_API_KEY` is present it uses OpenAI; it
only falls back to `ANTHROPIC_API_KEY` (and prints a same-lineage warning) when OpenAI
is unavailable.

## What it is — and what it is NOT

Research is consistent that **AI-only review underperforms and is noisy**: it misses real
bugs, invents non-issues, and cannot be trusted as a merge gate. So this layer is an
**augmentation, not a gate**:

- It **augments human review** and the **deterministic gates** that actually protect the
  repo (`tests`, `security-scan` = bandit + pip-audit + gitleaks, the Schemathesis fuzz).
- It is **non-blocking**: `scripts/ai_review.py` always exits `0` (missing key, empty
  diff, API error, network error, even an unexpected exception). The workflow cannot fail
  a build.
- **It never replaces a human on money or PII paths.** Loan/payment/disbursement changes
  and anything touching SIN, consent, or borrower PII still require human sign-off.

The reviewer is given an adversarial system prompt — it is told the code is Claude's, to
**try to reject** the change, and to report concrete issues as `[SEVERITY] file:line —
issue. Fix: …`. It focuses on three lenses:

1. **Security** — broken authorization / IDOR, injection, secret handling.
2. **Money path** — loan / payment / disbursement correctness (rounding, sign, currency,
   idempotency, double-spend, race conditions, transaction boundaries).
3. **Compliance (Canada)** — APR / interest-cap / fee-cap invariants, SIN handling,
   consent capture, adverse-action / disclosure requirements.

## How to enable it

The workflow runs on every PR but is inert until a key exists.

1. Create an **OpenAI** API key (recommended — cross-vendor from the Claude builder).
2. Add it as a repository secret named **`OPENAI_API_KEY`**:
   - GitHub → repo **Settings → Secrets and variables → Actions → New repository secret**,
     or: `gh secret set OPENAI_API_KEY`
3. That's it. The next PR gets an adversarial review posted as a comment.

> Fallback only: setting `ANTHROPIC_API_KEY` instead makes the reviewer Claude as well.
> The harness will run but prints a same-lineage (echo-chamber) warning — prefer OpenAI.

### Optional tuning (env vars)

- `AI_REVIEW_OPENAI_MODEL` (default `gpt-4o`)
- `AI_REVIEW_ANTHROPIC_MODEL` (default `claude-3-5-sonnet-latest`)

### Running locally

```bash
# Review the current PR branch against main:
OPENAI_API_KEY=sk-... python scripts/ai_review.py

# Review a saved diff, or pipe one in:
python scripts/ai_review.py some.diff
git diff origin/main...HEAD | python scripts/ai_review.py -
```

With no key set it prints a "not configured" note and exits 0 — safe to run anywhere.

## Managed alternative (CodeRabbit / Greptile)

If you'd rather not self-host the harness, a managed cross-model reviewer is a reasonable
swap. Both install as a **GitHub App** and post inline PR review comments:

- **CodeRabbit** — https://coderabbit.ai
- **Greptile** — https://greptile.com

High-level setup (either one):

1. Install the GitHub App from the vendor's site / the GitHub Marketplace.
2. Authorize it on the PaySpyre org and grant access to the backend repo.
3. Configure it to run on pull requests (optionally add a repo config file, e.g.
   `.coderabbit.yaml`, to scope paths and tune verbosity).
4. Confirm it posts review comments on a test PR.

The **same caveat applies** to any managed tool: keep it **advisory and non-blocking**,
and **never** let it stand in for a human reviewer on money or PII paths.

## Caveat (load-bearing)

This layer **augments** human review; it does not replace it. On any change touching the
money path (loan / payment / disbursement) or PII (SIN / consent / borrower data), a human
must review and approve regardless of what the AI says.
