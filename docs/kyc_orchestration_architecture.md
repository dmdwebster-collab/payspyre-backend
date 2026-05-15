# KYC Orchestration Layer — Architecture & Specification

**Purpose:** Wire Didit (or Persona as Plan B) webhooks → Supabase → n8n → underwriting state machine.

**Owner:** PaySpyre Financial
**Date:** 2026-05-14

---

## 1. Architecture Diagram

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Patient Portal │────▶│  PaySpyre API   │────▶│  Didit / Persona │
│  (loan app)     │     │  (FastAPI)      │     │  (KYC vendor)   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                               │                       │
                               │ create_kyc_session() │
                               │◀──────────────────────┘
                               │ return: verification_url
                               │
                               ▼
                        ┌─────────────────┐
                        │     Supabase    │
                        │   (PostgreSQL)  │
                        │                 │
                        │ kyc_sessions    │
                        │ kyc_results     │
                        │ kyc_events      │
                        └─────────────────┘
                               ▲
                               │
                 ┌─────────────┼─────────────┐
                 │             │             │
        ┌────────┴─────┐ ┌─────┴──────┐ ┌───┴────────┐
        │   n8n Webhook│ │   Risk     │ │ Manual     │
        │   Receiver   │ │   Rules    │ │ Review     │
        │   (Supabase  │ │  Engine    │ │ Queue      │
        │   real-time) │ │            │ │            │
        └──────────────┘ └────────────┘ └────────────┘
                 │
                 │ emit: kyc_result.completed
                 │
                 ▼
        ┌─────────────────────┐
        │  Underwriting State │
        │      Machine        │
        │                     │
        │  STATES:            │
        │  - pending_kyc      │
        │  - kyc_in_progress  │
        │  - kyc_completed    │
        │  - risk_eval        │
        │  - manual_review    │
        │  - approved         │
        │  - rejected         │
        └─────────────────────┘
                 │
                 │ emit: underwriting.decision
                 │
                 ▼
        ┌─────────────────────┐
        │  DocuSign / Rotessa │
        │  (next workflow)    │
        └─────────────────────┘
```

---

## 2. Database Schema (Supabase)

### `kyc_sessions`
| Column | Type | Description |
|---|---|---|
| `id` | uuid | Primary key |
| `loan_application_id` | uuid (FK) | Links to loan_applications |
| `borrower_id` | uuid (FK) | Links to borrowers |
| `vendor` | text | 'didit' or 'persona' |
| `vendor_session_id` | text | Vendor's session/token ID |
| `verification_url` | text | URL patient clicks to verify |
| `status` | text | 'pending', 'in_progress', 'completed', 'expired', 'failed' |
| `created_at` | timestamptz | Auto-generated |
| `updated_at` | timestamptz | Auto-updated |
| `expires_at` | timestamptz | Vendor session expiry |
| `metadata` | jsonb | Vendor-specific data (raw response) |

**Indexes:**
- `idx_kyc_sessions_loan_app` on `loan_application_id`
- `idx_kyc_sessions_status` on `status`
- `idx_kyc_sessions_vendor` on `vendor`

### `kyc_results`
| Column | Type | Description |
|---|---|---|
| `id` | uuid | Primary key |
| `kyc_session_id` | uuid (FK) | Links to kyc_sessions |
| `vendor` | text | 'didit' or 'persona' |
| `overall_status` | text | 'pass', 'fail', 'review_required' |
| `check_type` | text | 'identity', 'liveness', 'aml', 'sanctions' |
| `check_status` | text | 'pass', 'fail', 'pending', 'skip' |
| `check_details` | jsonb | Raw vendor response for this check |
| `score` | numeric | Vendor confidence score (if provided) |
| `flags` | jsonb[] | Array of risk flags (address mismatch, etc.) |
| `created_at` | timestamptz | Auto-generated |

**Indexes:**
- `idx_kyc_results_session` on `kyc_session_id`
- `idx_kcy_results_status` on `overall_status`

### `kyc_events`
| Column | Type | Description |
|---|---|---|
| `id` | uuid | Primary key |
| `kyc_session_id` | uuid (FK) | Links to kyc_sessions |
| `event_type` | text | 'session_created', 'verification_started', 'webhook_received', 'result_processed' |
| `vendor_event_id` | text | Vendor's event ID (if provided) |
| `payload` | jsonb | Full event payload (immutable audit trail) |
| `processed_at` | timestamptz | When PaySpyre processed it |
| `created_at` | timestamptz | Auto-generated |

**Indexes:**
- `idx_kyc_events_session` on `kyc_session_id`
- `idx_kyc_events_type` on `event_type`

### `kyc_co_borrower_links`
| Column | Type | Description |
|---|---|---|
| `id` | uuid | Primary key |
| `loan_application_id` | uuid (FK) | Links to loan_applications |
| `primary_kyc_session_id` | uuid (FK) | Links to kyc_sessions |
| `co_borrower_kyc_session_id` | uuid (FK) | Links to kyc_sessions |
| `co_borrower_role` | text | 'guarantor', 'joint_applicant' |
| `created_at` | timestamptz | Auto-generated |

**Indexes:**
- `idx_kyc_co_borrower_loan` on `loan_application_id`

### `manual_kyb_reviews`
| Column | Type | Description |
|---|---|---|
| `id` | uuid | Primary key |
| `vendor_id` | uuid (FK) | Links to vendors (clinics) |
| `business_name` | text | Legal business name |
| `business_structure` | text | 'sole_proprietor', 'partnership', 'corporation', 'other' |
| `business_registration_number` | text | BC Registry / CRA BN |
| `status` | text | 'pending_submission', 'under_review', 'approved', 'rejected' |
| `submitted_by` | uuid (FK) | Admin user who created it |
| `reviewed_by` | uuid (FK) | Admin user who approved/rejected |
| `documents` | jsonb[] | Array of Supabase Storage paths |
| `beneficial_owners` | jsonb[] | Array of KYC session IDs for each owner |
| `notes` | text | Admin review notes |
| `created_at` | timestamptz | Auto-generated |
| `updated_at` | timestamptz | Auto-updated |

**Indexes:**
- `idx_manual_kyb_vendor` on `vendor_id`
- `idx_manual_kyb_status` on `status`

---

## 3. State Machine (Underwriting)

### States & Transitions

```python
KYC_STATE_MACHINE = {
    "pending_kyc": {
        "on": {
            "kyc_session_created": "kyc_in_progress"
        }
    },
    "kyc_in_progress": {
        "on": {
            "kyc_result_received": "kyc_completed"
        },
        "timeout": "kyc_expired"
    },
    "kyc_expired": {
        "on": {
            "kyc_session_recreated": "kyc_in_progress"
        }
    },
    "kyc_completed": {
        "on": {
            "risk_eval_passed": "approved",
            "risk_eval_failed": "rejected",
            "risk_eval_review": "manual_review"
        }
    },
    "manual_review": {
        "on": {
            "review_approved": "approved",
            "review_rejected": "rejected",
            "review_more_info": "pending_kyc"  # restart with new docs
        }
    },
    "approved": {
        "on": {
            "docs_ready": "funding_prep"  # next: DocuSign
        }
    },
    "rejected": {
        "terminal": True
    }
}
```

### Event Payloads

```python
# n8n emits this to PaySpyre API
KYC_RESULT_EVENT = {
    "event_id": "evt_abc123",
    "kyc_session_id": "uuid-456",
    "loan_application_id": "uuid-789",
    "vendor": "didit",
    "overall_status": "pass",
    "checks": [
        {
            "type": "identity",
            "status": "pass",
            "details": { ... }
        },
        {
            "type": "liveness",
            "status": "pass",
            "score": 0.98
        },
        {
            "type": "aml",
            "status": "pass",
            "details": { ... }
        }
    ],
    "flags": [],
    "timestamp": "2026-05-14T18:00:00Z"
}
```

---

## 4. API Endpoints (FastAPI)

### KYC Session Management

```python
# POST /api/v1/kyc/sessions
# Create KYC session for a loan application
{
    "loan_application_id": "uuid",
    "borrower_id": "uuid",
    "vendor": "didit"  # or "persona"
}

# Response
{
    "kyc_session_id": "uuid",
    "verification_url": "https://verify.didit.me/xyz123",
    "expires_at": "2026-05-14T19:00:00Z",
    "qr_code_url": "data:image/png;base64,..."  # for in-clinic display
}

# GET /api/v1/kyc/sessions/{session_id}
# Retrieve session status

# POST /api/v1/kyc/sessions/{session_id}/recreate
# Expired session → recreate (same borrower, new vendor token)
```

### Webhook Receiver (Vendor → PaySpyre)

```python
# POST /api/v1/kyc/webhooks/{vendor}
# Didit or Persona posts verification result
# Headers: X-Didit-Signature or X-Persona-Signature

# Didit payload (example)
{
    "event": "verification.completed",
    "data": {
        "verification_id": "didit_ver_123",
        "external_id": "uuid-456",  # our kyc_session_id
        "status": "passed",
        "checks": { ... }
    }
}

# Response
{
    "received": true,
    "kyc_session_id": "uuid-456"
}
```

### Risk Rules Engine (Internal)

```python
# POST /api/v1/kyc/evaluate
# Internal API, called by n8n webhook processor
{
    "kyc_session_id": "uuid",
    "loan_application_id": "uuid"
}

# Response
{
    "decision": "approve" | "reject" | "manual_review",
    "reason": "string or null",
    "risk_score": 0.85,
    "flags_applied": []
}
```

### Manual KYB Admin (Operator Portal)

```python
# GET /api/v1/kyb/reviews
# List pending reviews (admin only, VPN/IP-restricted)

# POST /api/v1/kyb/reviews
# Create new KYB review request

# POST /api/v1/kyb/reviews/{review_id}/approve
# Approve vendor KYB

# POST /api/v1/kyb/reviews/{review_id}/reject
# Reject vendor KYB
```

### Co-borrower Management

```python
# POST /api/v1/kyc/co-borrowers/link
# Link two KYC sessions to one loan application
{
    "loan_application_id": "uuid",
    "primary_kyc_session_id": "uuid",
    "co_borrower_kyc_session_id": "uuid",
    "co_borrower_role": "guarantor"
}

# GET /api/v1/loan-applications/{id}/co-borrowers
# Retrieve linked verifications
```

### Audit & Reporting

```python
# GET /api/v1/kyc/audit/events
# FINTRAC examination query
# Query params: kyc_session_id, event_type, date_range, vendor

# Response
{
    "events": [
        {
            "id": "uuid",
            "event_type": "webhook_received",
            "timestamp": "2026-05-14T18:00:00Z",
            "payload": { ... },
            "processed_at": "2026-05-14T18:00:01Z"
        }
    ],
    "total": 1250,
    "page": 1,
    "page_size": 50
}
```

---

## 5. Risk Rules Engine (PaySpyre IP)

### Rule Categories

```python
RISK_RULES = {
    "identity_match": {
        "description": "Name/DOB match ID document",
        "threshold": 0.9,
        "on_fail": "manual_review",
        "severity": "high"
    },
    "address_match": {
        "description": "Address on ID matches application",
        "threshold": "exact",
        "on_fail": "manual_review",
        "severity": "medium"
    },
    "liveness_confidence": {
        "description": "Face liveness score",
        "threshold": 0.95,
        "on_fail": "reject",
        "severity": "critical"
    },
    "aml_hits": {
        "description": "AML / sanctions / PEP matches",
        "threshold": 0,
        "on_fail": "reject",
        "severity": "critical"
    },
    "thin_file_hygiene": {
        "description": "Low credit history + high loan amount",
        "rule": "credit_history_months < 12 and loan_amount > 5000",
        "on_match": "manual_review",
        "severity": "medium"
    },
    "multiple_applications": {
        "description": "Same identity, multiple pending apps (fraud signal)",
        "rule": "count(pending_apps with same_name/dob) > 1",
        "on_match": "reject",
        "severity": "critical"
    },
    "canada_address_required": {
        "description": "PIPEDA — must have Canadian address",
        "rule": "address.country != 'CA'",
        "on_match": "reject",
        "severity": "critical"
    }
}
```

### Composite Decision Logic

```python
def evaluate_risk(kyc_result: dict, loan_app: dict) -> dict:
    """
    Returns:
    {
        "decision": "approve" | "reject" | "manual_review",
        "reason": "...",
        "risk_score": float,
        "flags_applied": [...]
    }
    """
    flags = []
    risk_score = 1.0  # start at 1.0, deduct for each issue

    # Critical checks = auto-reject
    if kyc_result["liveness"]["score"] < 0.95:
        return {
            "decision": "reject",
            "reason": "Liveness check failed",
            "risk_score": 0.0,
            "flags_applied": ["liveness_failed"]
        }

    if any(hit for hit in kyc_result["aml"]["hits"]):
        return {
            "decision": "reject",
            "reason": "AML / sanctions hit",
            "risk_score": 0.0,
            "flags_applied": ["aml_hit"]
        }

    if loan_app["address"]["country"] != "CA":
        return {
            "decision": "reject",
            "reason": "Non-Canadian address (PIPEDA)",
            "risk_score": 0.0,
            "flags_applied": ["non_ca_address"]
        }

    # Identity match = high severity
    if kyc_result["identity"]["match_score"] < 0.9:
        flags.append("identity_mismatch")
        risk_score -= 0.3

    # Address mismatch = medium severity
    if kyc_result["address"]["match_status"] != "exact":
        flags.append("address_mismatch")
        risk_score -= 0.2

    # Thin file + high loan = medium severity
    if loan_app["credit_history_months"] < 12 and loan_app["loan_amount"] > 5000:
        flags.append("thin_file_high_amount")
        risk_score -= 0.15

    # Multiple applications = critical
    duplicate_check = check_duplicate_applications(loan_app)
    if duplicate_check["count"] > 1:
        flags.append("multiple_applications")
        return {
            "decision": "reject",
            "reason": f"Multiple pending applications ({duplicate_check['count']})",
            "risk_score": 0.0,
            "flags_applied": flags
        }

    # Decision
    if risk_score >= 0.85:
        decision = "approve"
        reason = "All checks passed"
    elif risk_score >= 0.6:
        decision = "manual_review"
        reason = "Requires human review: " + ", ".join(flags)
    else:
        decision = "reject"
        reason = "Risk score too low: " + ", ".join(flags)

    return {
        "decision": decision,
        "reason": reason,
        "risk_score": max(0.0, risk_score),
        "flags_applied": flags
    }
```

---

## 6. Security & Compliance

### Webhook Signature Verification

```python
# Didit: X-Didit-Signature header
# Verify HMAC-SHA256 with vendor secret

def verify_didit_webhook(payload: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)

# Persona: X-Persona-Signature header (same pattern)
```

### Data Retention (FINTRAC)

- **KYC results:** Keep 5 years after relationship ends (FINTRAC minimum)
- **Audit events:** Keep 5 years
- **Supabase Storage docs:** Keep 5 years, then archive to cold storage

### PIPEDA Data Residency

- All KYC data stored in `ca-central-1` (Montreal)
- Vendor (Didit/Persona) must confirm Canadian PII can be pinned to Canada/US/EU
- This is an open question in the handoff — confirm before signing

### RLS (Row-Level Security)

```sql
-- Clinics see only their own KYC sessions
CREATE POLICY clinic_kyc_isolation ON kyc_sessions
  FOR SELECT
  USING (
    loan_application_id IN (
      SELECT id FROM loan_applications
      WHERE clinic_id = current_clinic_id()
    )
  );

-- Admins (Mike/David) see everything
CREATE POLICY admin_kyc_full_access ON kyc_sessions
  FOR ALL
  USING (is_admin());
```

---

## 7. Implementation Checklist

- [ ] Create Supabase tables (`kyc_sessions`, `kyc_results`, `kyc_events`, `kyc_co_borrower_links`, `manual_kyb_reviews`)
- [ ] Run Alembic migrations
- [ ] Implement FastAPI endpoints (`POST /kyc/sessions`, `POST /kyc/webhooks/{vendor}`, `POST /kyc/evaluate`)
- [ ] Build webhook signature verification (Didit + Persona)
- [ ] Implement risk rules engine (PaySpyre IP)
- [ ] Wire up n8n webhook → Supabase real-time → PaySpyre API
- [ ] Build underwriting state machine
- [ ] Create manual KYB admin UI (Operator Portal)
- [ ] Implement co-borrower linking
- [ ] Build audit/reporting dashboard
- [ ] Add FINTRAC audit export endpoint
- [ ] Test with Didit sandbox (free tier)
- [ ] Test with Persona sandbox (backup)
- [ ] Load test: 50 concurrent KYC sessions
- [ ] Document vendor failover (Didit → Persona cutover)

---

## 8. Vendor Comparison (At-a-Glance)

| Feature | Didit | Persona |
|---|---|---|
| Pricing | 500 free/mo, then $0.15/verification | $250/mo for 500 verifications |
| AML check | Premium add-on (price TBD) | Not specified |
| Data residency | Question — confirm before signing | Likely US (need PIPEDA check) |
| Liveness | ISO 30107-3 PAD Level 2 | Not specified |
| ISO 27001 | Yes | Yes |
| Canada-specific page | Yes | No |
| Company stage | YC W26 | Established |
| Verdict | **Primary** | Plan B |

---

**Status:** Draft, ready for review by David (legal/compliance) and implementation by Mike.