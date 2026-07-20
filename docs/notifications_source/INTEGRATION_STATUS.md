# Dave's notification templates — integration status

**Date:** 2026-07-19 · **Source:** Dave's OneDrive `PaySpyre/Notifications` docs (v1.0, 2026-07-15), extracted to this folder.

All 51 notifications from Dave's five docs are now **in the platform as code** —
47 registry types (some of Dave's rows folded into one parameterized type).
Copy is Dave's, with typo fixes and the adaptations listed in
`MERGEFIELD_MAP.md` (magic-link instead of passwords; our portal URLs instead
of Turnkey's; generalized payment steps).

## What sends automatically today (no action needed)

| Trigger | Notification(s) |
|---|---|
| Application approved (decision engine) | `application_approved` — Dave's "Loan Pre-Approved - Acceptance Required" |
| Application declined | Adverse-action notice (compliance copy, unchanged — see Open questions) |
| Agreement sent for e-signature | `offer_accepted_signing` |
| Agreement signed (SignNow callback) | `agreement_signed` |
| Loan disbursed / activated | `loan_activated` |
| Payment recorded | `payment_received` |
| Final payment | `payment_received` + `loan_repaid` |
| Loan charged off | `loan_written_off` |
| 7 / 2 days before autopay | `payment_due_reminder` (Dave's autopay copy) |
| 7 / 14 / 21 / 30 / 60 / 90 days past due | `payment_overdue` (tiered copy; 90-day = default notice, Collections tone) |

Cadence + channels + copy are editable per type in the cockpit
(`/api/v1/admin/config/notification-rules`) — no developer needed. Defaults
match Dave's docs exactly (email-only; 7,2 before / 7,14,21,30,60,90 after).

## Registered, wired when the module/creds arrive

- **Bank verification** (request / reminder / expired) — needs Flinks creds.
- **NSF + payment error** — needs Zumrails live (real payment failure webhooks).
- **PTP recorded / broken, deferment approved / rejected** — needs the
  promise-to-pay / hardship module (not built; Turnkey parity P1).
- **Welcome / registration / sign-in link** — borrower-portal login emails;
  the portal's magic-link flow has its own send today, swap-in pending.
- **Offer expired / signature reminder / signature expired** — needs the
  offer-expiry + reminder cron (small; scheduled with dunning crons).
- **Back-office + vendor notices (17)** — copy shipped; wiring arrives with
  the staff-assignment feature (no assignee model yet).
- **`loan_cancelled`** — no cancel transition exists on booked loans yet.

## Open questions for Dave

1. **Rejection email**: your softer "Application Rejection" copy is registered
   (`application_rejected`) but declines still send the adverse-action notice —
   counsel should confirm which satisfies notice obligations before we swap.
2. **Welcome-email attestations**: reworded "will not share login credentials"
   to "will not share access to your account or sign-in links" (passwordless
   portal). Confirm wording.
3. **CompanyPhone**: templates render a phone number wherever your docs used
   `*|CompanyPhone|*` — set `COMPANY_PHONE` when there's a support line.
