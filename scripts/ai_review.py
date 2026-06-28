#!/usr/bin/env python3
"""Cross-model adversarial AI code-review harness for pull requests.

ONE part of PaySpyre's automated "CTO system". The builder model on this repo is
Claude (Anthropic). This harness sends a PR's unified diff to a DIFFERENT vendor's
model and asks it to adversarially review the change. The whole point is cross-MODEL
independence: a model reviewing its own lineage is an echo chamber, so the reviewer
should be OpenAI/GPT when the builder is Claude.

This is ADVISORY only, never a gate. AI-only review is noisy and underperforms the
deterministic gates (tests / bandit / pip-audit / gitleaks / fuzz) that already run on
this repo; treat its output as a second pair of eyes, not a verdict. The reviewer is
told to focus on three lenses that matter for a lending platform:
  1. Security    — authz / IDOR, injection, secret handling
  2. Money path  — loan / payment / disbursement correctness
  3. Compliance  — APR / cap / SIN / consent invariants

INERT until a key is configured. If neither OPENAI_API_KEY nor ANTHROPIC_API_KEY is
set, it prints a clear "not configured" message and exits 0. Every failure mode
(missing key, empty diff, API error, network error) is handled and exits 0 — this
harness must NEVER fail a build.

Zero extra dependencies: stdlib + urllib only. The provider SDKs are not assumed to be
installed; we call the REST APIs directly.

Usage:
    python scripts/ai_review.py                 # diffs origin/main...HEAD
    python scripts/ai_review.py path/to.diff    # reviews a diff file
    git diff origin/main...HEAD | python scripts/ai_review.py -   # reads stdin
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

# --- Tunables --------------------------------------------------------------------
# Keep the diff payload bounded so we never blow the model context window or rack up a
# surprise bill on a giant PR. Truncation is noted in the prompt.
MAX_DIFF_CHARS = 120_000
HTTP_TIMEOUT_SECONDS = 120
ANTHROPIC_MODEL = os.environ.get("AI_REVIEW_ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
OPENAI_MODEL = os.environ.get("AI_REVIEW_OPENAI_MODEL", "gpt-4o")

SYSTEM_PROMPT = """\
You are a senior staff security & correctness engineer performing an ADVERSARIAL code \
review of a pull request on PaySpyre, a Canadian consumer-lending platform. The code \
under review was written by a different AI model (Claude). Your job is to find what it \
got wrong — assume it is hiding a bug and try to REJECT the change. Do not be polite or \
agreeable; be skeptical and concrete.

Review ONLY the diff. Do not invent issues about code you cannot see, but DO flag where \
the diff plausibly breaks an invariant that lives outside the diff.

Focus your scrutiny on three lenses, in priority order:

1. SECURITY
   - Broken authorization / IDOR: can a user act on another tenant's loan, payment,
     application, or PII by changing an id? Is every mutating route ownership-checked?
   - Injection: raw SQL string building, shell, template, or unsanitized user input.
   - Secret handling: keys/tokens logged, returned in responses, or committed.

2. MONEY PATH
   - Loan / payment / disbursement correctness: rounding, sign, currency, double-spend,
     idempotency on retries, race conditions, balance going negative, off-by-one on
     schedules, missing transaction boundaries / post-commit ordering.

3. COMPLIANCE (Canada)
   - APR / interest-cap invariants, fee caps, SIN handling (storage/masking/logging),
     consent capture, adverse-action / disclosure requirements.

Output STRICT markdown in exactly this shape:

## AI adversarial review (advisory)

**Verdict:** REQUEST CHANGES | COMMENT | (no blocking issues found)

### Findings
For each issue, a bullet:
- **[SEVERITY]** `file:line` — concise description of the issue. _Fix:_ concrete fix.

SEVERITY is one of: CRITICAL, HIGH, MEDIUM, LOW. Order findings most-severe first. \
Use the real file path and line number from the diff hunk headers. If you find no \
real issues, say so plainly under Findings and set Verdict to "(no blocking issues \
found)" — do NOT pad with nitpicks.

End with one line:
> _Advisory only — augments human review, never replaces it on money/PII paths._
"""


def _read_diff_from_args_or_stdin() -> str:
    """Resolve the diff text from: explicit file arg, stdin ('-' or piped), or git."""
    args = [a for a in sys.argv[1:] if not a.startswith("-")] + [
        a for a in sys.argv[1:] if a == "-"
    ]
    # Explicit stdin request.
    if "-" in sys.argv[1:]:
        return sys.stdin.read()
    # Explicit file path.
    if args:
        path = args[0]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError as exc:
            print(f"AI review: could not read diff file '{path}': {exc}")
            return ""
    # Piped stdin (no tty) with no args.
    if not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped.strip():
            return piped
    # Fall back to computing the PR diff.
    return _git_diff()


def _git_diff() -> str:
    """Best-effort `git diff origin/main...HEAD`. Returns '' on any failure."""
    for ref in ("origin/main", "main"):
        try:
            out = subprocess.run(
                ["git", "diff", f"{ref}...HEAD"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0:
            return out.stdout
    return ""


def _pick_provider() -> tuple[str | None, str | None]:
    """Choose (provider, api_key).

    The builder is Claude, so for true cross-model independence we PREFER OpenAI/GPT as
    the reviewer. We only fall back to Anthropic if OpenAI is unavailable (echo-chamber
    review is still better than no review, but we warn).
    """
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if openai_key:
        return "openai", openai_key
    if anthropic_key:
        return "anthropic", anthropic_key
    return None, None


def _user_prompt(diff: str) -> str:
    truncated = ""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS]
        truncated = (
            "\n\n[diff truncated to "
            f"{MAX_DIFF_CHARS} chars — review what is shown and note that the tail "
            "was not included]"
        )
    return (
        "Adversarially review the following unified diff. Try to reject it; list "
        "concrete issues with file:line, severity, and a fix.\n\n"
        "```diff\n" + diff + "\n```" + truncated
    )


def _http_post_json(url: str, headers: dict, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _review_openai(api_key: str, diff: str) -> str:
    body = _http_post_json(
        "https://api.openai.com/v1/chat/completions",
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        {
            "model": OPENAI_MODEL,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(diff)},
            ],
        },
    )
    return body["choices"][0]["message"]["content"].strip()


def _review_anthropic(api_key: str, diff: str) -> str:
    body = _http_post_json(
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 4096,
            "temperature": 0.0,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": _user_prompt(diff)}],
        },
    )
    return "".join(
        block.get("text", "")
        for block in body.get("content", [])
        if block.get("type") == "text"
    ).strip()


def main() -> int:
    provider, api_key = _pick_provider()

    if not provider:
        print(
            "## AI adversarial review\n\n"
            "AI review not configured (set OPENAI_API_KEY or ANTHROPIC_API_KEY).\n\n"
            "The builder model on this repo is Claude, so for true cross-model "
            "independence set **OPENAI_API_KEY** (a different vendor) as the reviewer. "
            "This step is advisory and intentionally inert until a key is present."
        )
        return 0

    if provider == "anthropic":
        print(
            "> ⚠️ Reviewing with Anthropic while the builder is also Claude — this is a "
            "same-lineage (echo-chamber) review. Set OPENAI_API_KEY for true "
            "cross-model independence.\n"
        )

    diff = _read_diff_from_args_or_stdin()
    if not diff.strip():
        print(
            "## AI adversarial review\n\n"
            "No diff to review (empty diff). Skipping."
        )
        return 0

    try:
        if provider == "openai":
            review = _review_openai(api_key, diff)
        else:
            review = _review_anthropic(api_key, diff)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001 — never let cleanup crash the build
            pass
        print(
            "## AI adversarial review\n\n"
            f"AI review skipped: {provider} API returned HTTP {exc.code}. {detail}"
        )
        return 0
    except (urllib.error.URLError, KeyError, ValueError, OSError) as exc:
        print(
            "## AI adversarial review\n\n"
            f"AI review skipped: could not complete {provider} request ({exc})."
        )
        return 0

    if not review:
        print(
            "## AI adversarial review\n\n"
            f"AI review skipped: {provider} returned an empty response."
        )
        return 0

    print(review)
    print(f"\n<sub>Reviewer model: {provider} · advisory, non-blocking.</sub>")
    return 0


if __name__ == "__main__":
    # Belt-and-suspenders: even an unforeseen error must exit 0 so the build never fails.
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"## AI adversarial review\n\nAI review skipped (unexpected error: {exc}).")
        sys.exit(0)
