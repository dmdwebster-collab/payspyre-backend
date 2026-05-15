# PaySpyre Financial — Comprehensive Security Documentation

**Document Classification:** Confidential — For Internal Use, Investor Due Diligence, Regulatory Review  
**Version:** 1.0  
**Effective Date:** 2025  
**Document Owner:** Chief Technology Officer / Designated Privacy Officer  
**Review Cycle:** Annual (or following any material system change or security incident)

---

## Table of Contents

1. [Security Architecture Overview](#1-security-architecture-overview)
2. [Data Classification & Protection](#2-data-classification--protection)
3. [Authentication & Access Control](#3-authentication--access-control)
4. [Encryption Standards](#4-encryption-standards)
5. [Infrastructure Security](#5-infrastructure-security)
6. [PIPEDA Compliance](#6-pipeda-compliance)
7. [FINTRAC Security Requirements](#7-fintrac-security-requirements)
8. [Incident Response Plan](#8-incident-response-plan)
9. [Monitoring & Alerting](#9-monitoring--alerting)
10. [Backup & Disaster Recovery](#10-backup--disaster-recovery)
11. [Security Audit Checklist (Pre-Launch)](#11-security-audit-checklist-pre-launch)
12. [Penetration Testing Plan](#12-penetration-testing-plan)

---

## 1. Security Architecture Overview

### 1.1 System Architecture

PaySpyre's lending platform is built on a multi-tier architecture designed with security-by-design principles, data minimization, and defense-in-depth.

| Component | Technology | Role | Exposure |
|---|---|---|---|
| Consumer Portal | React (static, CDN-hosted) | Patient-facing loan application | Public internet |
| Vendor Portal | React (static, CDN-hosted) | Dental clinic loan initiation | Public internet |
| Admin Panel | React (static, CDN-hosted) | Internal team operations | VPN / IP-restricted |
| Backend API | FastAPI (Python) | Core business logic, all data operations | Public (TLS only) |
| Underwriting Engine | Python service (internal) | Loan decision processing | Internal only |
| Database | PostgreSQL | Primary data store | Internal only (no internet exposure) |
| Bank Verification | Flinks API (third-party) | Open banking data, bank account verification | Third-party SaaS |
| Payment Processing | Rotessa (third-party) | Pre-Authorized Debit (PAD) collection | Third-party SaaS |
| SMS / Notifications | Twilio API (third-party) | OTP delivery, application status | Third-party SaaS |
| Document Signing | DocuSign API (third-party) | Loan agreement e-signatures | Third-party SaaS |
| KYC / Identity | OCR + face matching (internal) | Driver's licence / passport verification | Internal service |

### 1.2 Data Flow Diagram

The following diagram illustrates the end-to-end flow of patient data through the PaySpyre platform:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        PATIENT APPLICATION FLOW                         │
└─────────────────────────────────────────────────────────────────────────┘

1. PATIENT SUBMITS APPLICATION
   Browser → [TLS 1.3 encrypted] → FastAPI Backend
   - Personal info (name, DOB, SIN, address) submitted via HTTPS POST
   - All fields validated and sanitized server-side before processing
   - Rate limiting: 10 applications/hour per IP enforced at edge

2. IDENTITY VERIFICATION
   FastAPI → KYC OCR Service (internal)
   - Identity document (driver's licence / passport) uploaded via secure
     multipart form
   - OCR extracts name, DOB, document number
   - Face matching compares selfie to document photo
   - Result stored as verified/unverified flag — document image purged
     after verification per data minimization policy

3. BANK VERIFICATION (FLINKS)
   Patient Browser → [TLS 1.3] → Flinks Widget → Flinks API
   - Patient authenticates directly with their bank via Flinks
   - Flinks returns a read-only token to PaySpyre backend
   - PaySpyre calls Flinks Enrich API to retrieve transactions, income,
     cash flow — credentials never transmitted to PaySpyre servers
   - Flinks token stored encrypted (AES-256 field encryption)

4. DATA STORED ENCRYPTED AT REST
   FastAPI → PostgreSQL
   - CRITICAL fields (SIN, bank account numbers, Flinks tokens) encrypted
     with AES-256-GCM before writing to database
   - All database connections use TLS (pg_hba.conf enforced)
   - Database not accessible from public internet

5. UNDERWRITING ENGINE PROCESSES APPLICATION
   FastAPI → Internal Underwriting Service
   - Application data passed via internal API call (no internet exposure)
   - CashScore calculated from Flinks transaction data
   - Credit bureau data integrated (if applicable)
   - Decision: Approve / Decline / Review — written back to database
   - All processing events logged to audit trail

6. DECISION STORED & LOAN DOCUMENTS GENERATED
   Underwriting Engine → PostgreSQL → DocuSign API
   - Approved loan terms written to database with full audit trail
   - Loan agreement PDF generated from template with borrower's terms
   - DocuSign API called [TLS 1.3] to create envelope for e-signature
   - DocuSign sends signing link to patient via email

7. PAD AUTHORIZATION & PAYMENT SETUP
   Patient → DocuSign [signed PAD agreement] → FastAPI
   - Signed PAD agreement transmitted back via DocuSign webhook [TLS 1.3,
     webhook signature verified]
   - Rotessa API called to register PAD authorization
   - Bank account number transmitted to Rotessa encrypted in transit
   - Rotessa stores payment data in their PCI-compliant vault

8. ONGOING LOAN SERVICING
   Rotessa → [webhook, HMAC-verified] → FastAPI → PostgreSQL
   - Payment events (success, NSF, decline) received via Rotessa webhook
   - Events validated against HMAC signature before processing
   - Payment records stored and available to borrower via authenticated portal
```

### 1.3 Network Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    PUBLIC INTERNET ZONE                         │
│                                                                 │
│  Patients/Vendors → Cloudflare CDN / AWS CloudFront            │
│  (DDoS protection, WAF, TLS termination, static asset cache)   │
└────────────────────┬────────────────────────────────────────────┘
                     │ HTTPS only (443)
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                    APPLICATION TIER (AWS / VPS)                 │
│                                                                 │
│  FastAPI Backend (Python)                                       │
│  - Runs as non-root Docker container                            │
│  - Ports exposed: 443 (HTTPS) only                              │
│  - SSH access: port 22, IP-restricted (team IPs only)           │
│  - Internal APIs: bind to 127.0.0.1, not 0.0.0.0               │
└────────────────────┬────────────────────────────────────────────┘
                     │ Internal only (no internet exposure)
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                    DATA TIER (Private Subnet)                   │
│                                                                 │
│  PostgreSQL Database                                            │
│  - Bound to 127.0.0.1 or private subnet IP only                 │
│  - Connections require TLS + application credentials            │
│  - No direct internet access                                    │
│  - Security group / firewall: only app server IP permitted      │
│                                                                 │
│  Backups: Encrypted → Separate AWS Region (ca-west-1)           │
└─────────────────────────────────────────────────────────────────┘
```

**Exposed to the internet:**
- Ports 443 (HTTPS) on application server — all traffic passes through Cloudflare or AWS WAF
- No other ports exposed to the public internet

**Internal only (not internet-accessible):**
- PostgreSQL database (port 5432 — blocked by security group)
- Underwriting engine (internal service)
- Admin panel (IP-restricted)
- SSH (port 22 — restricted to team IP allowlist)

### 1.4 Third-Party Integration Security

| Integration | Purpose | Security Certifications | Data Shared | Access Model |
|---|---|---|---|---|
| **Flinks** | Bank verification & transaction data | SOC 2 Type II, ISO 27001-inspired framework, AES-256 at rest, per-user encryption key rotation | Read-only bank transaction data, income, account ownership | OAuth-style token; credentials never touch PaySpyre servers |
| **Rotessa** | PAD payment processing | Payments Canada (Rule H1) compliant, AES-256 encryption at rest, PCI DSS adherent, ACAMS AML certified staff | Bank account number, PAD authorization | HTTPS API with API key authentication; webhook HMAC verification |
| **Twilio** | SMS delivery (OTP, notifications) | SOC 2 Type II | Mobile phone number, message content | HTTPS API with Account SID + Auth Token |
| **DocuSign** | E-signature for loan agreements | SOC 2 Type II, ISO 27001:2022, ISO 27017, ISO 27018, PCI DSS v4.0 | Borrower name, email, loan document | HTTPS API with OAuth 2.0; webhook signature verification |

**Third-Party Data Processing Agreements:** PaySpyre must maintain signed Data Processing Agreements (DPAs) with all third-party processors that handle personal information on PaySpyre's behalf, as required under PIPEDA Principle 1 (Accountability).

---

## 2. Data Classification & Protection

### 2.1 Classification Framework

All data processed by PaySpyre is classified into one of four tiers. Classification determines encryption requirements, access controls, retention periods, and disposal methods.

---

### CRITICAL — Highest Sensitivity

**Data types:** Social Insurance Number (SIN), bank account numbers, Flinks read tokens / bank credentials, PAD authorization with banking details, identity document images (pre-verification)

| Attribute | Requirement |
|---|---|
| Encryption at rest | AES-256-GCM field-level encryption before database storage; keys stored in AWS KMS or HashiCorp Vault (not in application code or database) |
| Encryption in transit | TLS 1.3 minimum; HSTS enforced |
| UI display | Masked in all user interfaces (e.g., SIN displayed as `***-***-XXX`, bank account as `****1234`) |
| Access — who can read | SYSTEM (automated processing only); ADMIN (with explicit justification logged); BORROWER (own data, masked) |
| Access — who cannot read | VENDOR, UNDERWRITER (sees derived data only, not raw CRITICAL fields) |
| Logging | Every read and write of CRITICAL fields logged to tamper-evident audit log with user ID, timestamp, IP address, action |
| Retention | Loan active + 7 years (regulatory minimum for financial records in Canada); SIN and identity documents retained minimum period necessary only |
| Disposal | Cryptographic erasure (destroy the encryption key) + secure overwrite; documented certificate of destruction |

---

### SENSITIVE — High Sensitivity

**Data types:** Full name, date of birth, home address, employment information, income and expenses, credit score, CashScore, loan terms and amounts, application decisions, loan repayment history, payment amounts, dental treatment type

| Attribute | Requirement |
|---|---|
| Encryption at rest | Encrypted at database level (transparent data encryption + column-level for highest-sensitivity fields) |
| Encryption in transit | TLS 1.3 minimum |
| Access — who can read | BORROWER (own data), UNDERWRITER (during active application), ADMIN (with audit log), SYSTEM |
| Access — VENDOR | Limited: vendors may see application status and loan amount for their clinic's patients; no access to financial details, SIN, or health information |
| Logging | All reads and writes logged; bulk export operations trigger alert |
| Retention | Loan active + 7 years |
| Disposal | Secure overwrite; documented |

---

### INTERNAL — Moderate Sensitivity

**Data types:** Application workflow events, underwriting reports, internal notes, collections notes, admin actions, system configuration, audit logs

| Attribute | Requirement |
|---|---|
| Encryption at rest | Database-level encryption |
| Encryption in transit | TLS 1.3 |
| Access | UNDERWRITER, ADMIN, SYSTEM |
| Logging | Logged at system level |
| Retention | 5 years minimum (FINTRAC record-keeping requirement for compliance-related records) |
| Disposal | Secure overwrite |

---

### PUBLIC — No Special Sensitivity

**Data types:** Dental treatment type descriptions (generic), interest rate ranges (public), loan calculator results, marketing content, publicly posted privacy policy

| Attribute | Requirement |
|---|---|
| Encryption at rest | Standard database storage (no special requirement) |
| Encryption in transit | TLS (standard web security) |
| Access | Unrestricted |
| Logging | Standard web server access logs |
| Retention | As needed for business operations |
| Disposal | Standard deletion |

---

### 2.2 Data Minimization Policy

In accordance with PIPEDA Principle 4 (Limiting Collection), PaySpyre collects only the data strictly necessary for each defined purpose:

| Purpose | Data Collected | Data NOT Collected |
|---|---|---|
| Identity verification | Name, DOB, address, government ID number (not full document retained post-verification) | Medical history, social media profiles, employment history beyond income verification |
| Credit underwriting | Income, bank transactions (90–365 days), existing obligations, employment status | Racial origin, religion, sexual orientation, health information beyond what patient volunteers |
| Loan servicing | Bank account for PAD, contact info for notices | Marketing preferences (collected separately with consent) |
| FINTRAC compliance | Identity verification records, transaction history | Beyond what PCMLTFA requires |

### 2.3 Data Retention Schedule

| Data Category | Retention Period | Basis |
|---|---|---|
| Identity verification records (KYC) | 5 years from last transaction | FINTRAC PCMLTFA requirement |
| Loan application records | 7 years from loan closure | Financial records retention (Canada) |
| Payment transaction records | 7 years | CRA and financial regulatory requirement |
| PIPEDA breach records | 2 years minimum from date of breach | PIPEDA Breach of Security Safeguards Regulations |
| Audit logs | 3 years | Security best practice; regulatory review window |
| Marketing consent records | Duration of consent + 3 years | CASL compliance |
| Declined applications | 7 years | Credit reporting regulatory requirements |

---

## 3. Authentication & Access Control

### 3.1 JWT Implementation

PaySpyre uses JSON Web Tokens (JWT) for all API authentication.

**Token structure:**
```json
{
  "header": {
    "alg": "RS256",
    "typ": "JWT"
  },
  "payload": {
    "sub": "user_uuid",
    "role": "BORROWER | VENDOR | UNDERWRITER | ADMIN | SYSTEM",
    "session_id": "uuid",
    "iat": 1700000000,
    "exp": 1700000900,
    "jti": "unique_token_id"
  }
}
```

**Token specifications:**
- **Algorithm:** RS256 (asymmetric) — private key signs tokens, public key verifies; compromise of the public key does not allow token forgery
- **Access token expiry:** 15 minutes
- **Refresh token expiry:** 7 days (stored in httpOnly, Secure, SameSite=Strict cookie — not in localStorage)
- **Token rotation:** On every refresh, the old refresh token is invalidated and a new one issued (rotation prevents token replay attacks)
- **Token revocation:** Revocation list maintained in Redis / database for immediate invalidation (e.g., on logout, password change, suspected compromise)
- **jti (JWT ID):** Unique per token; used for revocation checking

**Refresh token security:**
- Stored server-side (hashed) in database, not purely stateless
- Refresh token reuse detection: if a previously-used refresh token is presented, all tokens for that session are immediately revoked and the user is forced to re-authenticate

### 3.2 Password Security

| Requirement | Specification |
|---|---|
| Hashing algorithm | bcrypt with work factor ≥ 12 (reviewed annually and increased as hardware capabilities improve) |
| Minimum length | 12 characters |
| Complexity | Must include at least one uppercase, one lowercase, one digit, and one special character |
| Password history | Last 10 passwords cannot be reused |
| Account lockout | 5 failed attempts → 15-minute lockout; 10 failed attempts → account locked, requires email unlock or admin intervention |
| Breach checking | Passwords checked against Have I Been Pwned (HIBP) API (k-anonymity model — only first 5 chars of SHA-1 hash transmitted) at registration and password change |
| Default passwords | None — system-generated temporary passwords expire after 24 hours and force change on first login |

### 3.3 Role-Based Access Control (RBAC)

#### BORROWER
- **Can access:** Own application data, own loan status, own payment history, own documents, own profile (update permitted)
- **Cannot access:** Other borrowers' data, internal underwriting notes, admin functions, system configuration
- **Authentication:** Email/password + SMS OTP (Twilio) for sensitive actions (PAD setup, SIN entry)

#### VENDOR (Dental Clinic)
- **Can access:** Applications submitted through their clinic, status of those applications, loan amount confirmed for their patient (not the patient's financial details), payment confirmation from their clinic's loan
- **Cannot access:** Full borrower financial data, SIN, bank account details, other clinics' patients, underwriting engine, admin panel
- **Authentication:** Email/password + SMS OTP for login

#### UNDERWRITER
- **Can access:** Full application data for assigned applications (including SENSITIVE data), credit bureau output, Flinks-enriched data, ability to approve/decline/modify applications, internal notes, collections queue
- **Cannot access:** Other system configuration, user management, CRITICAL raw fields (SIN is masked; bank account numbers are masked; they see derived data)
- **Authentication:** Email/password + authenticator app MFA (TOTP, mandatory)

#### ADMIN
- **Can access:** All borrower data (with access logged), all vendor data, user management, system configuration, audit logs, collections management, reporting
- **Cannot access:** Encryption key material directly (KMS/Vault handles keys; admin cannot export raw keys)
- **Authentication:** Email/password + authenticator app MFA (TOTP, mandatory) + IP restriction to office/VPN IP range
- **Principle of least privilege:** Admin accounts used only when required; day-to-day work done with standard user accounts

#### SYSTEM
- **Purpose:** Internal service-to-service communication (underwriting engine to database, webhook processors, scheduled jobs)
- **Authentication:** Long-lived API keys with limited scope (not user-facing JWTs); keys rotated every 90 days
- **Cannot access:** User management, configuration changes requiring human authorization

### 3.4 Multi-Factor Authentication (MFA)

| Role | MFA Type | Status |
|---|---|---|
| ADMIN | TOTP authenticator app (Google Authenticator, Authy) | **Mandatory — enforced at login** |
| UNDERWRITER | TOTP authenticator app | **Mandatory — enforced at login** |
| VENDOR | SMS OTP (Twilio) | Required for login |
| BORROWER | SMS OTP (Twilio) | Required for sensitive actions (first login, PAD setup, SIN submission) |

**Backup codes:** Generated at MFA enrollment; stored by the user; system stores bcrypt-hashed backup codes only.

**MFA bypass policy:** No MFA bypass permitted except through formal account recovery process requiring identity verification. Recovery codes are single-use.

### 3.5 Session Management

| Parameter | Value |
|---|---|
| Session timeout (inactivity) | 30 minutes for all roles |
| Session timeout (absolute) | 8 hours for BORROWER/VENDOR; 4 hours for ADMIN/UNDERWRITER |
| Concurrent sessions | BORROWER/VENDOR: 2 concurrent sessions permitted; ADMIN/UNDERWRITER: 1 session per user |
| Session termination | Logout invalidates refresh token server-side immediately |
| Sensitive action re-authentication | For ADMIN: operations that delete data or export bulk records require password re-confirmation |
| Idle session notification | 5-minute warning before automatic logout |

### 3.6 API Security

**Rate Limiting:**

| Endpoint Category | Limit | Window |
|---|---|---|
| Login / authentication | 10 attempts per IP | 5 minutes (then exponential backoff) |
| Application submission | 5 per IP | 1 hour |
| OTP requests (SMS) | 3 per phone number | 10 minutes |
| General API (authenticated) | 300 requests per user | 1 minute |
| General API (unauthenticated) | 60 requests per IP | 1 minute |
| Webhooks (Flinks, Rotessa) | Validated by HMAC signature before processing; no rate limit bypass |

**Input Validation:**
- All inputs validated against strict schemas using Pydantic (FastAPI's native validation)
- String inputs sanitized to prevent SQL injection (parameterized queries enforced — no string interpolation into SQL)
- File uploads: type validation, size limits, content inspection (magic bytes, not just file extension)
- JSON depth and size limits enforced to prevent DoS via deeply nested payloads

**SQL Injection Prevention:**
- ORM (SQLAlchemy) enforced throughout — raw SQL prohibited except in reviewed migrations
- Parameterized queries mandatory for all database interactions
- Database user has minimal privileges (no DROP, no schema alteration — separate migration user)

**XSS Prevention:**
- React frontend auto-escapes HTML by default — `dangerouslySetInnerHTML` prohibited by code review policy
- Content Security Policy (CSP) headers configured: `default-src 'self'`; external scripts require explicit allowlisting
- `X-Content-Type-Options: nosniff` header set
- `X-Frame-Options: DENY` header set

**CORS Configuration:**
- Allowed origins: `https://payspyre.com`, `https://app.payspyre.com`, `https://vendor.payspyre.com`
- CORS headers not set to wildcard (`*`) under any circumstances
- Pre-flight cache set to 600 seconds

### 3.7 Third-Party API Key Management

| Service | Storage Method | Rotation Policy | Access |
|---|---|---|---|
| Flinks API key | AWS Secrets Manager / environment variable (never in code or version control) | Every 90 days or upon staff departure | Backend only; never exposed to frontend |
| Rotessa API key | AWS Secrets Manager / environment variable | Every 90 days or upon staff departure | Backend only |
| Twilio Account SID + Auth Token | AWS Secrets Manager / environment variable | Every 90 days or upon staff departure | Backend only |
| DocuSign OAuth client secret | AWS Secrets Manager / environment variable | Every 90 days or upon staff departure | Backend only |
| Database credentials | AWS Secrets Manager (with automatic rotation enabled) | Every 30 days | Backend only |
| JWT signing key (RSA private key) | AWS Secrets Manager or HSM | Annually or upon suspected compromise | Backend only |

**Key management policy:**
- No credentials in source code, configuration files committed to version control, or CI/CD environment variable logs
- `.env` files are in `.gitignore` and never committed
- All production secrets accessible only to application processes via IAM role / environment injection — never to developers directly
- Key rotation procedure: rotate in Secrets Manager → deploy new version → verify → revoke old key

---

## 4. Encryption Standards

### 4.1 Encryption in Transit

| Connection | Standard | Details |
|---|---|---|
| Browser to API | TLS 1.3 (minimum TLS 1.2) | TLS 1.0 and 1.1 disabled at Cloudflare / load balancer level |
| API to PostgreSQL | TLS (SSL mode=verify-full) | Certificate verification enforced |
| API to Flinks | TLS 1.3 (Flinks-enforced) | Flinks uses 256-bit HTTPS |
| API to Rotessa | TLS 1.3 (Rotessa-enforced) | AES-256 in transit per Rotessa security standards |
| API to Twilio | TLS 1.3 (Twilio-enforced) | |
| API to DocuSign | TLS 1.3 (DocuSign-enforced) | DocuSign ISO 27001:2022 certified |
| Internal services | TLS (mutual TLS recommended for internal APIs in production) | |

**HTTP Security Headers (mandatory on all responses):**
```
Strict-Transport-Security: max-age=31536000; includeSubDomains; preload
Content-Security-Policy: default-src 'self'; script-src 'self'; ...
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
X-XSS-Protection: 1; mode=block
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: geolocation=(), microphone=(), camera=()
```

**Certificate Management:**
- TLS certificates provisioned via Let's Encrypt (auto-renewal) or AWS Certificate Manager
- Certificate expiry monitored; alert triggered 30 days before expiry
- HSTS preload registration: `payspyre.com` submitted to HSTS preload list to prevent SSL stripping
- Certificate pinning: considered for native mobile app (future); not applicable to web

### 4.2 Encryption at Rest

| Data | Encryption Standard | Key Management |
|---|---|---|
| CRITICAL fields in database (SIN, bank account numbers, Flinks tokens) | AES-256-GCM field-level encryption via application layer | AWS KMS Customer Managed Key (CMK) or HashiCorp Vault |
| SENSITIVE fields in database | AES-256 via database-level transparent data encryption (TDE) or pgcrypto | Database encryption key managed via KMS |
| Database files (all data on disk) | AES-256 full-disk encryption (AWS RDS encryption enabled, or LUKS on self-hosted) | AWS-managed EBS encryption keys |
| Backup files | AES-256 (separate backup encryption key from primary) | Separate KMS key; stored in separate AWS account or geographic region |
| Application logs | Encrypted at rest on log storage (AWS CloudWatch with KMS, or S3 with SSE) | KMS |
| Identity documents (temporary) | AES-256; purged after verification | Ephemeral key; document deleted within 24 hours of successful verification |

**Why AES-256-GCM for CRITICAL fields:**
- GCM (Galois/Counter Mode) provides both encryption (confidentiality) and authentication (integrity) — detects tampering with encrypted data
- Superior to CBC mode which requires separate MAC
- NIST-recommended for symmetric encryption in financial applications

### 4.3 Key Management

**Production Recommendation: AWS KMS or HashiCorp Vault**

PaySpyre's production deployment should use AWS KMS (for AWS-hosted deployments) or HashiCorp Vault (for multi-cloud or hybrid deployments). Both provide:

| Capability | How Implemented |
|---|---|
| Key generation | Hardware Security Module (HSM) backed key generation |
| Key storage | Keys never leave the KMS/Vault boundary; application receives encrypted data back |
| Key rotation | Automatic rotation every 365 days (AWS KMS) or configured rotation policy (Vault) |
| Key destruction | Full key deletion workflow with audit log and mandatory approval |
| Access control | IAM policies restrict which services/users can use which keys |
| Audit trail | Every key usage logged to CloudTrail / Vault audit log |

**Encryption key hierarchy:**
```
Root Key (HSM-backed, KMS)
    └── Data Encryption Key (DEK) — rotated per record or per user
         └── Used to encrypt: SIN, bank account, Flinks token
    └── Database Master Key
         └── Encrypts: tablespace, WAL archive, backups
    └── Backup Encryption Key (separate from primary DEK)
         └── Encrypts: daily database backups
    └── JWT Signing Key (RSA-2048 or RSA-4096)
         └── Signs: all access and refresh tokens
```

**Pre-production / Development:**
- Development environments must use separate, non-production keys
- Production keys must never be accessible in development or staging environments
- Developers use synthetic/anonymized test data — production data never used in development

### 4.4 Backup Encryption

- All database backups encrypted using AES-256 with a **separate** backup encryption key (not the same key used for the live database)
- Backup encryption key stored in a separate AWS KMS key or offline secure location
- Backups stored in a geographically separate AWS region (ca-west-1 as primary, with ca-central-1 as live database region)
- Backup integrity: each backup verified with a cryptographic hash (SHA-256) after creation; hash stored separately
- Backup restoration tested quarterly as part of Disaster Recovery testing (see Section 10)

---

## 5. Infrastructure Security

### 5.1 Deployment Architecture

**Primary deployment:** AWS ca-central-1 (Canada Central — Montreal)  
**Backup / DR region:** AWS ca-west-1 (Canada West — Calgary)  
**Data residency:** All data (primary, backup, logs) stored within Canada, satisfying PIPEDA data residency expectations and provincial privacy law requirements in BC (PIPA BC) and Alberta (PIPA AB).

**Rationale for Canadian data residency:**
- PIPEDA does not prohibit cross-border transfers but requires equivalent protection and organizational accountability
- Storing all data in Canada eliminates cross-border risk and simplifies compliance with provincial regulators (OIPC BC, OIPC AB)
- Financial regulators (OSFI, provincial regulators) increasingly expect Canadian data residency for financial data

### 5.2 Network Segmentation

```
Internet
    │
    ▼
[Cloudflare WAF / AWS Shield] — DDoS protection, bot mitigation
    │
    ▼
[Public Subnet] — Only HTTPS (443) and SSH (22, IP-restricted)
    │  FastAPI Application Server
    │  Load Balancer (if multi-instance)
    │
    ▼ (Private subnet only — no internet route)
[Private Subnet]
    │  PostgreSQL Database (port 5432 — firewall blocks all except app server)
    │  Internal services (underwriting engine, KYC service)
    │
    ▼
[Separate Isolated Subnet]
    │  Monitoring / log aggregation (CloudWatch, or self-hosted ELK)
    │  Backup transfer to ca-west-1
```

### 5.3 Firewall Rules

| Direction | Source | Destination | Port | Protocol | Action |
|---|---|---|---|---|---|
| Inbound | 0.0.0.0/0 (internet) | Application server | 443 | HTTPS/TLS | ALLOW |
| Inbound | Team IP allowlist only | Application server | 22 | SSH | ALLOW |
| Inbound | Application server IP | Database | 5432 | PostgreSQL/TLS | ALLOW |
| Inbound | ANY | Database | 5432 | ANY | **DENY** |
| Inbound | Application server IP | Internal services | Internal ports | HTTPS/TLS | ALLOW |
| Inbound | ANY | ANY | 80 (HTTP) | HTTP | DENY (redirect to 443) |
| Outbound | Application server | Flinks API | 443 | HTTPS | ALLOW |
| Outbound | Application server | Rotessa API | 443 | HTTPS | ALLOW |
| Outbound | Application server | Twilio API | 443 | HTTPS | ALLOW |
| Outbound | Application server | DocuSign API | 443 | HTTPS | ALLOW |
| Outbound | Application server | AWS KMS | 443 | HTTPS | ALLOW |

**AWS Security Group implementation:**
- All resources in private VPC
- Security groups follow least-privilege (only named source/destination allowed)
- Security group changes require peer review and are logged via AWS CloudTrail
- Network ACLs provide stateless backup layer to security groups

### 5.4 Database Security

| Control | Implementation |
|---|---|
| Network access | PostgreSQL bound to private subnet IP; security group blocks all except app server |
| Authentication | Strong password + optional certificate authentication; no default/generic accounts |
| Least privilege | Application user has SELECT, INSERT, UPDATE, DELETE only — no CREATE TABLE, DROP, GRANT |
| Separate migration user | Schema migrations run by a separate higher-privilege user during deployment only |
| Connection encryption | `sslmode=verify-full` enforced; plaintext connections rejected |
| Connection pooling | PgBouncer (or built-in pool) limits connections; monitors pool exhaustion |
| Audit logging | `pgaudit` extension enabled — logs all DDL changes and sensitive data access |
| PostgreSQL version | Always current supported version; automatic minor version updates enabled |
| Point-in-time recovery | WAL archiving enabled (see Section 10) |

### 5.5 Container Security

PaySpyre services run as Docker containers:

| Control | Implementation |
|---|---|
| Base images | Official minimal images (e.g., `python:3.12-slim`, Alpine-based where feasible); no general-purpose OS images |
| Root user | Containers run as non-root user (UID ≥ 1000); USER directive set in Dockerfile |
| Read-only filesystem | Root filesystem mounted read-only where possible; specific write directories mounted explicitly |
| Capabilities | All Linux capabilities dropped by default; only required capabilities added |
| Secrets in images | Zero — no credentials, keys, or environment-specific config in Docker images |
| Vulnerability scanning | Images scanned with Trivy or AWS ECR image scanning on every build; critical/high CVEs block deployment |
| Image signing | Docker image signing (Docker Content Trust or AWS Signer) for production deployments |
| Resource limits | CPU and memory limits set per container to prevent resource exhaustion attacks |

### 5.6 Operating System Hardening

| Control | Implementation |
|---|---|
| SSH authentication | SSH key-only authentication; password authentication disabled in `sshd_config` |
| SSH key management | Individual SSH keys per team member; keys rotated on staff departure within 24 hours |
| Root SSH | Root login via SSH disabled; use non-root user + `sudo` for privileged operations |
| Automatic security updates | `unattended-upgrades` (Ubuntu) or equivalent configured for automatic security patch application |
| Fail2ban | Configured to ban IPs after 5 failed SSH attempts; also configured for web application brute force |
| Unused services | All non-essential services disabled and removed; only required ports listening |
| Host-based firewall | `ufw` or `iptables` providing host-level firewall as backup to security groups |
| OS auditing | `auditd` configured to log privileged command execution, file access to sensitive directories |

### 5.7 DDoS Protection

| Layer | Protection |
|---|---|
| Network layer (L3/L4) | AWS Shield Standard (included with AWS) or Cloudflare DDos protection (automatic, always-on) |
| Application layer (L7) | AWS WAF or Cloudflare WAF: rate limiting, bot detection, known attack signature blocking |
| API rate limiting | Application-level rate limiting (see Section 3.6) as a secondary defense |
| CDN caching | Static assets served from CDN edge; reduces origin server load; **authenticated responses never cached** |
| Anomaly detection | Unusual traffic spikes trigger alerts (see Section 9) |

### 5.8 CDN and Caching Policy

| Content Type | Caching | Cache Duration |
|---|---|---|
| Static assets (JS, CSS, images, fonts) | Cached at CDN edge | Up to 1 year with content hash in filename |
| API responses (authenticated) | **Never cached** — `Cache-Control: no-store, no-cache` header required | N/A |
| API responses (unauthenticated public data) | Short-term cache permitted only for public calculator/rate data | Maximum 5 minutes |
| Loan documents, application data | **Never cached** | N/A |
| Admin panel | **Never cached**; CDN bypassed entirely | N/A |

---

## 6. PIPEDA Compliance

Canada's *Personal Information Protection and Electronic Documents Act* (PIPEDA, S.C. 2000, c. 5) applies to PaySpyre as a private-sector organization conducting commercial activities involving personal information. PIPEDA non-compliance carries penalties up to CAD $100,000 per violation.

PaySpyre's operations also intersect with provincial privacy legislation:
- **BC:** *Personal Information Protection Act* (PIPA BC) — substantially similar to PIPEDA; PIPEDA applies federally but PIPA BC guidance informs best practice
- **Alberta:** *Personal Information Protection Act* (PIPA AB) — substantially similar to PIPEDA

### Principle 1 — Accountability

**Requirement:** An organization must designate an individual to be accountable for its compliance with PIPEDA.

**PaySpyre implementation:**
- **Designated Privacy Officer:** [Insert name/title — recommended: CEO or COO at early stage; dedicated CPO as company scales]
- The Privacy Officer is responsible for: maintaining this security documentation, conducting privacy impact assessments for new features, handling data subject requests, managing breach response, and liaising with the Office of the Privacy Commissioner (OPC)
- Third-party processors (Flinks, Rotessa, Twilio, DocuSign) are governed by Data Processing Agreements that hold them to PIPEDA-equivalent standards. PaySpyre remains accountable for personal information even when processed by third parties
- Annual privacy compliance review conducted by Privacy Officer
- **Contact:** A privacy contact address (privacy@payspyre.com) is published on the PaySpyre website

### Principle 2 — Identifying Purposes

**Requirement:** The purposes for data collection must be identified before or at the time of collection.

**PaySpyre's identified purposes:**

| Purpose | Data Used | Legal Basis |
|---|---|---|
| Application processing and identity verification | Name, DOB, SIN, address, identity documents | Explicit consent at application; contractual necessity |
| Credit underwriting and loan decision | Bank transactions (via Flinks), income, expenses, employment | Explicit consent at application |
| Loan servicing and repayment | Bank account for PAD, contact info | Contractual necessity (loan agreement) |
| Collections (if applicable) | Contact info, loan balance, payment history | Contractual necessity; legal obligation |
| Regulatory compliance (FINTRAC KYC) | Identity verification records, transaction history | Legal obligation under PCMLTFA |
| Marketing communications | Email address, name | Separate, optional consent (CASL-compliant) |
| Product improvement and analytics | Anonymized/aggregated usage data | Legitimate interest (no PII in analytics) |

### Principle 3 — Consent

**Requirement:** The knowledge and consent of the individual are required for collection, use, or disclosure.

**PaySpyre implementation:**
- Explicit informed consent obtained at the start of every loan application, before any data is collected
- Consent is:
  - **Specific:** Identifies each category of data and purpose
  - **Informed:** Plain-language explanation of what is collected, why, and who it is shared with
  - **Freely given:** Applying for a loan is voluntary; no deception or coercion
  - **Documented:** Consent timestamp and version of consent language recorded with each application
- Patients are informed that their bank data is accessed via Flinks (read-only) and that repayments will be collected via Rotessa (PAD)
- Dental clinic (vendor) is informed that patient consent is required; vendors cannot initiate an application without patient initiation or explicit patient consent
- Consent can be withdrawn; withdrawal results in cessation of non-essential processing (marketing), but may not terminate an existing loan agreement
- Marketing consent is separate and optional — unchecked by default (no pre-ticked boxes)

### Principle 4 — Limiting Collection

**Requirement:** Collect only what is necessary for the identified purposes.

**PaySpyre implementation:**
- Data minimization policy enforced at product design stage (Privacy by Design)
- SIN collected only where required for identity verification; not collected for marketing or operational purposes beyond KYC
- Bank transaction history limited to what Flinks provides for underwriting (90–365 days); not stored indefinitely beyond underwriting
- Identity document images purged within 24 hours of successful KYC verification; only verified status and extracted data retained
- No collection of: health information, racial/ethnic origin, religious beliefs, sexual orientation, biometric data beyond what KYC OCR requires

### Principle 5 — Limiting Use, Disclosure, and Retention

**Requirement:** Personal information used only for stated purposes; retained only as long as needed.

**PaySpyre implementation:**
- Data used only for purposes identified in Principle 2; new purposes require fresh consent
- Data sharing: limited to:
  - Flinks (bank verification — read-only token)
  - Rotessa (PAD processing — bank account)
  - Twilio (SMS delivery — phone number)
  - DocuSign (document signing — name, email, loan document)
  - Credit bureau (if integrated — with consent)
  - FINTRAC / law enforcement (if legally required — no consent required by law)
- Dental clinic (vendor) receives only: application status, approved loan amount, payment confirmation. No access to patient's financial data, SIN, or identity documents
- Retention periods enforced by automated database purge jobs (see Section 2.3)
- Third-party DPAs prohibit processors from using PaySpyre customer data for their own purposes (e.g., Flinks cannot use a PaySpyre customer's transaction data for Flinks' own marketing)

### Principle 6 — Accuracy

**Requirement:** Personal information must be accurate, complete, and up-to-date.

**PaySpyre implementation:**
- Borrowers can update their contact information (address, phone, email) via the borrower portal at any time
- Employment and income information can be re-submitted if circumstances change
- Data correction requests handled within 30 days of receipt
- Stale data flagged: bank transaction data older than 90 days prompts re-verification if used for a new loan decision
- System-generated data (CashScore, application decisions) documented with methodology and input data, providing basis for challenging accuracy

### Principle 7 — Safeguards

**Requirement:** Personal information must be protected by security safeguards appropriate to the sensitivity.

**PaySpyre implementation:**
This entire document fulfills Principle 7. Key safeguards include:
- Technical: AES-256-GCM field-level encryption, TLS 1.3, JWT with 15-minute expiry, MFA for privileged users, RBAC
- Physical: Data hosted in AWS ca-central-1 certified data centers; no physical on-premises servers handling production data
- Organizational: Role-based access control, separation of duties, security training, privacy-by-design development practices, vendor due diligence
- Administrative: This security documentation, incident response plan (Section 8), annual audit, penetration testing plan (Section 12)

### Principle 8 — Openness

**Requirement:** Organizations must make their privacy policies and procedures available to individuals.

**PaySpyre implementation:**
- Privacy Policy published at `https://payspyre.com/privacy` — plain language, not just legal jargon
- Privacy Policy covers: what data is collected, why, who it is shared with, how it is protected, retention periods, individual rights, and how to contact the Privacy Officer
- Privacy Policy version-controlled; historical versions retained; users notified of material changes
- This Security Documentation summary is available on request

### Principle 9 — Individual Access

**Requirement:** Individuals have the right to know what personal information is held about them and to request corrections.

**PaySpyre implementation:**
- Borrowers can access their own data via the borrower portal (application history, loan terms, payment history, contact information)
- Data subject access requests (DSAR) handled by the Privacy Officer within 30 calendar days
- Response includes: all personal information held, how it is used, who it has been shared with (categories of recipients)
- Identity verification required before releasing data to requestor (to prevent unauthorized disclosure)
- Requests submitted to: privacy@payspyre.com
- Requests are logged and tracked to ensure timely response
- PaySpyre may refuse access only in limited circumstances permitted by PIPEDA (e.g., information would reveal third-party personal information that cannot be severed; legal proceeding confidentiality)

### Principle 10 — Challenging Compliance

**Requirement:** Individuals must be able to challenge an organization's compliance with PIPEDA.

**PaySpyre implementation:**
- Formal complaint process: Complaints submitted to privacy@payspyre.com
- Complaints acknowledged within 5 business days
- Investigation and response within 30 calendar days (complex matters may require extension with communication to complainant)
- Privacy Officer empowered to remediate complaints and update practices
- Complainants informed of their right to escalate unresolved complaints to the **Office of the Privacy Commissioner of Canada (OPC)**: www.priv.gc.ca
- Complaint records retained for 3 years

---

## 7. FINTRAC Security Requirements

The *Proceeds of Crime (Money Laundering) and Terrorist Financing Act* (PCMLTFA) imposes compliance obligations on Money Services Businesses (MSBs) in Canada. PaySpyre's legal counsel should advise on whether dental patient financing constitutes an MSB activity triggering registration. This section documents PaySpyre's approach assuming MSB registration may be required or as a proactive compliance measure.

### 7.1 MSB Registration

- If PaySpyre's lending activities constitute an MSB under PCMLTFA, registration with FINTRAC is required before commencing operations
- FINTRAC registration is free; registration completed through FINTRAC's online portal
- Compliance program must be submitted as part of registration
- **Compliance Officer:** A designated individual (recommended: Privacy Officer or CFO) must be named as FINTRAC Compliance Officer
- Registration must be renewed every 2 years

### 7.2 Know Your Customer (KYC) Requirements

FINTRAC requires identity verification for applicable transactions. PaySpyre's KYC process:

| Verification Method | What It Verifies | FINTRAC Acceptability |
|---|---|---|
| OCR on government-issued photo ID (driver's licence, passport) | Name, DOB, document number, expiry | Accepted as government-issued photo ID verification |
| Face matching (selfie vs. document photo) | Confirms the person is the same as the document holder | Strengthens confidence level; supports risk-based approach |
| Data consistency check | Cross-references stated name/DOB/address with bank account holder details via Flinks | Supporting corroboration method |

**Required identity information to capture and retain:**
- Full legal name
- Date of birth
- Address (residential)
- Government ID type, number, issuing jurisdiction, expiry date
- Date of verification
- Method of verification used

**Timing:** Identity verification completed before or at the time of the first transaction.

### 7.3 Transaction Monitoring

**Automated monitoring rules:**
- Loan amounts greater than $10,000 CAD: trigger enhanced due diligence review before approval
- Multiple applications from the same person within 30 days: trigger review
- Unusually high frequency of applications from a single dental clinic: trigger review
- Applications with inconsistencies between stated income and bank transaction data: flag for underwriter review

**Structuring detection:** Monitor for patterns that suggest intentional structuring to avoid reporting thresholds (e.g., multiple near-$10,000 applications).

### 7.4 Large Cash Transaction Reporting

- Large Cash Transaction Reports (LCTRs) required when a transaction involves $10,000 CAD or more in cash within a 24-hour period
- Dental patient financing typically involves electronic disbursements (direct to dental clinic) and PAD repayments — not cash
- PaySpyre must still implement the capability to file LCTRs if a cash component exists
- Reports submitted to FINTRAC electronically via FINTRAC Web Reporting System (FWR) or API

### 7.5 Suspicious Transaction Reports (STRs)

**When to file:** A Suspicious Transaction Report must be filed with FINTRAC *as soon as practicable* after reasonable grounds to suspect a transaction is related to money laundering, terrorist financing, or sanctions evasion.

**No monetary threshold** — suspicion is the trigger, not the transaction amount.

**Indicators specific to dental financing context:**
- Borrower requests loan far exceeding actual dental treatment costs
- Borrower insists on disbursement to a third party unrelated to the dental clinic
- Borrower provides multiple different identities or inconsistent information across applications
- Borrower willing to accept unfavorable loan terms without apparent reason
- IP address originates from OFAC/OSFI sanctioned jurisdiction
- Application pattern matches known fraud rings (multiple applications from same device/IP with different identities)
- Borrower asks questions about FINTRAC reporting obligations

**How to file:**
- STRs filed electronically via FINTRAC Web Reporting System (FWR) or FINTRAC API
- STR must include: transaction details, party information, grounds for suspicion (facts, context, indicators)
- STR submitted as soon as practicable — delays require documented justification
- **Tipping off is prohibited:** Do not inform the subject of the STR

### 7.6 Record-Keeping Requirements

| Record Type | Retention Period |
|---|---|
| Identity verification records | 5 years from date of last transaction |
| Transaction records | 5 years from date of transaction |
| STRs and supporting documentation | 5 years |
| LCTRs and supporting documentation | 5 years |
| AML compliance program documents | 5 years |
| Risk assessment | Keep current; retain versions for 5 years |

Records must be produced to FINTRAC within 30 days of a written request.

### 7.7 FINTRAC Compliance Program

A complete FINTRAC compliance program includes:

1. **Written compliance policies and procedures** — documented AML/CTF procedures, STR filing process, KYC process
2. **Designated Compliance Officer** — named individual responsible; authority to implement changes
3. **Ongoing employee training** — annual training for all staff who handle transactions or customer onboarding
4. **Risk assessment** — documented assessment of PaySpyre's inherent ML/TF risks (low for dental financing, but must be documented)
5. **Effectiveness review** — independent review of the compliance program every 2 years (can be done by an external AML consultant)

---

## 8. Incident Response Plan

### 8.1 Severity Classification

| Severity | Definition | Examples |
|---|---|---|
| **P1 — Critical** | Active data breach with confirmed or likely PII exposure; system compromise; unauthorized access to financial data; ransomware | Database exfiltration confirmed; production server compromised; SIN data exposed; Rotessa credentials stolen |
| **P2 — High** | Suspected (unconfirmed) breach; DDoS taking system fully offline; payment processing system failure; third-party integration breach (Flinks, Rotessa, Twilio, DocuSign) | Anomalous access pattern suggesting breach; complete service outage >30 min; Rotessa reports compromise |
| **P3 — Medium** | Vulnerability discovered (not actively exploited); suspicious activity on single account; non-critical system compromise; failed penetration attempt | Security researcher reports SQL injection vulnerability; single user account shows unauthorized access; failed brute-force detected |
| **P4 — Low** | Failed login attempts (within normal parameters); minor misconfiguration; non-critical bug with security implications | IP banned by fail2ban; misconfigured CORS header on non-sensitive endpoint; dependency with low CVE |

### 8.2 Response Time Objectives

| Severity | Initial Response | Containment | Eradication | Recovery |
|---|---|---|---|---|
| P1 | **15 minutes** (24/7) | 1 hour | 4 hours | 24 hours |
| P2 | **1 hour** (24/7) | 4 hours | 8 hours | 48 hours |
| P3 | **4 hours** (business hours) | Next business day | 5 business days | 10 business days |
| P4 | **Next business day** | 5 business days | 30 days | 30 days |

### 8.3 Incident Response Playbook

#### Phase 1: Preparation (Ongoing)

- Incident response contacts list maintained and reviewed quarterly
- Escalation tree: CTO → CEO → Legal Counsel → Privacy Officer
- Emergency contact list includes: Flinks security team, Rotessa support, Twilio support, DocuSign support, AWS support (Business/Enterprise tier)
- Secure out-of-band communication channel established (e.g., Signal group for incident response team) — assume main email/Slack may be compromised during a P1
- Tabletop exercise conducted annually

#### Phase 2: Detection & Analysis

**How incidents are detected:**

| Detection Method | Covers |
|---|---|
| Automated monitoring alerts (see Section 9) | Failed logins, anomalous traffic, API error spikes, unusual transaction patterns |
| User/customer reports | Account takeover, suspicious charges, unauthorized access |
| Third-party notification | Flinks, Rotessa, or other vendor notifies PaySpyre of breach on their end |
| Security researcher disclosure | Responsible disclosure program (security@payspyre.com) |
| Penetration test findings | Annual pen test (see Section 12) |
| Threat intelligence feeds | Known attacker IPs, CVE disclosures for dependencies |

**Initial analysis steps:**
1. Assign an Incident Commander (IC) — typically CTO for P1/P2
2. Create incident channel (out-of-band if needed)
3. Determine scope: what systems are affected? What data is at risk?
4. Determine timeline: when did the incident start? Is it ongoing?
5. Assess severity using Section 8.1 criteria
6. Preserve evidence: take snapshot of affected systems before any remediation

#### Phase 3: Containment

**P1 Immediate Containment Actions:**
1. Isolate affected system(s) from the network (revoke security group rules, disable NAT gateway if needed)
2. Revoke suspected compromised credentials (API keys, database passwords, JWT signing keys)
3. Force logout all active sessions (invalidate all refresh tokens in revocation list)
4. Block attacker IP(s) at Cloudflare / AWS WAF
5. Disable affected user accounts if account compromise is suspected
6. Preserve system state (snapshot EC2 instance, dump memory if live forensics needed) **before** patching/reimaging
7. Notify CEO and Legal Counsel immediately

**P2 Containment:**
1. Assess whether isolation is needed or increased monitoring sufficient
2. Revoke any credentials that may be affected
3. Notify internal team

**P3 Containment:**
1. Disable/patch the specific vulnerability
2. Review access logs for exploitation evidence
3. If account compromise: force logout and password reset for affected account

#### Phase 4: Eradication

1. Identify root cause: vulnerability, misconfiguration, compromised credential, social engineering
2. Remove malware, backdoors, or unauthorized accounts from affected systems
3. Patch the exploited vulnerability (or implement compensating control if patch unavailable)
4. Rotate all credentials that may have been exposed: database passwords, API keys, JWT signing keys
5. Reimage affected servers if compromise depth is uncertain
6. Deploy from clean, known-good application image
7. Verify no unauthorized code changes in Git repository

#### Phase 5: Recovery

1. Restore service from clean backup if data corruption occurred
2. Verify data integrity via hash comparison
3. Re-enable systems in controlled sequence with enhanced monitoring
4. Notify affected users if service interruption occurred
5. Confirm payment processing systems (Rotessa) are functioning correctly
6. Monitor closely for 72 hours post-recovery for recurrence

#### Phase 6: Post-Incident Review

Mandatory within **5 business days** for P1/P2, within **10 business days** for P3:

1. Root cause analysis (RCA) document: what happened, why, what was the initial access vector
2. Timeline reconstruction: when was the attacker first present, what did they access
3. Lessons learned: what controls failed, what controls worked
4. Remediation action items with owners and due dates
5. Update this Incident Response Plan if gaps identified
6. Preserve all evidence per retention schedule

### 8.4 PIPEDA Breach Notification Obligations

Under PIPEDA s.10.1 (*Breach of Security Safeguards Regulations*), breach notification is required when a breach creates a **real risk of significant harm (RROSH)** to individuals.

**RROSH assessment — factors to consider:**
- Sensitivity of the exposed data (SIN, bank account, loan history = high sensitivity; name alone = lower)
- Probability of misuse (was a malicious actor involved? Is the data already indexed/sold?)
- Whether data was encrypted (encrypted data that remains encrypted has lower RROSH)

**Significant harm includes:** bodily harm, humiliation, damage to reputation, loss of employment or business opportunities, financial loss, identity theft, negative credit effects, damage to property.

**Notification obligation timeline:**

| Action | Timing | Recipient |
|---|---|---|
| Report to OPC | **As soon as feasible** after determining RROSH exists | Office of the Privacy Commissioner of Canada |
| Notify affected individuals | **As soon as feasible** (same trigger as OPC notification) | Each affected individual |
| Record-keeping (all breaches, regardless of RROSH) | Within timeframe of incident | Internal breach register |

**All breaches must be recorded** for a minimum of **24 months**, regardless of whether they meet the RROSH threshold.

**OPC breach report must include:**
- Circumstances of the breach (including known cause)
- Date or approximate period the breach occurred
- Personal information involved
- Number (or approximate number) of affected individuals
- Steps taken or being taken to reduce risk / mitigate harm
- Contact information for follow-up

**OPC notification channel:** Submit via OPC's online breach report form at www.priv.gc.ca

---

### Template A: OPC Breach Notification Letter

```
To: Office of the Privacy Commissioner of Canada
    Via: priv.gc.ca online reporting portal

Date: [DATE]
Organization: PaySpyre Financial Inc.
Contact: [PRIVACY OFFICER NAME], Privacy Officer
Email: privacy@payspyre.com
Phone: [PHONE NUMBER]

SUBJECT: Mandatory Breach Report — Breach of Security Safeguards

1. DESCRIPTION OF THE BREACH
[Describe what occurred in plain language: unauthorized access to / 
disclosure of personal information of borrowers. Include the nature 
of the breach — hacking, accidental disclosure, lost device, etc.]

2. DATE OR PERIOD OF THE BREACH
[Date breach occurred, if known. If unknown, provide date breach was 
detected and estimated range.]

3. PERSONAL INFORMATION INVOLVED
[List categories of personal information involved: e.g., name, address, 
loan amounts, SIN (if applicable), bank account numbers (if applicable). 
State the approximate number of records affected.]

4. NUMBER OF INDIVIDUALS AFFECTED
Approximately [X] individuals.

5. REAL RISK OF SIGNIFICANT HARM ASSESSMENT
[State why PaySpyre has determined this breach poses a real risk of 
significant harm. Reference the sensitivity of the data and probability 
of misuse.]

6. STEPS TAKEN TO REDUCE RISK / MITIGATE HARM
[Describe containment and eradication steps taken: e.g., system 
isolated, credentials rotated, affected accounts locked, patches applied.]

7. STEPS TAKEN TO NOTIFY AFFECTED INDIVIDUALS
[Confirm individual notification has been or is being made per 
PIPEDA s.10.1(3).]

8. THIRD-PARTY NOTIFICATIONS
[List any other organizations or government institutions notified that 
may be able to reduce harm — e.g., Rotessa (payment processor), 
law enforcement if criminal activity suspected.]

Submitted by: [NAME]
Title: Privacy Officer
Date: [DATE]
```

---

### Template B: Individual Breach Notification

```
Subject: Important Security Notice Regarding Your PaySpyre Account

Dear [BORROWER NAME],

We are writing to inform you of a security incident that may have 
affected your personal information held by PaySpyre Financial.

WHAT HAPPENED
[Plain language description: e.g., On [DATE], we detected unauthorized 
access to a system containing loan application information.]

WHEN DID IT HAPPEN
The incident occurred on approximately [DATE] and was detected on [DATE].

WHAT INFORMATION WAS INVOLVED
The following types of your personal information may have been involved:
• [List specific categories applicable to this individual: name, 
  address, loan amount, etc.]
• [If SIN involved, state explicitly]
• [If bank account involved, state explicitly]

WHAT WE HAVE DONE
We have taken the following steps to address this incident and protect 
your information:
• [List containment actions taken]
• [List remediation actions taken]
• [Confirm report filed with OPC]

WHAT YOU CAN DO
We recommend you take the following steps to protect yourself:
• Monitor your bank accounts and credit card statements for any 
  unusual activity
• Place a fraud alert or credit freeze with Equifax Canada and 
  TransUnion Canada if your SIN was involved
• Be alert to phishing emails or calls claiming to be from PaySpyre 
  or your financial institution
• Report any suspected identity theft to the Canadian Anti-Fraud Centre 
  (1-888-495-8501 or www.antifraudcentre.ca)

[If SIN involved, add:]
• Contact Service Canada (1-800-206-7218) to report potential misuse 
  of your SIN
• Contact the Canada Revenue Agency if you are concerned about 
  fraudulent tax filings

FOR MORE INFORMATION
If you have questions about this notice, please contact our Privacy 
Officer at:

Email: privacy@payspyre.com
Phone: [PHONE NUMBER]
Mail: [ADDRESS]

You also have the right to file a complaint with the Office of the 
Privacy Commissioner of Canada at www.priv.gc.ca.

We sincerely apologize for this incident and the concern it may cause 
you. Protecting your personal information is our highest priority.

Sincerely,
[CEO NAME]
CEO, PaySpyre Financial
```

---

## 9. Monitoring & Alerting

### 9.1 Monitoring Coverage

| What | How | Alert Threshold | Severity |
|---|---|---|---|
| **Application uptime** | External HTTP health check (UptimeRobot, AWS Route 53 Health Checks, or Pingdom) | 2 consecutive failures (~2 min downtime) | P2 |
| **API response time** | Application Performance Monitoring (APM — Datadog, New Relic, or AWS X-Ray) | >2 second average over 5-minute window | P3 |
| **API error rate** | Application logs + APM | >1% of requests returning 5xx in 5 minutes | P2 if sustained |
| **Failed login attempts** | Application logs → log aggregation (CloudWatch Logs / ELK) | >10 failed logins from same IP in 5 minutes | P3 |
| **Authentication anomalies** | Application logs | Login from new country/device for ADMIN/UNDERWRITER account | P2 |
| **Database connection pool** | PostgreSQL monitoring (pg_stat_activity, PgBouncer stats) | >80% connection pool utilization | P3 |
| **Database query performance** | pg_stat_statements | Queries >5 seconds average | P4 |
| **Disk space** | OS metrics (CloudWatch, Prometheus node_exporter) | >85% utilization on any volume | P3 |
| **Memory utilization** | OS metrics | >90% for >10 minutes | P3 |
| **SSL certificate expiry** | Certificate monitoring (certbot auto-renewal check, Uptime Robot certificate monitor) | 30 days before expiry | P3 |
| **Payment processing failures** | Rotessa webhook monitoring — log all payment events | Any NSF or decline rate >10% in 1 hour | P2 |
| **Unusual transaction patterns** | Custom rules engine | Loan amount >$35,000; >5 applications/hour from same IP; multiple apps same device ID | P2 |
| **Bulk data export** | Application audit log | Any API call returning >1,000 records; any admin export operation | P1 investigate |
| **New admin account creation** | Application audit log | Any new ADMIN role assignment | P2 alert to CTO |
| **CRITICAL data field access** | Application audit log | Any direct read of SIN or bank account number | P3 review |
| **Container health** | Docker/ECS health checks | Container restart >3 times in 1 hour | P2 |
| **Dependency vulnerabilities** | Automated CVE scanning (Dependabot, Snyk) | Any critical/high severity CVE in dependencies | P2 (patch within 72 hours) |

### 9.2 Alerting Channels

| Alert Severity | Channel | Response |
|---|---|---|
| P1 | SMS + phone call to on-call engineer + CTO + CEO | Immediate; 24/7 |
| P2 | SMS to on-call engineer; Slack #security-alerts | Within 1 hour; 24/7 |
| P3 | Email to engineering team; Slack #security-alerts | Within 4 business hours |
| P4 | Ticket in issue tracker (Jira, Linear, or GitHub Issues) | Next business day |

### 9.3 Audit Log Requirements

All audit logs must be:
- **Tamper-evident:** Logs written to append-only storage; log integrity protected via cryptographic hash chain or WORM storage
- **Complete:** Every authenticated API call logged with: user ID, role, action, resource accessed, IP address, timestamp (UTC), response code
- **Searchable:** Indexed for rapid query during incident response (CloudWatch Insights, Elasticsearch)
- **Retained:** 3 years minimum
- **Monitored:** Log anomaly detection rules run continuously

---

## 10. Backup & Disaster Recovery

### 10.1 Backup Architecture

| Component | Backup Method | Frequency | Retention | Storage Location |
|---|---|---|---|---|
| PostgreSQL database | AWS RDS automated snapshots (or `pg_dump` to S3 with server-side encryption) | Daily automated; continuous WAL archiving | 30 days for snapshots; 7 days WAL | AWS ca-west-1 (separate region) |
| Application code | Git repository (GitHub) with branch protection; all releases tagged | On every commit | Indefinite (version history) | GitHub + local clone |
| Infrastructure configuration | Infrastructure as Code (Terraform / CloudFormation) in version-controlled repository | On every change | Indefinite | GitHub |
| Environment configuration / secrets | AWS Secrets Manager (automatic backups) | Continuous | Per AWS Secrets Manager retention policy | AWS ca-central-1 + replication |
| Application logs | AWS CloudWatch Logs export to S3 | Continuous + daily export | 3 years | AWS S3 (ca-central-1 + cross-region replication) |
| Identity documents (pre-verification) | Not backed up — ephemeral; purged within 24 hours of verification | N/A | 24 hours | Local ephemeral storage only |

### 10.2 Recovery Objectives

| Metric | Target | Basis |
|---|---|---|
| **Recovery Time Objective (RTO)** | 4 hours (system fully operational) | Financial services availability expectation; loan servicing continuity |
| **Recovery Point Objective (RPO)** | 1 hour maximum data loss | WAL archiving provides point-in-time recovery to within seconds; snapshot backup provides 24-hour fallback |

### 10.3 Point-in-Time Recovery

- PostgreSQL Write-Ahead Log (WAL) archiving enabled via AWS RDS or manual `archive_command`
- WAL archives stored encrypted in AWS S3 (separate bucket from application data)
- Enables restoration to any point within the last 30 days (to the second)
- Tested quarterly as part of DR testing

### 10.4 Disaster Recovery Procedure

**Trigger:** Primary region (ca-central-1) catastrophic failure, or P1 incident requiring complete system rebuild

**DR Runbook:**
1. Declare DR event (CTO authorization required)
2. Provision standby infrastructure in ca-west-1 from Terraform/IaC templates (target: 30 minutes)
3. Restore database from latest snapshot in ca-west-1 (target: 60 minutes for typical database size)
4. Apply WAL archives to minimize data loss (target: RPO < 1 hour)
5. Deploy application from Docker image (tagged release in container registry) in ca-west-1
6. Retrieve secrets from Secrets Manager (pre-replicated or restored from backup)
7. Update DNS to point to ca-west-1 endpoint via Route 53 (target: 5-minute TTL for rapid propagation)
8. Verify application health and data integrity
9. Notify users of recovery completion (status page update)
10. Conduct post-incident RCA

**Total target time: 4 hours (RTO)**

### 10.5 Geographic Redundancy

| Component | Primary | Secondary (DR) |
|---|---|---|
| Application server | AWS ca-central-1 (Montreal) | AWS ca-west-1 (Calgary) |
| Database | AWS RDS ca-central-1 | RDS snapshot restored in ca-west-1 |
| Database backups | AWS S3 ca-central-1 | Cross-region replication to ca-west-1 |
| Log storage | AWS CloudWatch / S3 ca-central-1 | S3 cross-region replication |
| Secrets | AWS Secrets Manager ca-central-1 | Replicated to ca-west-1 |

### 10.6 Disaster Recovery Testing

- **Quarterly DR test:** Simulate full recovery from backup in ca-west-1; restore database from snapshot; deploy application; verify all features function; document RTO achieved
- **Annual full DR exercise:** Simulate complete primary region failure; test full DR runbook including DNS failover; measure actual RTO/RPO; identify gaps
- All DR tests documented; results reviewed by CTO
- Failed DR tests trigger immediate remediation priority (P2)

---

## 11. Security Audit Checklist (Pre-Launch)

This checklist must be completed by the CTO and verified by an independent party (internal security review or external auditor) before PaySpyre processes any real borrower data.

### 11.1 Encryption & Data Protection

- [ ] All CRITICAL data fields (SIN, bank account numbers, Flinks tokens) confirmed encrypted at rest — verify with database query showing encrypted values, not plaintext
- [ ] AES-256-GCM confirmed as encryption standard for CRITICAL fields
- [ ] Encryption keys stored in AWS KMS or equivalent — **not** hardcoded in application code
- [ ] No plaintext credentials, API keys, or secrets in GitHub repository (`git log --all` checked)
- [ ] `.env` files excluded from version control (`.gitignore` confirmed)
- [ ] Database disk encryption enabled (AWS RDS encryption confirmed in console)
- [ ] Backup encryption enabled with separate backup key

### 11.2 Network & Transport Security

- [ ] TLS 1.3 active on all public endpoints — verify with SSL Labs test (ssllabs.com/ssltest/) — target grade A+
- [ ] TLS 1.0 and 1.1 disabled
- [ ] HSTS header configured: `Strict-Transport-Security: max-age=31536000; includeSubDomains; preload`
- [ ] CSP header configured and tested
- [ ] CORS configured correctly — only `payspyre.com` origins allowed, no wildcard (`*`)
- [ ] HTTP → HTTPS redirect active on all endpoints
- [ ] Database not accessible from the internet (confirm security group blocks port 5432 from 0.0.0.0/0)

### 11.3 Authentication & Access Control

- [ ] All default passwords changed (database, admin accounts, hosting control panels)
- [ ] MFA enforced for ADMIN and UNDERWRITER roles — tested with a test account
- [ ] JWT access token expiry confirmed at 15 minutes
- [ ] JWT refresh token rotation working — verify old token rejected after use
- [ ] Password hashing confirmed as bcrypt with work factor ≥ 12 — check source code
- [ ] Rate limiting active on login endpoint — test: 10+ rapid failed logins triggers lockout
- [ ] Rate limiting active on OTP/SMS endpoint — test: >3 SMS requests to same number in 10 minutes blocked
- [ ] Session timeout confirmed at 30 minutes inactivity — test manually

### 11.4 Application Security

- [ ] SQL injection testing passed — run sqlmap against all API endpoints; zero critical findings
- [ ] XSS testing passed — test all input fields with `<script>alert(1)</script>` and encoding variants
- [ ] CSRF protection active on all state-changing endpoints
- [ ] Input validation active — test with oversized inputs, special characters, null bytes
- [ ] File upload validation tested — verify malicious file types (`.php`, `.exe`) rejected
- [ ] All API endpoints require authentication except explicitly public ones — audit the endpoint list
- [ ] Broken Object Level Authorization (BOLA) tested — verify borrower A cannot access borrower B's data by changing ID in request

### 11.5 Third-Party Integrations

- [ ] Flinks API credentials confirmed as production credentials (not test/demo credentials)
- [ ] Flinks API credentials stored in Secrets Manager, not in `.env` file or code
- [ ] Rotessa webhook signature verification active — test with invalid signature (should return 401)
- [ ] Rotessa API key stored in Secrets Manager
- [ ] Twilio Auth Token stored in Secrets Manager
- [ ] DocuSign OAuth credentials stored in Secrets Manager
- [ ] All third-party integrations verified over TLS (no HTTP API calls)

### 11.6 Infrastructure

- [ ] Admin panel access IP-restricted — confirm public access returns 403
- [ ] SSH password authentication disabled (`PasswordAuthentication no` in `sshd_config`)
- [ ] fail2ban active and tested (simulate failed SSH attempts)
- [ ] Automatic security updates enabled on all servers
- [ ] Container vulnerability scan completed — zero critical/high CVEs
- [ ] Containers confirmed running as non-root user
- [ ] Cloudflare WAF or AWS WAF active with rules enabled

### 11.7 Monitoring & Alerting

- [ ] Monitoring system active and tested — trigger a deliberate test alert, confirm receipt
- [ ] Uptime monitoring configured and alerting to correct channel
- [ ] SSL certificate expiry monitoring active
- [ ] Log aggregation confirmed — logs flowing to CloudWatch or equivalent
- [ ] Audit logging active — confirm CRITICAL field access is logged

### 11.8 Backup & Recovery

- [ ] Backup system tested — successfully restore database from a backup to a test environment
- [ ] Backup encryption confirmed
- [ ] Backups stored in secondary region (ca-west-1)
- [ ] WAL archiving active and tested

### 11.9 Compliance & Documentation

- [ ] Privacy Policy published at payspyre.com/privacy
- [ ] Privacy Policy reviewed by legal counsel
- [ ] PIPEDA consent obtained at application start — confirm in application flow
- [ ] Breach response plan reviewed by CEO and Privacy Officer
- [ ] Privacy Officer designated and trained
- [ ] FINTRAC compliance obligations assessed by legal counsel; registration initiated if required
- [ ] All third-party DPAs signed (Flinks, Rotessa, Twilio, DocuSign)
- [ ] security@payspyre.com inbox active and monitored for responsible disclosure

---

## 12. Penetration Testing Plan

### 12.1 Recommended Testing Cadence

| Test Type | Recommended Frequency | Timing |
|---|---|---|
| Full application penetration test | Annually | Before launch; then annually |
| Web application / API testing | Annually + after major feature releases | With annual pentest; after any major architecture change |
| Infrastructure / external network test | Annually | With annual pentest |
| Social engineering assessment | Annually | With annual pentest |
| Vulnerability assessment (automated) | Quarterly | Continuous via Dependabot/Snyk; quarterly manual scan |

### 12.2 Scope of the Pre-Launch Penetration Test

**External Network Testing:**
- Confirm no services unintentionally exposed to the internet
- Test for open ports, misconfigured services, insecure administrative interfaces
- Test CDN/WAF bypass techniques

**Web Application Testing (OWASP Top 10 and beyond):**
- A01: Broken Access Control — verify RBAC, BOLA, BFLA (function-level authorization)
- A02: Cryptographic Failures — verify encryption implementation, TLS configuration
- A03: Injection — SQL injection on all 49+ API endpoints; NoSQL injection if applicable; command injection
- A04: Insecure Design — business logic flaws (e.g., manipulating loan amount, bypassing credit check)
- A05: Security Misconfiguration — headers, CORS, error messages (no stack traces exposed)
- A06: Vulnerable Components — dependency audit for known CVEs
- A07: Authentication Failures — session fixation, token leakage, brute force
- A08: Software & Data Integrity — dependency tampering, CI/CD pipeline security
- A09: Logging & Monitoring Failures — verify alerts trigger on attack attempts
- A10: SSRF — test for Server-Side Request Forgery via URL parameters

**API Testing:**
- All ~49+ API endpoints enumerated and tested
- Rate limiting tested and confirmed effective
- Authorization: verify each endpoint checks role correctly (no privilege escalation)
- JWT: test for algorithm confusion attacks (`none` algorithm, RS256 → HS256 confusion), expired token acceptance, signature bypass

**Mobile / Future:**
- If a mobile app is developed, include iOS and Android testing in subsequent pentest

**Social Engineering:**
- Phishing simulation targeting all team members
- Vishing (voice phishing) simulation if customer support phone line is active
- Test adherence to social engineering policies

### 12.3 Estimated Cost

A professional penetration test covering the scope above from a Canadian firm:

| Scope | Estimated Cost (CAD) |
|---|---|
| Web application + API testing only | $8,000 – $20,000 |
| Full scope (web app, API, infrastructure, social engineering) | $15,000 – $40,000 |
| Ongoing PTaaS (quarterly testing) | $30,000 – $60,000 per year |

*Costs vary based on number of endpoints, application complexity, and firm selected.*

### 12.4 Recommended Canadian Penetration Testing Firms

The following firms are specifically recommended for fintech/financial services penetration testing in Canada:

---

**1. Packetlabs (Mississauga, Ontario)**
- **Website:** packetlabs.net
- **Certifications:** CREST-accredited, SOC 2 Type II attested
- **Methodology:** 95% manual testing, zero false positives commitment, no outsourcing of testers
- **Fintech experience:** Financial sector clients including Fidelity Canada; explicit compliance coverage for PIPEDA, OSFI, PCI DSS v4.0
- **Reporting:** Attack-path narrative with business impact analysis — directly useful for investor due diligence and regulatory review
- **Pricing:** Projects typically $14,000 – $100,000+ CAD depending on scope
- **Best for:** PaySpyre's complete pre-launch scope (application + API + infrastructure + social engineering)

---

**2. Software Secured (Ottawa, Ontario)**
- **Website:** softwaresecured.com
- **Methodology:** Manual-first, engineering-focused; reports structured for developer remediation and compliance (CVSS + DREAD scoring)
- **Fintech experience:** Top-ranked fintech penetration testing firm; specializes in fraud simulation, payment logic abuse, and API authorization flaws specific to financial platforms
- **Delivery model:** Project-based or PTaaS (Penetration Testing as a Service) subscription for ongoing testing
- **Best for:** Fintech startups that need testing integrated into their development lifecycle; compliance-ready reporting for SOC 2, PCI DSS, ISO 27001
- **Note:** Ottawa-based firm; strong track record with growth-stage Canadian fintechs

---

**3. GoSecure (Montreal, Quebec)**
- **Website:** gosecure.net
- **Certifications:** CREST-affiliated; managed security services + penetration testing combined
- **Canadian headquarters:** Montreal — all data remains in Canadian jurisdiction
- **Fintech experience:** Strong Canadian mid-market and financial services track record; PIPEDA-aware testing and reporting
- **Delivery model:** Penetration testing as standalone service or bundled with Managed Detection & Response (Titan platform)
- **Best for:** PaySpyre if seeking ongoing managed security services alongside the penetration test; continuity advantage — the team monitoring your environment also tests it
- **Note:** Offers French-language services, beneficial for Quebec regulatory interactions

---

### 12.5 Pre-Test Preparation Checklist

Before engaging a penetration testing firm, PaySpyre must:
- [ ] Complete the Security Audit Checklist in Section 11 — fix obvious issues before paying for a pen test
- [ ] Prepare an asset inventory (all API endpoints, server IP addresses, domains in scope)
- [ ] Establish rules of engagement: testing window (hours), out-of-scope systems, emergency contact
- [ ] Ensure test environment is representative of production (or test on production with proper safeguards)
- [ ] Obtain written authorization documenting the scope (pen testers need this to avoid legal issues)
- [ ] Brief the monitoring team that legitimate pen test traffic will be occurring (to avoid false incident triggers)

### 12.6 Remediation and Re-Testing

- All **Critical** and **High** severity findings must be remediated before launch
- **Medium** severity findings: remediation plan within 30 days of report
- **Low/Informational** findings: tracked and addressed in subsequent sprint
- Re-test (free or included re-test from selected firm) confirms findings are fully remediated before go-live
- Penetration test report retained and made available to investors, regulators, and banking partners on request (under NDA)

---

## Appendix A: Key Contacts

| Role | Contact |
|---|---|
| Privacy Officer | [Name, email, phone] |
| FINTRAC Compliance Officer | [Name, email, phone] |
| CTO (Incident Commander) | [Name, email, phone] |
| CEO | [Name, email, phone] |
| Legal Counsel (Privacy) | [Law firm, name, emergency contact] |
| Flinks Security Team | security@flinks.com |
| Rotessa Support | support@rotessa.com |
| Twilio Security | [Twilio security disclosure page] |
| DocuSign Security | trust@docusign.com |
| OPC (breach reporting) | www.priv.gc.ca / 1-800-282-1376 |
| FINTRAC | fintrac-canafe.canada.ca / 1-866-346-8722 |
| Canadian Anti-Fraud Centre | 1-888-495-8501 |

---

## Appendix B: Regulatory Reference Index

| Regulation | Full Name | Administered By | PaySpyre's Obligations |
|---|---|---|---|
| PIPEDA | Personal Information Protection and Electronic Documents Act (S.C. 2000, c. 5) | Office of the Privacy Commissioner of Canada (OPC) | Privacy program, consent, breach notification, data subject rights |
| PIPA BC | Personal Information Protection Act (SBC 2003, c. 63) | Office of the Information & Privacy Commissioner of BC | Substantially similar to PIPEDA; applies to BC borrowers/clinics |
| PIPA AB | Personal Information Protection Act (SA 2003, c. P-6.5) | Office of the Information & Privacy Commissioner of Alberta | Substantially similar to PIPEDA; applies to AB borrowers/clinics |
| PCMLTFA | Proceeds of Crime (Money Laundering) and Terrorist Financing Act (S.C. 2000, c. 17) | FINTRAC | KYC, STR, LCTR, record-keeping (if MSB registration required) |
| CASL | Canada's Anti-Spam Legislation (S.C. 2010, c. 23) | CRTC | Marketing emails require express consent |
| Consumer Protection Acts | Various provincial consumer protection legislation | Provincial consumer protection offices | Loan disclosure, cooling-off periods, fee disclosure |

---

## Appendix C: Glossary

| Term | Definition |
|---|---|
| AES-256-GCM | Advanced Encryption Standard, 256-bit key, Galois/Counter Mode. Provides authenticated encryption — both confidentiality and integrity. NIST-recommended for financial data. |
| BOLA | Broken Object Level Authorization — API vulnerability where a user can access another user's data by changing an ID in the request. |
| CashScore | PaySpyre's proprietary creditworthiness score derived from Flinks bank transaction data. |
| FINTRAC | Financial Transactions and Reports Analysis Centre of Canada — Canada's financial intelligence unit. |
| HSTS | HTTP Strict Transport Security — browser directive that forces HTTPS for a specified period, preventing SSL stripping attacks. |
| KMS | Key Management Service — AWS's managed service for creating, storing, and controlling encryption keys using hardware security modules. |
| KYC | Know Your Customer — the process of verifying the identity of customers, required under PCMLTFA for MSBs. |
| MSB | Money Services Business — a category of financial service providers required to register with FINTRAC. |
| OPC | Office of the Privacy Commissioner of Canada — the federal regulator responsible for PIPEDA enforcement. |
| PAD | Pre-Authorized Debit — an authorization allowing a business to withdraw funds from a customer's bank account on scheduled dates. PaySpyre uses Rotessa for PAD processing. |
| PCMLTFA | Proceeds of Crime (Money Laundering) and Terrorist Financing Act — the Canadian law that created FINTRAC and established AML/CTF obligations. |
| PIPEDA | Personal Information Protection and Electronic Documents Act — Canada's primary federal private-sector privacy law. |
| RPO | Recovery Point Objective — the maximum acceptable data loss measured in time (e.g., RPO = 1 hour means at most 1 hour of data can be lost). |
| RROSH | Real Risk of Significant Harm — the PIPEDA standard that triggers breach notification obligations. |
| RTO | Recovery Time Objective — the maximum acceptable downtime (e.g., RTO = 4 hours means the system must be back online within 4 hours of a disaster). |
| SIN | Social Insurance Number — Canada's national identification number; high-sensitivity data under PIPEDA. |
| STR | Suspicious Transaction Report — a report filed with FINTRAC when there are reasonable grounds to suspect a transaction is related to money laundering or terrorist financing. |
| TLS 1.3 | Transport Layer Security 1.3 — the current gold standard for encrypting data in transit; TLS 1.0 and 1.1 are deprecated and insecure. |
| WAL | Write-Ahead Log — PostgreSQL's transaction log, used for point-in-time recovery and replication. |
| WAF | Web Application Firewall — a security layer that filters malicious HTTP traffic (SQL injection, XSS, etc.) before it reaches the application. |

---

*End of Document*

**Document Control:**  
Version 1.0 — Initial Release  
Next Review Date: [12 months from initial publication]  
Approved by: [CEO / CTO signature block]

**Sources and References:**
- PIPEDA 10 Fair Information Principles: https://www.priv.gc.ca/en/privacy-topics/privacy-laws-in-canada/the-personal-information-protection-and-electronic-documents-act-pipeda/p_principle/
- PIPEDA Breach Notification Requirements (Gowling WLG analysis): https://gowlingwlg.com/en/insights-resources/articles/2025/new-breach-assessment-tool-from-opc
- OPC RROSH Assessment Tool: https://www.priv.gc.ca/en/privacy-topics/business-privacy/breaches-and-safeguards/privacy-breaches-at-your-business/rrosh-tool/
- FINTRAC Suspicious Transaction Reporting: https://fintrac-canafe.canada.ca/guidance-directives/transaction-operation/str-dod/str-dod-eng
- FINTRAC MSB Registration: https://fintrac-canafe.canada.ca/msb-esm/msb-eng
- FINTRAC KYC Requirements (Trulioo): https://www.trulioo.com/blog/financial-services/fintrac-identification
- Flinks SOC 2 Type II and Security: https://www.flinks.com/data-safety
- Flinks Terms and Conditions (SOC 2 commitment): https://www.flinks.com/terms-and-conditions
- Rotessa Security Standards: https://rotessa.com/wp-content/uploads/2024/03/Rotessa-security-standards-doc-web.pdf
- DocuSign Certifications: https://www.docusign.com/trust/compliance/certifications
- Packetlabs Penetration Testing: https://www.packetlabs.net
- Software Secured Fintech Pentesting: https://www.softwaresecured.com/post/top-10-fintech-penetration-testing-provider
- GoSecure: https://www.gosecure.net
- PIPEDA vs GDPR Overview (F12.net): https://f12.net/blog/pipeda-vs-gdpr-uncovering-key-differences-in-2024/
- Breach Reporting Regulations Analysis (David Young Law): https://davidyounglaw.ca/compliance-bulletins/pipedas-breach-reporting-rules-in-force-november-1/
