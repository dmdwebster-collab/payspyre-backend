# Vendor self-serve disbursements (W2-DISB)

Turnkey-parity **video 10 "Vendor Access"** — the biggest net-new ask (Dave's
"best case scenario", doesn't exist in Turnkey). Spec: `10__Vendor_Access.md`
§2 "Disbursements to vendor", §4.7, §5 "Disbursements (the big new ask)".

## What it does

Gives each vendor (clinic) a self-serve payout surface on their **own**
portfolio:

- **MTD collected** — payments collected this month on the vendor's loans.
- **Due to vendor** — the vendor's share of *cleared* collections.
- **Available now** — releasable balance, net of the clearing holdback and any
  settled / in-flight payouts. The vendor controls when they get paid.
- A **free monthly auto-payout** of the undispersed balance (parked cron), plus
  vendor-initiated **extra** payouts that carry a transaction fee.
- Payout fires straight through the **Zumrails** processor integration
  (direct processor→vendor), keeping PaySpyre out of "intermediary" territory.

## Wallet math (derived, not stored)

```
holdback_cutoff   = as_of − VENDOR_DISBURSEMENT_HOLDBACK_BUSINESS_DAYS  (WS-F calendar)
cleared_collected = net payments (payment − reversal) with effective_date ≤ cutoff
due_to_vendor     = cleared_collected × VENDOR_DISBURSEMENT_SHARE_BPS / 10000
available         = max(0, due_to_vendor − disbursed_settled − disbursed_in_flight)
```

Computed on every read from the existing money ledger
(`platform_loan_transactions`) minus this engine's own payouts. It only READS
the ledger — no interest/ledger math is touched. Reversals net conservatively
(never over-pays a vendor).

## Surface

| Endpoint | Auth | Money |
|---|---|---|
| `GET  /clinic/v1/disbursements/wallet` | clinic (own vendor) | read-only |
| `GET  /clinic/v1/disbursements` | clinic (own vendor) | read-only |
| `POST /clinic/v1/disbursements/request` | clinic + `loan_servicing` role | **flag-gated** extra payout |
| `GET  /admin/disbursements/vendors/{id}/wallet` | admin/staff | read-only |
| `GET  /admin/disbursements` | admin/staff | read-only ledger |
| `POST /admin/disbursements/run-monthly` | admin/staff | **flag-gated** monthly sweep |
| cron `python -m app.jobs.vendor_disbursements` | — | **flag-gated** monthly sweep |

## Safety — money-out master flag

`VENDOR_DISBURSEMENTS_ENABLED` (default **False**), mirroring
`AUTO_COLLECTION_ENABLED`. While off, wallet READS stay live but **no cent can
move** — the extra-payout endpoint returns 409 and the sweep is a strict no-op.

Idempotency spine (mirrors `platform_collection_attempts`): a `pending` row is
CLAIMED (committed) under a UNIQUE `client_transaction_id` **before** the
Zumrails push. The monthly sweep's id is deterministic
(`vdisb-auto-{vendor}-{YYYYMM}`), so a re-run in the same month is a no-op.

## Activation requires (Mike must review before flipping the flag)

1. `VENDOR_DISBURSEMENTS_ENABLED=true`.
2. The `zumrails` `integration_settings` row enabled + buildable (real or
   sandbox creds) — "real adapters on" is governed by integration_settings,
   not the flag.
3. **Dave decisions (placeholders flagged in `config.py`):**
   - `VENDOR_DISBURSEMENT_SHARE_BPS` — the real vendor revenue-share (default
     10000 = 100% is a placeholder).
   - `VENDOR_DISBURSEMENT_EXTRA_FEE_CENTS` — the per-extra-payout fee (default 0).
   - `VENDOR_DISBURSEMENT_HOLDBACK_BUSINESS_DAYS` — 4 per Dave, confirm.
4. A **vendor → Zumrails recipient** mapping (`_recipient_id_for_vendor`
   currently returns None → every push safely skips until wired).

## Descoped (follow-ups)

- **Async webhook settlement wiring.** `on_disbursement_settled` /
  `on_disbursement_failed` hooks exist but are not yet called from the Zumrails
  webhook endpoint. The simulator settles synchronously on the create ack, which
  is sufficient for flag-off / staging; wave-2 integration wires the async path.
- **Vendor→recipient onboarding UI/model** (funding-profile mapping).
- **Concurrency hardening** for two truly-simultaneous extra-payout requests
  (sequential requests are already safe; unique-id + in-flight subtraction cover
  the common case).
- **Statement/receipt document** for each payout (Documents tab).
