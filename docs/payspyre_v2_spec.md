# PaySpyre v2 — Platform-First Build Spec

**Status:** Draft v1.0 — ready for Claude Code
**Owner:** Michael Webster (PaySpyre)
**Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.x, Alembic, PostgreSQL (Supabase), Cloudflare Workers (edge), React 18 + Vite + TypeScript + Tailwind + shadcn/ui (frontends)
**Repo:** `payspyre-backend` (existing) — this spec extends the KYC orchestration work shipped in PRs 1–15
**Origin:** Dave Wilson's platform reframe (May 21, 2026) — collect-everything-once-consented, monetize the funnel beyond lending, credit-product configuration drives the flow

---

## 0. Read this first

This is **not** a greenfield build. It extends the existing `payspyre-backend` work:

- ✅ KYC orchestration layer (Didit/Persona vendor abstraction, append-only `kyc_events`, webhook HMAC + idempotency, state machine, risk-rule YAML engine)
- ✅ 5 KYC tables: `kyc_sessions`, `kyc_events`, `kyc_webhook_inbox`, `kyb_applications`, `kyb_beneficial_owners`
- ✅ FINTRAC audit export
- ✅ 11 starter risk rules

This spec adds the **platform layer** on top:

1. Platform-first **patient profile** (one record per human, lives independently of any application)
2. **Credit product configuration matrix** (per product / per amount bracket: which verifications run, what decision rules apply)
3. **Configurable flow engine** (the orchestrator that reads credit-product config and runs the right verifications in the right order)
4. **Marketplace lead engine** (lead tiering, clinic-side discovery, lead pricing)
5. **Monetization surfaces** (cross-sell catalog, advertising slots, aggregate-data products — scaffolded, most disabled at launch)
6. **Consent & audit extension** (per-purpose granular consent, supports both lending and non-lending data uses)

**Critical:** all hard rules from the prior KYC kickoff still apply. Specifically:
- `kyc_events` and the new `platform_events` table are **append-only** (REVOKE UPDATE, DELETE at the DB level)
- Webhooks must HMAC-verify before doing anything else
- Idempotency enforced at the DB unique-constraint level, not the application layer
- State transitions go through the allowed-transition table, no direct status writes
- Vendor abstractions are real, not nominal
- No PII in logs
- All thresholds/rules YAML-configurable

If anything in this spec contradicts the existing build, **stop and surface to Mike** — do not silently overwrite.

---

## 1. Mental model

PaySpyre v2 has four conceptual layers, in dependency order:

```
┌─────────────────────────────────────────────────────────────────┐
│  L4: MONETIZATION                                                │
│  Cross-sell catalog · Marketplace leads · Ad surfaces · Data    │
├─────────────────────────────────────────────────────────────────┤
│  L3: PRODUCTS                                                    │
│  Credit Products (configured) · Marketplace listings · Add-ons  │
├─────────────────────────────────────────────────────────────────┤
│  L2: FLOW ENGINE                                                 │
│  Runs verifications per product config, manages session state   │
├─────────────────────────────────────────────────────────────────┤
│  L1: PROFILE & VERIFICATIONS                                     │
│  Patient Profile (1 per human) · KYC sessions · Bank links ·    │
│  Bureau pulls · Consents · Events                                │
└─────────────────────────────────────────────────────────────────┘
```

A patient profile exists **independently of any application**. A patient can:
- Apply for financing (multiple times, multiple products)
- Be a marketplace lead (with no financing application at all)
- Be a cross-sell customer (credit monitoring, insurance, etc.)
- Have a verification depth (none / ID / ID+Bank / ID+Bank+CB) that gates which products they can access

Credit products are **configurations** — they declare which verifications they need, in what order, with what decision rules, at what amount brackets. The flow engine reads the config and runs.

---

## 2. Data model (new tables — Alembic migrations)

All new tables live in the `platform` schema namespace (existing KYC tables stay in `kyc`). Reference existing tables by FK.

### 2.1 `platform_patients`

The **canonical human record**. One row per person, ever.

```sql
CREATE TABLE platform_patients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Identity (canonical, verified where possible — source-tagged in platform_patient_fields)
    legal_first_name TEXT,
    legal_last_name TEXT,
    dob DATE,
    email TEXT,
    phone_e164 TEXT,

    -- SIN — encrypted at rest, never logged, never returned by default APIs
    -- Legally optional; declining is allowed but flagged for ops/decisioning per §5.7
    sin_encrypted BYTEA,
    sin_last3 CHAR(3),  -- for ops display/search only
    sin_collected_at TIMESTAMPTZ,
    sin_declined BOOLEAN NOT NULL DEFAULT false,
    sin_declined_at TIMESTAMPTZ,

    -- Verification depth (denormalized for fast filtering — sourced from platform_patient_verifications)
    verification_depth TEXT NOT NULL DEFAULT 'none',
        -- enum: 'none' | 'email_verified' | 'phone_verified' | 'id_verified'
        --     | 'id_bank_verified' | 'id_bank_cb_verified'

    -- Lead state (denormalized for marketplace queries)
    lead_state TEXT NOT NULL DEFAULT 'unqualified',
        -- enum: 'unqualified' | 'pre_qualified' | 'pre_approved' | 'approved' | 'declined'
    lead_state_updated_at TIMESTAMPTZ,

    -- Marketing & monetization flags
    marketing_consent_at TIMESTAMPTZ,  -- null = no marketing
    cross_sell_eligible BOOLEAN NOT NULL DEFAULT false,
    marketplace_listed BOOLEAN NOT NULL DEFAULT false,

    -- Soft-delete only (PIPEDA retention obligations)
    deleted_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX platform_patients_email_lower ON platform_patients (lower(email)) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX platform_patients_phone ON platform_patients (phone_e164) WHERE deleted_at IS NULL;
CREATE INDEX platform_patients_verification_depth ON platform_patients (verification_depth);
CREATE INDEX platform_patients_lead_state ON platform_patients (lead_state);
```

**Hard rule:** never UPDATE `legal_first_name`, `legal_last_name`, `dob` once they've been verified. New verified values get written to `platform_patient_fields` with source tagging; the denorm column is updated through a service function that records the change in `platform_events`.

### 2.2 `platform_patient_fields`

Every fielded value the patient has — with source tagging. **This is the source-of-truth audit trail.**

```sql
CREATE TABLE platform_patient_fields (
    id BIGSERIAL PRIMARY KEY,
    patient_id UUID NOT NULL REFERENCES platform_patients(id),
    field_key TEXT NOT NULL,
        -- 'legal_first_name', 'address_street', 'employer_name',
        -- 'monthly_income_after_tax', 'time_at_address_months', etc.
    field_value JSONB NOT NULL,  -- jsonb so we can store strings, numbers, structured values
    source TEXT NOT NULL,
        -- 'self_reported' | 'practice_prefill' | 'id_doc' | 'bureau_soft'
        -- | 'bureau_hard' | 'bank_aggregator' | 'didit_kyc' | 'manual_override'
    source_event_id BIGINT,  -- FK to kyc_events or platform_events for traceability
    confidence NUMERIC(3,2),  -- 0.00-1.00 where applicable
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_current BOOLEAN NOT NULL DEFAULT true,
    superseded_at TIMESTAMPTZ,
    superseded_by_id BIGINT REFERENCES platform_patient_fields(id)
);

CREATE INDEX platform_patient_fields_current ON platform_patient_fields (patient_id, field_key) WHERE is_current = true;
CREATE INDEX platform_patient_fields_source ON platform_patient_fields (source);
```

**Rule:** updates are inserts. The old row gets `is_current = false`, `superseded_at = now()`, `superseded_by_id = new.id`. Never DELETE.

### 2.3 `platform_credit_products`

The configurable credit product. **This is the table Dave's toggle matrix lives in.**

```sql
CREATE TABLE platform_credit_products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT NOT NULL UNIQUE,  -- 'dental_small_v1', 'dental_full_arch_v1', 'auto_dealer_v1'
    name TEXT NOT NULL,
    vertical TEXT NOT NULL,  -- 'dental' | 'auto' | 'veterinary' | future
    status TEXT NOT NULL DEFAULT 'draft',  -- 'draft' | 'active' | 'archived'

    min_amount_cents BIGINT NOT NULL,
    max_amount_cents BIGINT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CAD',

    -- The verification matrix: per amount bracket, which verifications run
    -- Stored as JSONB for flexibility; validated against a JSON Schema on write
    verification_matrix JSONB NOT NULL,
        -- See §3 for the schema

    -- Decision rules — references to YAML rule files in /config/decision_rules/
    decision_ruleset TEXT NOT NULL,  -- e.g. 'dental_small_v1.yaml'

    -- Pricing
    pricing_config JSONB NOT NULL,  -- {term_options: [12, 24, 36], apr_range: [9.99, 29.99], ...}

    -- Funding model
    funding_source TEXT NOT NULL,  -- 'payspyre_capital' | 'partner_lender' | 'hybrid' | 'clinic_self'

    created_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    version INTEGER NOT NULL DEFAULT 1
);
```

The `verification_matrix` JSONB is the heart of Dave's design. See §3 for its structure.

### 2.4 `platform_credit_applications`

A patient's specific application to a credit product. (Replaces / supersedes any existing `applications` table — confirm with Mike before dropping.)

```sql
CREATE TABLE platform_credit_applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id UUID NOT NULL REFERENCES platform_patients(id),
    credit_product_id UUID NOT NULL REFERENCES platform_credit_products(id),
    credit_product_version INTEGER NOT NULL,  -- snapshot of which product version they applied under

    -- Co-applicant linkage (see §2.11 and §4.5)
    -- If non-null, this row is a co-applicant attached to the primary application.
    -- The primary application's id has this column = NULL.
    co_applicant_of_application_id UUID REFERENCES platform_credit_applications(id),
    applicant_role TEXT NOT NULL DEFAULT 'primary',  -- 'primary' | 'co_applicant'

    -- Requested amount: source tagging — see §2.4.1
    requested_amount_cents BIGINT NOT NULL,
    requested_amount_source TEXT NOT NULL,
        -- 'clinic' | 'patient' | 'clinic_then_patient_adjusted'
    clinic_proposed_amount_cents BIGINT,  -- captured if clinic seeded the amount
    patient_proposed_amount_cents BIGINT, -- captured if patient entered/adjusted

    -- Origination context
    clinic_id UUID,  -- nullable for direct-to-consumer
    treatment_plan_ref TEXT,

    -- State
    status TEXT NOT NULL DEFAULT 'started',
        -- 'started' | 'verifying' | 'pre_qualified' | 'awaiting_hard_pull'
        -- | 'under_review' | 'approved' | 'declined' | 'withdrawn' | 'expired'
    status_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Flow state — which steps have run
    flow_state JSONB NOT NULL DEFAULT '{}'::jsonb,
        -- {steps_completed: ['id', 'soft_pull'], current_step: 'bank',
        --  step_results: {id: 'pass', soft_pull: 'pre_qualified'}}

    -- Outcome
    decision JSONB,  -- {result: 'approved', amount_cents: 250000, apr_bps: 1299, term_months: 24, reason_codes: [...]}
    decision_at TIMESTAMPTZ,
    decision_by TEXT,  -- 'auto' | 'manual:<operator_id>'

    -- Self-reported overrides (kept separate from verified fields)
    self_reported JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX platform_credit_applications_patient ON platform_credit_applications (patient_id);
CREATE INDEX platform_credit_applications_status ON platform_credit_applications (status);
CREATE INDEX platform_credit_applications_coapp ON platform_credit_applications (co_applicant_of_application_id) WHERE co_applicant_of_application_id IS NOT NULL;
```

**Rule:** a primary application can have at most one co-applicant row referencing it (enforced by partial unique index in PR P3.5). The co-applicant is its own full application row — it runs through the same flow engine with its own verifications. Scoring and decisioning happen at the **application group** level (see §4.5).

#### 2.4.1 `requested_amount` — how it's set

Per Dave: clinic + patient can both contribute. The application creation API accepts whichever is provided; if both are provided, both are stored and `requested_amount_cents` defaults to the clinic value unless the patient explicitly adjusts (which sets `requested_amount_source = 'clinic_then_patient_adjusted'`). Any patient adjustment > clinic proposed by more than 10% triggers a soft flag on the application for ops/clinic visibility (does not block — informational only).

### 2.5 `platform_verifications`

A row per *attempted* verification across all types. Links to the type-specific tables (`kyc_sessions`, bank link records, bureau pull records).

```sql
CREATE TABLE platform_verifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id UUID NOT NULL REFERENCES platform_patients(id),
    application_id UUID REFERENCES platform_credit_applications(id),  -- nullable for standalone verifications (marketplace, cross-sell)
    verification_type TEXT NOT NULL,
        -- 'kyc_id' | 'bank_link' | 'bureau_soft' | 'bureau_hard'
        -- | 'income_attestation' | 'address_proof'
    status TEXT NOT NULL DEFAULT 'pending',
        -- 'pending' | 'in_progress' | 'passed' | 'failed' | 'expired'
    vendor TEXT,  -- 'didit' | 'persona' | 'flinks' | 'inverite' | 'equifax_ca' | 'transunion_ca'
    vendor_session_ref TEXT,  -- FK-like reference into kyc_sessions / bank_links / bureau_pulls
    cost_cents INTEGER,  -- vendor cost (for unit economics tracking)
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    consent_id UUID REFERENCES platform_consents(id)  -- explicit consent that authorized this verification
);
```

### 2.6 `platform_consents`

Per-purpose, granular consent. **Critical for compliance — every verification ties back to a consent row.**

```sql
CREATE TABLE platform_consents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id UUID NOT NULL REFERENCES platform_patients(id),
    purpose TEXT NOT NULL,
        -- 'id_verification' | 'bank_verification' | 'soft_bureau_pull'
        -- | 'hard_bureau_pull' | 'automated_decision_making'
        -- | 'marketing_email' | 'marketplace_listing' | 'cross_sell_eligibility'
        -- | 'aggregate_data_use'
    consent_granted BOOLEAN NOT NULL,
    consent_text_shown TEXT NOT NULL,  -- IMMUTABLE — exact text the user saw
    consent_text_version TEXT NOT NULL,  -- e.g. 'id_verif_v2_2026-05'
    granted_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    ip_address INET,
    user_agent TEXT,
    application_id UUID REFERENCES platform_credit_applications(id)  -- nullable
);

CREATE INDEX platform_consents_patient_purpose ON platform_consents (patient_id, purpose) WHERE revoked_at IS NULL;
```

**Hard rule:** `consent_text_shown` and `consent_text_version` are **never UPDATEd**. If consent language changes, new consents reference the new version. Old consents preserve the old language verbatim. This is non-negotiable for class-action defense.

### 2.7 `platform_events`

The platform-wide append-only event log. Mirrors the design of `kyc_events`.

```sql
CREATE TABLE platform_events (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    patient_id UUID REFERENCES platform_patients(id),
    application_id UUID REFERENCES platform_credit_applications(id),
    event_type TEXT NOT NULL,
        -- 'patient.created' | 'patient.field_updated' | 'application.started'
        -- | 'application.status_changed' | 'verification.requested'
        -- | 'verification.completed' | 'consent.granted' | 'consent.revoked'
        -- | 'decision.made' | 'lead.state_changed' | 'marketplace.listed' | etc.
    actor TEXT NOT NULL,  -- 'system' | 'patient:<id>' | 'operator:<id>' | 'clinic:<id>' | 'vendor:<name>'
    payload JSONB NOT NULL,  -- event-specific data
    correlation_id UUID  -- for tracing a single flow across events
);

CREATE INDEX platform_events_patient ON platform_events (patient_id, occurred_at DESC);
CREATE INDEX platform_events_application ON platform_events (application_id, occurred_at DESC);
CREATE INDEX platform_events_type ON platform_events (event_type, occurred_at DESC);
```

Migration must `REVOKE UPDATE, DELETE ON platform_events FROM <app_role>;` — same pattern as `kyc_events`. Add a test that verifies the revoke took effect.

### 2.8 `platform_marketplace_listings`

A patient's marketplace listing (when they opt in).

```sql
CREATE TABLE platform_marketplace_listings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id UUID NOT NULL REFERENCES platform_patients(id),
    status TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'paused' | 'closed' | 'expired'

    -- What the patient needs
    treatment_categories TEXT[] NOT NULL,  -- ['general_dentistry', 'implants', 'orthodontics']
    treatment_urgency TEXT NOT NULL,  -- 'immediate' | 'this_week' | 'this_month' | 'flexible'
    estimated_budget_cents BIGINT,
    location_postal_code TEXT NOT NULL,
    max_travel_km INTEGER NOT NULL DEFAULT 25,

    -- Lead enrichment (derived from patient profile)
    lead_state TEXT NOT NULL,  -- denormed from platform_patients.lead_state
    verification_depth TEXT NOT NULL,  -- denormed

    -- Marketplace economics
    base_lead_price_cents INTEGER NOT NULL,  -- pricing engine fills this in
    accepted_clinic_id UUID,
    accepted_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
```

### 2.9 `platform_marketplace_clinic_interest`

Records when a clinic expresses interest in a listing — and when they're charged.

```sql
CREATE TABLE platform_marketplace_clinic_interest (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id UUID NOT NULL REFERENCES platform_marketplace_listings(id),
    clinic_id UUID NOT NULL,
    expressed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    selected_by_patient_at TIMESTAMPTZ,
    appointment_booked_at TIMESTAMPTZ,
    lead_charge_cents INTEGER,
    lead_charged_at TIMESTAMPTZ,
    charge_trigger TEXT  -- 'expressed_interest' | 'patient_selected' | 'appointment_booked' | 'treatment_completed'
);
```

Lead-charge trigger is configurable — see §6.

### 2.10 `platform_cross_sell_offers`


Available cross-sell products (credit monitoring, insurance, etc.). Scaffolded for v1, most disabled at launch.

```sql
CREATE TABLE platform_cross_sell_offers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT NOT NULL UNIQUE,  -- 'equifax_credit_monitoring', 'credit_insurance_v1', 'prepaid_card_v1'
    name TEXT NOT NULL,
    category TEXT NOT NULL,  -- 'credit_monitoring' | 'insurance' | 'prepaid_card' | 'budgeting_tool'
    vendor TEXT,
    status TEXT NOT NULL DEFAULT 'disabled',  -- 'disabled' | 'available' | 'sunset'
    eligibility_rules TEXT NOT NULL,  -- yaml file ref: 'cross_sell/credit_monitoring_v1.yaml'
    pricing_config JSONB,
    revenue_share_bps INTEGER  -- our cut
);

CREATE TABLE platform_cross_sell_engagements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id UUID NOT NULL REFERENCES platform_patients(id),
    offer_id UUID NOT NULL REFERENCES platform_cross_sell_offers(id),
    state TEXT NOT NULL,  -- 'presented' | 'clicked' | 'enrolled' | 'declined'
    presented_at TIMESTAMPTZ,
    enrolled_at TIMESTAMPTZ,
    revenue_to_date_cents BIGINT NOT NULL DEFAULT 0
);
```

---

### 2.11 Co-applicant linking (no new table)

Co-applicants do **not** get a separate table. Both applicants are full `platform_patients` rows with their own `platform_credit_applications` row. They are linked by `platform_credit_applications.co_applicant_of_application_id` and grouped by an implicit **application group** (the primary id).

**Invite flow:**
1. Primary applicant submits enough info to create their `platform_credit_applications` row (status `started`).
2. Primary clicks "Invite co-applicant" → enters co-applicant email **or** generates a share code (random 8-char token, single use, 24h expiry).
3. Co-applicant follows the link/code → if no PaySpyre patient profile exists, they create one (same quick-start as primary: name + email + optionally phone), then their own `platform_credit_applications` row is created with `co_applicant_of_application_id = <primary.id>` and `applicant_role = 'co_applicant'`.
4. Co-applicant runs through the **same flow** the credit product mandates for primaries — there is no separate "co-applicant flow." Same verifications, same consents, same risk rules.
5. Decisioning waits for both applications to reach a terminal verification state before evaluating the group (§4.5).

**Invite storage:** new table `platform_coapplicant_invites` (PR P3.5):

```sql
CREATE TABLE platform_coapplicant_invites (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    primary_application_id UUID NOT NULL REFERENCES platform_credit_applications(id),
    invite_email TEXT,
    share_code TEXT UNIQUE,  -- 8-char human-friendly token if email invite not used
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    consumed_by_patient_id UUID REFERENCES platform_patients(id),
    revoked_at TIMESTAMPTZ
);
CREATE INDEX platform_coapplicant_invites_app ON platform_coapplicant_invites (primary_application_id);
```

---

## 3. The verification matrix — Dave's toggle config

The `verification_matrix` JSONB on `platform_credit_products` is what makes the system configurable per credit product. **This is the core abstraction.**

### 3.1 JSON Schema (validated on write)

```json
{
  "$schema": "https://json-schema.org/draft-07/schema",
  "type": "object",
  "required": ["amount_brackets"],
  "properties": {
    "amount_brackets": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["min_cents", "max_cents", "verifications"],
        "properties": {
          "min_cents": {"type": "integer", "minimum": 0},
          "max_cents": {"type": "integer"},
          "verifications": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["type", "required"],
              "properties": {
                "type": {
                  "enum": ["kyc_id", "bank_link", "bureau_soft", "bureau_hard",
                           "income_attestation", "address_proof"]
                },
                "required": {"type": "boolean"},
                "order": {"type": "integer"},
                "vendor_preference": {"type": "string"},
                "fallback_allowed": {"type": "boolean"},
                "decline_on_fail": {"type": "boolean"},
                "decision_gate": {"type": "boolean"}
              }
            }
          },
          "decision_ruleset_override": {"type": "string"}
        }
      }
    }
  }
}
```

### 3.2 Example: small dental hygiene financing ($0–$2,500)

```json
{
  "amount_brackets": [
    {
      "min_cents": 0,
      "max_cents": 250000,
      "verifications": [
        {"type": "kyc_id", "required": true, "order": 1, "decline_on_fail": true},
        {"type": "bank_link", "required": true, "order": 2, "decline_on_fail": false, "fallback_allowed": true}
      ]
    }
  ]
}
```

No bureau pull at this tier. ID + bank only.

### 3.3 Example: full-arch implant financing ($15K–$60K)

```json
{
  "amount_brackets": [
    {
      "min_cents": 1500000,
      "max_cents": 3000000,
      "verifications": [
        {"type": "kyc_id", "required": true, "order": 1, "decline_on_fail": true},
        {"type": "bureau_soft", "required": true, "order": 2, "decision_gate": true},
        {"type": "bank_link", "required": true, "order": 3, "decline_on_fail": false, "fallback_allowed": true},
        {"type": "bureau_hard", "required": true, "order": 4}
      ]
    },
    {
      "min_cents": 3000000,
      "max_cents": 6000000,
      "verifications": [
        {"type": "kyc_id", "required": true, "order": 1, "decline_on_fail": true},
        {"type": "bureau_soft", "required": true, "order": 2, "decision_gate": true},
        {"type": "bank_link", "required": true, "order": 3, "decline_on_fail": true},
        {"type": "bureau_hard", "required": true, "order": 4},
        {"type": "income_attestation", "required": true, "order": 5}
      ]
    }
  ]
}
```

### 3.4 Example: marketplace-lead-only flow (no financing)

```json
{
  "amount_brackets": [
    {
      "min_cents": 0,
      "max_cents": 0,
      "verifications": [
        {"type": "kyc_id", "required": false, "order": 1},
        {"type": "bank_link", "required": false, "order": 2}
      ]
    }
  ]
}
```

Patient can list on the marketplace with no verifications — lead just gets tagged `unqualified` and priced accordingly.

---

## 4. The flow engine

The flow engine is the orchestrator that reads a credit product's verification matrix and runs the steps. **It must be pure — given the same product config + patient state, it produces the same next step.**

### 4.1 Module location

```
app/services/flow_engine/
├── __init__.py
├── engine.py           # main orchestrator
├── step_runner.py      # dispatches a single verification step
├── decision_gate.py    # evaluates whether to proceed/decline after each step
├── state.py            # FlowState class — immutable snapshot of where we are
└── tests/
```

### 4.2 Core API

```python
from app.services.flow_engine import FlowEngine, FlowState, NextStep

engine = FlowEngine(db_session, config_loader, vendor_registry)

# Determine the next step for an application
next_step: NextStep = engine.next_step(application_id=app.id)
# Returns: {action: 'run_verification', type: 'kyc_id', vendor: 'didit', ...}
#       or {action: 'await_decision_gate', ...}
#       or {action: 'submit_for_decision', ...}
#       or {action: 'complete', outcome: 'approved' | 'declined'}

# Record a step completion (called by webhook handlers, manual flows, etc.)
engine.record_step_completion(
    application_id=app.id,
    verification_id=verif.id,
    result='passed',
    payload={...}
)
# Engine internally:
# 1. Appends to platform_events
# 2. Updates application.flow_state
# 3. Updates patient verification_depth if applicable
# 4. Evaluates decision gates (e.g., soft pull says decline → application.status = 'declined')
# 5. Updates patient.lead_state if applicable
```

### 4.3 Hard rules

- **No direct status writes outside the engine.** Same rule as KYC state machine. Add a CI grep test that fails if `platform_credit_applications.status =` appears outside `flow_engine/`.
- **Engine reads, never mutates, the credit product config.** Config changes happen through the credit-product admin UI which goes through a separate migration-style versioning path.
- **Snapshot the credit product version on application creation.** If the product changes mid-application, the applicant continues under the version they started.

### 4.4 Decision gates

A verification with `"decision_gate": true` blocks further steps until evaluated. The decision-rule YAML for that bracket determines the outcome:

```yaml
# config/decision_rules/dental_full_arch_v1.yaml
gates:
  - after: bureau_soft
    rules:
      - if: credit_score < 580
        then: decline
        reason_code: SCORE_BELOW_FLOOR
      - if: active_bankruptcy == true
        then: decline
        reason_code: ACTIVE_BANKRUPTCY
      - if: credit_score >= 580 and credit_score < 650
        then: continue
        flag: manual_review_after_hard_pull
      - else: continue
```

Same YAML conventions as the existing risk engine. Reuse the rule parser.

---

### 4.5 Co-applicant scoring (per Dave)

When `co_applicant_of_application_id` is set, the flow engine treats the **application group** (primary + co-app) as the decisioning unit.

**Rules:**
1. **Same flow runs for both.** No separate "co-applicant" flow config.
2. **Wait for both.** The decision gate (§4.4) does not fire until both applications have all required verifications terminal (pass, fail, or skipped).
3. **Default combined score = average** of the two applicants' computed scores. Both pulled into the same `decision` record on the **primary** application's row. The co-applicant row mirrors the decision with `decision.role = 'co_applicant'`.
4. **"Strongest applicant" override available.** The decision ruleset YAML can opt into evaluating each applicant standalone and choosing the better outcome — e.g. `combination_strategy: average | strongest_standalone | both_required_pass`. Default is `average`. Per Dave: this mirrors old finance-industry systems that let ops count or not count one applicant.
5. **Hard declines pass through.** If either applicant hits a hard-decline rule (bankruptcy, fraud match, sanctions hit), the application group is declined regardless of strategy.
6. **Consents are per-applicant.** Each applicant signs their own consents on their own row in `platform_consents`. No "one signs for both."
7. **Revoking a co-applicant.** Primary can revoke the invite before the co-applicant submits (deletes the invite, no application row created). After the co-app row is created, removing the co-applicant requires a new application (we don't silently drop a co-app's data from a half-completed application).

The decision ruleset YAML adds:

```yaml
# config/decision_rules/dental_full_arch_v1.yaml
combination_strategy: average  # | strongest_standalone | both_required_pass
hard_declines:
  - any_applicant_bankruptcy_within_24mo
  - any_applicant_sanctions_hit
scoring:
  # ...
```

---

## 5. Verification adapters

Each verification type has a pluggable adapter. ID verification already has Didit + Persona; bank and bureau need new adapters.

### 5.1 Adapter interface

```python
# app/services/verifications/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class VerificationRequest:
    patient_id: UUID
    application_id: UUID | None
    consent_id: UUID
    vendor: str | None  # None = use default

@dataclass
class VerificationResult:
    status: Literal['passed', 'failed', 'pending', 'manual_review']
    vendor_session_ref: str
    raw_payload: dict  # encrypted at rest
    extracted_fields: dict[str, FieldValue]  -- for writing to platform_patient_fields
    cost_cents: int

class VerificationAdapter(ABC):
    verification_type: str

    @abstractmethod
    async def initiate(self, request: VerificationRequest) -> VerificationResult: ...

    @abstractmethod
    async def handle_webhook(self, payload: dict) -> VerificationResult: ...
```

### 5.2 Adapters to implement

| Adapter | Type | Vendors | Status |
|---------|------|---------|--------|
| `KycIdAdapter` | `kyc_id` | Didit (default), Persona (fallback) | ✅ exists (PRs 3, 4, 13) |
| `BankLinkAdapter` | `bank_link` | Flinks (default), Inverite (fallback) | 🆕 build |
| `BureauSoftAdapter` | `bureau_soft` | Equifax CA (default) | 🆕 build — **gated on Mike securing access** |
| `BureauHardAdapter` | `bureau_hard` | Equifax CA (default), TransUnion CA (alt) | 🆕 build |
| `IncomeAttestationAdapter` | `income_attestation` | manual upload + ops review | 🆕 build (simple) |
| `AddressProofAdapter` | `address_proof` | manual upload + ops review | 🆕 build (simple) |

### 5.3 Bank link adapter — Flinks

```
app/services/verifications/bank_link/
├── __init__.py
├── adapter.py          # BankLinkAdapter implementation
├── flinks_client.py    # Flinks API client
├── inverite_client.py  # Inverite fallback
├── income_detector.py  # extracts income, employer, time-at-employer from transactions
├── expense_categorizer.py
└── tests/
```

Extracted fields written to `platform_patient_fields`:
- `bank_account_holder_name` (source: `bank_aggregator`)
- `bank_routing_number` (encrypted at rest)
- `bank_account_number_last4`
- `bank_balance_current_cents` (source: `bank_aggregator`)
- `bank_balance_avg_30d_cents`
- `bank_nsf_count_90d`
- `monthly_income_after_tax_cents` (source: `bank_aggregator`, confidence based on payroll detection quality)
- `employer_name_bank_derived` (source: `bank_aggregator`)
- `time_at_employer_months_bank_derived` (source: `bank_aggregator`, computed from first matching payroll deposit)
- `monthly_housing_payment_cents` (source: `bank_aggregator`)

**Critical:** these fields write to `platform_patient_fields` with `source = 'bank_aggregator'`, never overwrite a `source = 'self_reported'` value silently. The review screen shows both and lets the patient confirm.

### 5.4 Bureau adapters — Equifax CA

```
app/services/verifications/bureau/
├── __init__.py
├── adapter.py
├── equifax_ca_client.py
├── transunion_ca_client.py
├── soft_pull.py        # uses prequalification product if access granted
├── hard_pull.py        # standard consumer credit report
├── prefill_extractor.py  # pulls candidate prefill fields (with appropriate caveats — see §5.5)
└── tests/
```

### 5.5 Bureau prefill — what we actually trust

Per the Equifax Consumer Credit Report User Guide (Nov 2021), **the bureau's "Since" date on address and employer is when the data was first reported to the file, not the actual start date.** Treat all derived fields accordingly:

| Field | Bureau source | Confidence | Treatment |
|-------|---------------|------------|-----------|
| Current address | `[15]` | high | Pre-fill, ask confirm |
| Previous addresses | `[18]`, `[19]` | high | Display, ask confirm |
| Time at current address | derived from `[16]` | **low** | Show as "Equifax first saw this address X years ago — when did you actually move in?" |
| Current employer | `[26]` | medium (often stale) | Pre-fill, ask confirm |
| Time at current employer | derived from `[27]` if present | **low** — often missing | Don't auto-fill; ask self-report. If bank-derived value exists from payroll history, use that instead. |
| Credit score | `[7]` | high | Display in decisioning, not to applicant |
| Trade lines | `[36]` | high | Decisioning input |
| Bankruptcy / collections | `[32]`, `[33]` | high | Hard decline rules where applicable |

Every prefill field written to `platform_patient_fields` carries `source = 'bureau_soft'` or `'bureau_hard'` and an appropriate `confidence` value. The review UI displays low-confidence fields differently (asks the applicant to actively confirm vs. passively review).

### 5.6 Vendor abstraction — same rules as KYC

- Adapters register with a vendor registry, looked up by `vendor_preference` in the verification matrix
- Adapter swap = config change, no orchestration code changes
- Each adapter has unit tests with recorded fixtures (no live API in CI)
- Sandbox integration tests for the active default vendor

### 5.7 SIN collection policy (per Dave — May 21, 2026)

SIN is **legally optional** in Canada — we cannot require it. But Equifax uses SIN as its strongest unique identifier; without it, common-name applicants (e.g. "John Smith") risk being matched to the wrong file. Dave has personally seen this happen many times.

**Implementation:**

1. **Always ask, never force.** The SIN field is rendered on every application that runs a bureau pull, with neutral copy explaining why it helps accuracy. The field is not required to submit the form.
2. **Patient may decline.** Declining sets `platform_patients.sin_declined = true` and writes a `platform_event` of type `sin_declined`. No retry pestering inside the same application.
3. **Decline carries a risk consequence.** Per Dave: "it is also our right to refuse a credit applicant that does not have the correct information." The decision ruleset YAML may add `sin_declined` as a soft flag or hard decline at our discretion per credit product. Default ruleset behavior:
   - Small-amount products (< $5,000): `sin_declined` → no impact
   - Mid-amount ($5,000–$25,000): `sin_declined` + common-name match collision detected → manual review queue
   - Large-amount ($25,000+): `sin_declined` → manual review queue always (not auto-decline; an ops reviewer decides)
4. **Common-name collision detection.** When a bureau pull returns a file, the bureau adapter compares the returned name + DOB + address against our `platform_patient_fields` values. If name+DOB match but address has zero overlap with self-reported or ID-doc-extracted address, raise `bureau.possible_misidentification` event. UI surfaces this as "We may have pulled the wrong file — please add your SIN to continue."
5. **Storage rules (hard).**
   - `sin_encrypted` stored with envelope encryption (KMS or pgcrypto with rotating key — confirm key management strategy with Mike).
   - `sin_last3` ok for ops display/search, full SIN never returned by APIs.
   - SIN never appears in logs, error messages, webhook payloads, or audit exports (FINTRAC export omits SIN; audits show `sin_last3` only).
   - Decrypt only inside bureau adapter calls, in-memory, never persisted in plaintext.
   - Service account that holds the decryption key is separate from the API server's key — read access logged to `platform_events`.

**API surface:**
- `POST /v1/patients/{id}/sin` — body `{ sin: "XXX-XXX-XXX" }` — encrypts, sets `sin_last3` + `sin_collected_at`, writes event. Response: 204 (never echoes value back).
- `POST /v1/patients/{id}/sin/decline` — sets `sin_declined = true`, writes event. Response: 204.
- `GET /v1/patients/{id}` returns `{ sin_on_file: bool, sin_declined: bool, sin_last3: "123" | null }`. Never the value.

**Per-credit-product UI copy** lives in `config/consent_text/sin_explainer/`. Default copy explains: "Your Social Insurance Number is the most accurate way for us to match your credit file. Providing it is your choice, but credit bureaus may return the wrong person's information for common names without it. We protect your SIN with encryption and never share it."

---

### 6.1 Lead state transitions

```
unqualified → pre_qualified : after kyc_id passes
pre_qualified → pre_approved : after bureau_soft passes a decision gate
pre_approved → approved : after full application approval
any → declined : explicit decline event
```

Patients without any application can still be `unqualified` and listed on the marketplace.

### 6.2 Lead pricing

Configurable in `config/marketplace/lead_pricing.yaml`:

```yaml
base_prices:
  unqualified: 1500     # cents — $15
  pre_qualified: 4000   # $40
  pre_approved: 7500    # $75
  approved: 15000       # $150

multipliers:
  treatment_category:
    implants: 1.5
    full_arch: 2.5
    orthodontics: 1.3
    general_dentistry: 1.0
  urgency:
    immediate: 1.4
    this_week: 1.2
    this_month: 1.0
    flexible: 0.8
  verification_depth:
    none: 0.5
    id_verified: 1.0
    id_bank_verified: 1.3
    id_bank_cb_verified: 1.5

charge_trigger: 'patient_selected'  # 'expressed_interest' | 'patient_selected' | 'appointment_booked' | 'treatment_completed'
```

### 6.3 Clinic marketplace API

Endpoints needed:

```
GET    /api/clinic/v1/marketplace/leads
       ?treatment_category=implants&max_distance_km=25&min_verification=id_verified
POST   /api/clinic/v1/marketplace/leads/{listing_id}/express_interest
GET    /api/clinic/v1/marketplace/listings/{listing_id}  (after interest expressed)
POST   /api/clinic/v1/marketplace/listings/{listing_id}/book_appointment
GET    /api/clinic/v1/marketplace/billing/leads  (history + charges)
```

Patient-side:

```
POST   /api/patient/v1/marketplace/listings
GET    /api/patient/v1/marketplace/listings/{listing_id}/interested_clinics
POST   /api/patient/v1/marketplace/listings/{listing_id}/select_clinic
POST   /api/patient/v1/marketplace/listings/{listing_id}/pause
```

### 6.4 Lead listing visibility rules

Per `config/marketplace/visibility.yaml`:

```yaml
default_visibility:
  unqualified:
    visible_to_clinics: true
    show_estimated_budget: false
    show_verification_depth: true
  pre_qualified:
    visible_to_clinics: true
    show_estimated_budget: true  -- range only
    show_verification_depth: true
  pre_approved:
    visible_to_clinics: true
    show_estimated_budget: true  -- exact
    show_verification_depth: true
    priority_placement: true
```

PII never goes over the wire to clinics until the patient selects them. Pre-select, clinics see: treatment category, urgency, postal-code first-3 (FSA), verification depth, lead state, estimated budget range.

---

## 7. Cross-sell engine (scaffold only at launch)

### 7.1 Eligibility evaluation

A simple rules engine reads `platform_cross_sell_offers.eligibility_rules` (YAML) and decides which offers to present to a patient.

```yaml
# config/cross_sell/credit_monitoring_v1.yaml
present_if:
  - patient.verification_depth in [id_bank_verified, id_bank_cb_verified]
  - patient.lead_state in [pre_qualified, pre_approved, declined]  # esp. declined — biggest value
  - patient.consents.has('cross_sell_eligibility') == true
  - patient.declined_offers does not contain 'equifax_credit_monitoring' (last 90 days)
```

### 7.2 Presentation surfaces

- Post-decline screen — "You weren't approved this time. Here's how to improve your credit."
- Post-approval thank-you screen — "Want alerts when your score changes?"
- Patient dashboard side panel

All disabled by default (`status = 'disabled'`). Build the scaffold; flip on when ready.

---

## 8. Consent & compliance

### 8.1 Granular consent UI rule

**Never** bundle. Every distinct purpose gets its own checkbox with its own exact-text record. Specifically:

- `id_verification` — at step 1
- `soft_bureau_pull` — at step 2 (only when running)
- `bank_verification` — at step 3 (only when running)
- `hard_bureau_pull` — at the review screen, immediately before submit
- `automated_decision_making` — at the review screen
- `marketing_email` — optional, separate, never default-checked
- `marketplace_listing` — only when patient opts in
- `cross_sell_eligibility` — separate, opt-in
- `aggregate_data_use` — separate, opt-in, disabled at launch (build the consent record but don't surface yet)

### 8.2 Consent versioning

```
config/consent_text/
├── id_verification/
│   ├── v1_2026-05.md
│   └── v2_2026-08.md
├── soft_bureau_pull/
│   └── v1_2026-05.md
└── ...
```

The active version per purpose is selected by `config/consent_text/active.yaml`. When the active version changes, new consent records reference the new version. Old consent records are immutable.

### 8.3 Adverse action notice

On decline:
- Generate notice with bureau name + reason codes (if bureau was a factor)
- Include right to obtain free copy of credit report from bureau (Canadian consumer reporting acts)
- Email + store PDF in `platform_events.payload`
- Worker job: `app/jobs/adverse_action_notifier.py`

### 8.4 Practice-provided prefill vs self-report (per Dave — May 21, 2026)

Dave's preference: keep the patient quick-start minimal (name + email, optionally phone) so we have **patient-entered values** to compare against later-collected sources. If clinic-provided info (treatment plan, demographic prefill from the clinic's own records) differs from what the patient self-reports, that's a **red flag worth surfacing**, not a silent overwrite.

**Implementation rule (hard):**

1. **Patient quick-start fields are always self-reported first.** Even if a clinic has the patient in its system and is generating the application link, the patient fills in name + email (+ optional phone) before anything else. Those values are written to `platform_patient_fields` with `source = 'self_reported'`.
2. **Clinic-prefilled values are stored separately with `source = 'practice_prefill'`.** They never overwrite a `self_reported` value silently. They appear alongside it in ops/clinic views.
3. **Discrepancy detection runs at end of quick-start.** When the patient submits the quick-start step, the flow engine compares all available `practice_prefill` values to the matching `self_reported` values. Mismatches above a per-field tolerance (e.g. fuzzy match < 0.85 on name, exact on email) raise a `discrepancy_detected` event on `platform_events`.
4. **Discrepancy resolution path.**
   - Soft mismatch (typo, minor variation): surfaced in ops review, not blocking.
   - Hard mismatch (different person entirely — name + email both don't match): block flow progression with a "Let's make sure we have the right info" screen that asks the patient to confirm or correct. If confirmed by patient, both values stay on the record; ops are notified.
5. **Once ID verification completes, `source = 'id_doc'` becomes the canonical identity value.** Self-reported and practice-prefill remain in `platform_patient_fields` (for audit/discrepancy history) but `is_current = false` if the ID-doc value supersedes.

**Why this matters:** patient-entered values are our cheapest fraud signal. If a clinic submits an application via API with prefilled identity and we never ask the patient to confirm it, we lose that signal. Always make the patient fill in at least name + email.

**Endpoint-level enforcement:** `POST /v1/applications` requires `patient_self_attested: true` in the payload when the patient is interacting (vs. clinic-only origination). Clinic-only origination (where the clinic creates an application with no patient interaction yet) is allowed but flagged and cannot progress past `started` until the patient quick-start runs.

---

### 8.5 Quebec deferral

Quebec (Law 25 automated decision-making disclosure + French content) is deferred from v1. Add a province check in the entry point — if `province == 'QC'`, return a "coming soon" page. Don't list QC credit products in the catalog.

---

## 9. APIs (FastAPI routers)

### 9.1 New routers

```
app/api/v1/endpoints/
├── platform_patients.py
├── platform_credit_products.py     # admin only — internal product config
├── platform_credit_applications.py
├── platform_marketplace.py         # patient-side
├── platform_marketplace_clinic.py  # clinic-side
├── platform_cross_sell.py
└── platform_admin.py               # back-office operations
```

### 9.2 Critical applicant-facing endpoints

```
POST   /api/patient/v1/applications              # start application
GET    /api/patient/v1/applications/{id}/next    # what's the next step? (driven by flow engine)
POST   /api/patient/v1/applications/{id}/consents
POST   /api/patient/v1/applications/{id}/verifications/{type}/initiate
POST   /api/patient/v1/applications/{id}/self_report  # the few manual fields
POST   /api/patient/v1/applications/{id}/submit  # triggers hard pull + decision
GET    /api/patient/v1/applications/{id}/decision
```

### 9.3 Webhooks

```
POST   /api/webhooks/flinks
POST   /api/webhooks/inverite
POST   /api/webhooks/equifax_ca
POST   /api/webhooks/transunion_ca
```

Each follows the same pattern as the existing Didit webhook handler:
1. HMAC verify body (reject if invalid, log failure separately)
2. Idempotency check (UNIQUE on (vendor, vendor_event_id) in a new `platform_webhook_inbox` table — or extend `kyc_webhook_inbox` to include these)
3. Parse → transactional update + event log + outbox enqueue
4. Return 200

---

## 10. Implementation order — PRs

**Do not bundle. Do not reorder.** Each PR ships with migration + impl + tests + spec updates.

### Phase A — Platform foundation (no new vendors)

- **PR P1** — Migrations for §2.1–2.7 (`platform_patients`, `platform_patient_fields`, `platform_credit_products`, `platform_credit_applications`, `platform_verifications`, `platform_consents`, `platform_events`). Includes `REVOKE UPDATE, DELETE ON platform_events`. Tests confirm revoke.
- **PR P2** — Patient profile service (CRUD with source-tagged field writes). No UI yet.
- **PR P3** — Credit product service (admin CRUD + verification_matrix JSON Schema validation). Seed two products: `dental_small_v1`, `dental_full_arch_v1`.
- **PR P3.5** — Co-applicant linkage: migration for `co_applicant_of_application_id`, `applicant_role`, `platform_coapplicant_invites` table (§2.11) + invite/accept service. No flow-engine work yet; just the linkage primitives + email/share-code invite endpoint.
- **PR P4** — Flow engine (§4) including co-applicant group decisioning (§4.5). Pure functional core, well-tested with synthetic flows. No vendor integrations yet — uses a `MockVerificationAdapter` that just records pass/fail. Combination strategies (`average | strongest_standalone | both_required_pass`) tested with synthetic two-applicant groups.
- **PR P5** — Consent service (§8.1, §8.2) + consent text loader from filesystem.
- **PR P6** — Applicant API endpoints (§9.2) wired to flow engine + mock adapter. Full applicant journey works end-to-end with mock verifications. Quick-start endpoint enforces patient-self-attested name + email per §8.4. Discrepancy detection runs at quick-start submit.
- **PR P6.5** — SIN collection & policy (§5.7): SIN endpoints, encryption + key rotation hooks, `sin_last3` display, common-name misidentification detection, decision-ruleset `sin_declined` handling. Default rulesets updated. Audit export confirms SIN never appears.
- **PR P7** — Adverse-action notice job + email template (covers SIN-decline-driven manual reviews that result in decline).

### Phase B — Real verifications

- **PR P8** — Flinks bank link adapter (§5.3) + webhook + income detector + tests with recorded Flinks fixtures.
- **PR P9** — Inverite bank link adapter as fallback. Vendor-swap test: same flow on Inverite via config change.
- **PR P10** — Equifax hard-pull adapter (§5.4) + webhook + prefill extractor (§5.5).
- **PR P11** — TransUnion hard-pull adapter as alternative. Vendor-swap test.
- **PR P12** — Equifax soft-pull adapter. **Gated on Mike confirming access** — implement against documented API + test with recorded fixtures, but feature-flag off until access granted.

### Phase C — Marketplace

- **PR P13** — Marketplace tables (§2.8, §2.9) + listing service + lead pricing engine (§6.2).
- **PR P14** — Patient-side marketplace endpoints + listing creation UI flow.
- **PR P15** — Clinic-side marketplace endpoints + clinic dashboard UI.
- **PR P16** — Lead billing (charge trigger logic, integration with existing billing system if any — confirm with Mike).

### Phase D — Cross-sell & monetization scaffolds

- **PR P17** — Cross-sell tables (§2.10) + eligibility engine. Scaffold only, all offers disabled.
- **PR P18** — Aggregate data export scaffold (PIA-required before enabling — do not enable in v1, just build the consent + the data-classification tables).

### Phase E — Operations

- **PR P19** — Back-office admin console for credit product config (visual editor for the verification matrix).
- **PR P20** — Manual review queue (extends the existing KYC manual review with platform application context).
- **PR P21** — Reporting endpoints (funnel KPIs from §11).

**Stop after each PR for Mike's review. Do not start the next until the previous is merged.**

---

## 11. KPIs to instrument from PR P6

Per `app/services/metrics/platform_metrics.py`:

| Metric | Tag dimensions |
|--------|----------------|
| `application.started` | credit_product_code, clinic_id, vertical |
| `application.step_completed` | credit_product_code, step_type, result |
| `application.completed` | credit_product_code, decision |
| `verification.completed` | type, vendor, status, cost_cents |
| `consent.granted` | purpose |
| `consent.revoked` | purpose |
| `lead_state_changed` | from_state, to_state |
| `marketplace.listing_created` | treatment_category, lead_state |
| `marketplace.clinic_interest` | clinic_id, treatment_category |
| `marketplace.lead_charged` | clinic_id, charge_trigger, amount_cents |

Expose via Prometheus endpoint; ship to existing observability stack.

---

## 12. Hard rules — DO NOT VIOLATE

Restating from the KYC kickoff, extended for platform:

1. **Append-only tables stay append-only.** `platform_events`, `platform_patient_fields`, `platform_consents` all enforce no UPDATE/DELETE at the DB level. Test the revoke after every migration that touches them.
2. **Consent text shown is immutable.** Versioning, not editing.
3. **Webhooks HMAC-verify first.** No exceptions.
4. **Idempotency at DB level**, not application level.
5. **State transitions through the flow engine only.** CI grep test for forbidden direct writes.
6. **Vendor abstraction is real.** Adapter swap = config change. Period.
7. **No PII in logs.** Verification IDs and patient UUIDs are fine; names, DOBs, document numbers, full addresses, bank credentials, full bureau payloads are not.
8. **All thresholds + rules in YAML.** No hardcoded magic numbers in code.
9. **Snapshot product version on application creation.** Mid-flow product updates don't affect in-flight applications.
10. **Bureau prefill fields are tagged with appropriate confidence.** Never claim "Equifax says you've lived here X years." Always: "Equifax first saw this address X years ago."
11. **No silent overwrites of self-reported by aggregator-derived values.** Review UI shows both and patient confirms.
12. **Quebec returns "coming soon."** Province check in entry point until Quebec Law 25 work is done.

---

## 13. Open questions — DO NOT GUESS

If you hit any of these, **stop and ask Mike.**

1. **Existing `applications` table.** Does one already exist in `payspyre-backend`? If so, do we migrate to `platform_credit_applications` or extend the existing one?
2. **Clinic/practice table.** Where does `clinic_id` resolve to? Is there an existing `practices` or `clinics` table from KDC/Alvero shared via the same DB?
3. **Auth/identity.** Does PaySpyre have its own user auth, or share with Alvero's Supabase Auth? Where do clinic users authenticate from?
4. **Billing rails.** Is there an existing billing/invoicing system for clinics that the marketplace lead charges should plug into? Or do we build a new one?
5. **Equifax soft-pull contractual access.** Confirmed with Mike that this is being pursued separately. PR P12 implements but feature-flags off pending confirmation.
6. **Funding source — `payspyre_capital` vs `partner_lender` vs `hybrid`.** What's actually live at launch? Affects which credit products can be in `status='active'` on day one.
7. ~~`requested_amount` source of truth.~~ **Resolved (Dave, May 21):** clinic and/or patient can both contribute; both stored; discrepancy > 10% raises a soft flag. See §2.4.1.
8. **Quebec re-enable timeline.** When do we expect French + Law 25 disclosures ready? Affects whether we ship Quebec deferral as a hard block or a config flag.
9. **SIN encryption key management.** Which approach for `sin_encrypted` — Supabase Vault, AWS KMS, pgcrypto with rotating column-key, or external HSM? Affects PR P6.5. Default plan: pgcrypto with key in Cloudflare Workers secret + quarterly rotation, but confirm with Mike before implementing.
10. **Co-applicant default `combination_strategy`.** Spec defaults to `average` per Dave. Confirm this is the right default for `dental_full_arch_v1` and whether any product should default to `strongest_standalone` (e.g. if one applicant has thin file).

---

## 14. File-tree summary (target state after Phase A)

```
payspyre-backend/
├── alembic/versions/
│   ├── 0XX_create_platform_patients.py
│   ├── 0XX_create_platform_patient_fields.py
│   ├── 0XX_create_platform_credit_products.py
│   ├── 0XX_create_platform_credit_applications.py
│   ├── 0XX_create_platform_verifications.py
│   ├── 0XX_create_platform_verifications.py
│   ├── 0XX_create_platform_consents.py
│   ├── 0XX_create_platform_events.py
│   └── 0XX_revoke_platform_events_mutations.py
├── app/
│   ├── models/
│   │   ├── platform_patient.py
│   │   ├── platform_credit_product.py
│   │   ├── platform_credit_application.py
│   │   ├── platform_verification.py
│   │   ├── platform_consent.py
│   │   └── platform_event.py
│   ├── schemas/
│   │   └── platform/...
│   ├── services/
│   │   ├── flow_engine/
│   │   ├── verifications/
│   │   │   ├── kyc_id/    # existing
│   │   │   ├── bank_link/  # PR P8
│   │   │   └── bureau/     # PR P10–P12
│   │   ├── patient_profile/
│   │   ├── credit_product/
│   │   ├── consent/
│   │   ├── marketplace/
│   │   ├── cross_sell/
│   │   └── metrics/
│   └── api/v1/endpoints/
│       └── (per §9)
├── config/
│   ├── credit_products/
│   │   ├── dental_small_v1.yaml
│   │   └── dental_full_arch_v1.yaml
│   ├── decision_rules/
│   │   ├── dental_small_v1.yaml
│   │   └── dental_full_arch_v1.yaml
│   ├── consent_text/
│   │   ├── active.yaml
│   │   └── (per-purpose, per-version dirs)
│   ├── marketplace/
│   │   ├── lead_pricing.yaml
│   │   └── visibility.yaml
│   └── cross_sell/
└── tests/
    ├── platform/
    └── (per-module test dirs)
```

---

## 15. Definition of done (for the full v2 build)

- [ ] All 21 PRs merged to `main`
- [ ] End-to-end test: practice generates link → patient completes flow for `dental_full_arch_v1` (ID + soft pull + bank + hard pull + review + decision)
- [ ] End-to-end test: patient completes `dental_small_v1` (ID + bank only)
- [ ] End-to-end test: patient lists on marketplace without applying for financing
- [ ] Vendor swap tests: Didit↔Persona, Flinks↔Inverite, Equifax↔TransUnion all swap by config only
- [ ] Soft-pull adapter feature-flag-tested (off path + on path)
- [ ] FINTRAC audit export covers `platform_events` in addition to `kyc_events`
- [ ] All 8 open questions resolved and recorded in the spec
- [ ] Phase 1 launch checklist signed off by Mike + Dave

---

## 16. Reporting back

After each PR is ready for review, post a summary in the PR description with:
- What was built
- What tests cover it
- Any spec deviations and why
- Any new open questions discovered

When you finish PR P21, write a `PLATFORM_V2_BUILD_COMPLETE.md` at repo root with the full closeout, including:
- The full credit product config catalog
- The full consent-text version manifest
- The full vendor cost table (per-verification, per-vendor)
- The funnel KPI baseline from the first 30 days of data

---

**Begin with reading §0, §1, §12, §13. Confirm understanding before writing migrations.**
