# PaySpyre Backend Security Hardening

## Implemented Security Features

### 1. Rate Limiting

**Location:** `app/core/rate_limit.py`

**Features:**
- Multi-layered rate limiting by IP and user ID
- Endpoint type classification (auth, read, write, webhook)
- Configurable limits per endpoint type
- User ID extraction from JWT tokens
- IP fallback for unauthenticated requests

**Limits:**
- Auth endpoints: 5 requests/60s
- Read endpoints: 100 requests/60s
- Write endpoints: 30 requests/60s
- Webhook endpoints: 1000 requests/60s

**Middleware:** Integrated in `app/main.py` with `SlowAPIMiddleware`

### 2. Security Headers

**Location:** `app/core/security_middleware.py`

**Implemented Headers:**
- `X-Content-Type-Options: nosniff` - Prevents MIME type sniffing
- `X-Frame-Options: DENY` - Prevents clickjacking
- `X-XSS-Protection: 1; mode=block` - XSS protection
- `Referrer-Policy: strict-origin-when-cross-origin` - Referrer control
- `Permissions-Policy: geolocation=(), microphone=(), camera=()` - Feature policy
- `Strict-Transport-Security: max-age=31536000; includeSubDomains` - HSTS
- `Content-Security-Policy: default-src 'self'` - CSP

### 3. CSRF Protection

**Location:** `app/core/security_middleware.py`

**Features:**
- CSRF token validation for state-changing requests
- Token generation endpoint: `GET /csrf-token`
- Exempt paths: `/health`, `/`, `/api/v1/kyc/webhooks`
- Secure, HttpOnly, SameSite=strict cookie

### 4. Request ID Tracking

**Location:** `app/core/security_middleware.py`

**Features:**
- Unique request ID generation (or from X-Request-ID header)
- Request ID logged in response header
- Audit trail capability

### 5. CORS Configuration

**Location:** `app/main.py`

**Configuration:**
- Strict origin whitelist from settings
- Explicit allowed methods: GET, POST, PUT, PATCH, DELETE, OPTIONS
- Allowed headers: Authorization, Content-Type, X-CSRF-Token, X-Request-ID, webhook signatures
- Exposed headers: X-Request-ID, Retry-After
- Max age: 600 seconds
- Credentials support enabled

### 6. Input Validation & Sanitization

**Location:** `app/core/validation.py`

**Functions:**
- `sanitize_string()` - Remove control characters, validate length
- `validate_email()` - Email format validation
- `validate_phone_number()` - Phone number normalization
- `validate_canadian_postal_code()` - CA postal code format
- `validate_ssn()` - SSN masking
- `validate_sin()` - Canadian SIN masking
- `validate_amount()` - Monetary amount validation
- `validate_percentage()` - Percentage validation
- `sanitize_html()` - Remove HTML tags and scripts
- `validate_url()` - URL scheme validation
- `check_sql_injection()` - SQL injection pattern detection
- `check_xss()` - XSS pattern detection
- `validate_input()` - Comprehensive input validation
- `QueryValidator` class - Database query sanitization

### 7. SQL Injection Prevention

**Implementation:**
- All database queries use SQLAlchemy ORM with parameterized queries
- No raw SQL string concatenation
- Input validation before database operations
- Query validation with `QueryValidator.validate_filter()`

### 8. XSS Prevention

**Implementation:**
- Security headers (CSP, X-XSS-Protection)
- Input sanitization (`sanitize_html()`)
- XSS pattern detection (`check_xss()`)
- HTML tag removal from user inputs

### 9. Configuration Management

**Location:** `app/core/config.py`

**Security Settings:**
```python
RATE_LIMIT_ENABLED: bool = True
RATE_LIMIT_AUTH_REQUESTS: int = 5
RATE_LIMIT_AUTH_WINDOW: int = 60
RATE_LIMIT_READ_REQUESTS: int = 100
RATE_LIMIT_READ_WINDOW: int = 60
RATE_LIMIT_WRITE_REQUESTS: int = 30
RATE_LIMIT_WRITE_WINDOW: int = 60
RATE_LIMIT_WEBHOOK_REQUESTS: int = 1000
RATE_LIMIT_WEBHOOK_WINDOW: int = 60
CSRF_ENABLED: bool = True
```

### 10. Dependencies

**Updated:** `pyproject.toml`

**Added:**
- `slowapi>=0.1.9` - Rate limiting

## Endpoint Rate Limits

### KYC Endpoints
- `POST /kyc/sessions` - 30/minute
- `GET /kyc/sessions/{session_id}` - 100/minute
- `POST /kyc/sessions/{session_id}/recreate` - 10/minute
- `POST /kyc/webhooks/didit` - 1000/minute
- `POST /kyc/webhooks/persona` - 1000/minute
- `POST /kyc/evaluate` - 20/minute
- `POST /kyc/co-borrowers/link` - 30/minute

### Loan Endpoints
- `POST /borrowers` - 10/minute
- `GET /borrowers/{borrower_id}` - 100/minute
- `POST /applications` - 10/minute
- `GET /applications/{application_id}` - 100/minute
- `GET /applications` - 100/minute
- `PATCH /applications/{application_id}` - 30/minute
- `POST /applications/{application_id}/submit` - 10/minute

### Underwriting Endpoints
- `POST /underwriting/evaluate` - 20/minute
- `POST /underwriting/manual-review` - 10/minute
- `GET /underwriting/status/{application_id}` - 100/minute
- `POST /underwriting/request-rereview/{application_id}` - 5/minute

### Funding Endpoints
- `POST /funding/applications/{application_id}/fund` - 10/minute
- `GET /funding/applications/{application_id}/funding-status` - 100/minute
- `GET /funding/payments/{payment_id}` - 100/minute
- `GET /funding/applications/{application_id}/payments` - 100/minute
- `POST /funding/payments` - 20/minute
- `GET /funding/statements/{statement_id}` - 100/minute
- `GET /funding/applications/{application_id}/statements` - 100/minute
- `POST /funding/payments/{payment_id}/refund` - 10/minute

### Vendor Endpoints
- `POST /vendors` - 10/minute
- `GET /vendors/{vendor_id}` - 100/minute
- `GET /vendors` - 100/minute
- `PATCH /vendors/{vendor_id}` - 30/minute
- `POST /vendors/{vendor_id}/onboarding` - 20/minute
- `GET /vendors/{vendor_id}/compliance` - 100/minute
- `POST /vendors/{vendor_id}/compliance/review` - 10/minute
- `GET /vendors/{vendor_id}/metrics` - 100/minute

## Usage

### Obtaining CSRF Token

```bash
curl -X GET http://localhost:8000/csrf-token \
  -c cookies.txt
```

### Making Authenticated Requests

```bash
curl -X POST http://localhost:8000/api/v1/loan/applications \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: <token-from-cookie>" \
  -H "Authorization: Bearer <jwt-token>" \
  -b cookies.txt \
  -d '{...}'
```

## Security Best Practices

1. **Environment Variables**: Never commit `.env` files. Use secrets management in production.
2. **JWT Secret**: Rotate regularly. Use strong random values.
3. **Rate Limits**: Adjust based on traffic patterns and requirements.
4. **CORS**: Only include production origins in production.
5. **Input Validation**: Always validate and sanitize user inputs.
6. **Parameterized Queries**: Never concatenate SQL strings.
7. **Logging**: Implement security event logging for audits.
8. **Monitoring**: Set up alerts for rate limit violations and security events.

## Testing Security

```bash
# Test rate limiting
for i in {1..150}; do
  curl http://localhost:8000/api/v1/loan/applications
done

# Test CSRF protection
curl -X POST http://localhost:8000/api/v1/loan/applications \
  -H "Content-Type: application/json" \
  -d '{}'

# Test input validation
curl -X POST http://localhost:8000/api/v1/loan/borrowers \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com<script>alert(1)</script>"}'
```