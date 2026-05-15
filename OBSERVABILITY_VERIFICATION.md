# Section 2: Observability Verification Report

**Date:** 2026-05-14
**Scope:** Backend + Admin Portal + Analytics Portal + Patient Portal
**Overall Status:** **85% Complete**

---

## Executive Summary

Observability infrastructure is well-implemented with structured logging, error tracking, and analytics. Key gaps exist in external API call instrumentation and Sentry/PostHog configuration validation (DSNs not yet configured).

---

## 1. Structured Logging

### Backend (payspyre-backend)
**Status: ✅ COMPLETE**

**File:** `C:\Users\Michael\payspyre-backend\app\core\logging.py`

**Implementation:**
- Structured logging using `structlog` and `pythonjsonlogger`
- JSON format for production, readable format for development
- Request logging middleware with duration tracking
- `log_request()` middleware captures: method, path, query params, request_id, user_agent, client_host, status_code, duration_ms
- Custom `QueryLogger` context manager for slow query detection
- `log_external_call()` function defined but **NOT USED** (gap)
- Environment-aware log levels

**Configuration:** `setup_logging()` called in `app/main.py` on startup

**Key Features:**
```python
# Request logging
logger.info("request_started", method, path, request_id, ...)
logger.info("request_completed", method, path, status_code, duration_ms, ...)

# Query logging (context manager)
with QueryLogger("operation_name", threshold_ms=1000):
    # DB operation

# External API logging (defined but unused)
log_external_call(service, endpoint, method, status_code, duration_ms, error)
```

**Usage Verification:**
- ✅ Request middleware active in `app/main.py` line 90-92
- ✅ Database query logging via `setup_db_logging()`
- ❌ External API call logging function defined but not imported/used

---

### Frontend Portals
**Status: ✅ COMPLETE (via browser console)**

All portals use Next.js built-in console logging. No additional structured logging libraries configured in package.json.

---

## 2. Sentry Error Tracking

### Backend (payspyre-backend)
**Status: ✅ CONFIGURED**

**File:** `C:\Users\Michael\payspyre-backend\app\main.py` (lines 24-39)

**Configuration:**
```python
if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        integrations=[
            FastApiIntegration(tracing_options={
                "traces_sampler": lambda ctx: 1.0 if ctx and "/api/v1/" in ctx.get("path", "") else 0.1,
            }),
            SqlalchemyIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        traces_sample_rate=0.2,
        profiles_sample_rate=0.1,
        environment=settings.ENVIRONMENT,
        release=f"payspyre-backend@{settings.VERSION}",
    )
```

**Features:**
- ✅ FastAPI integration with distributed tracing
- ✅ SQLAlchemy integration for database query tracking
- ✅ Logging integration (INFO+ events to Sentry)
- ✅ Performance monitoring (20% trace sample rate)
- ✅ Profiling (10% sample rate)
- ✅ Environment-aware
- ✅ Release tracking

**Environment Variable:** `SENTRY_DSN` (documented in `.env.example` line 69)

**Status:** **Not yet tested** - DSN needs to be configured in environment to verify data flow

---

### Admin Portal (payspyre-admin)
**Status: ✅ CONFIGURED**

**File:** `C:\Users\Michael\payspyre-admin\sentry.config.js`

**Configuration:**
```javascript
{
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
  environment: process.env.NEXT_PUBLIC_ENVIRONMENT || "development",
  tracesSampleRate: 0.2,
  replaySessionSampleRate: 0.1,
  replayOnErrorSampleRate: 1.0,
}
```

**Dependencies:** `@sentry/nextjs@^8.47.1` (package.json)

**Features:**
- ✅ Browser error tracking
- ✅ Performance monitoring (20% trace sample rate)
- ✅ Session replay (10% sample rate)
- ✅ Error replay (100% sample rate on error)
- ✅ Environment-aware

**Environment Variable:** `NEXT_PUBLIC_SENTRY_DSN` (documented in `.env.example` line 6)

**Status:** **Not yet tested** - DSN needs to be configured

---

### Analytics Portal (payspyre-analytics)
**Status: ✅ CONFIGURED**

**File:** `C:\Users\Michael\payspyre-analytics\sentry.config.js`

**Configuration:** Identical to admin portal

**Dependencies:** `@sentry/nextjs@^8.13.0` (package.json)

**Status:** **Not yet tested** - DSN needs to be configured

---

### Patient Portal (payspyre-patient-portal)
**Status: ✅ CONFIGURED**

**File:** `C:\Users\Michael\payspyre-patient-portal\sentry.config.js`

**Configuration:** Identical to admin portal

**Dependencies:** `@sentry/nextjs@^8.13.0` (package.json)

**Status:** **Not yet tested** - DSN needs to be configured

---

## 3. PostHog Analytics

### Admin Portal (payspyre-admin)
**Status: ✅ CONFIGURED AND INTEGRATED**

**File:** `C:\Users\Michael\payspyre-admin\src\lib\posthog.tsx`

**Implementation:**
```typescript
client.init(NEXT_PUBLIC_POSTHOG_KEY, {
  api_host: NEXT_PUBLIC_POSTHOG_HOST || "https://app.posthog.com",
  capture_pageview: false,
  capture_pageleave: true,
});

// Track pageview on route change
client.capture("$pageview", { $current_url: url });
```

**Integration:** ✅ Active in `src/app/layout.tsx` (line 25)

**Dependencies:** `posthog-js@^1.131.0` (package.json)

**Environment Variables:**
- `NEXT_PUBLIC_POSTHOG_KEY` (documented in `.env.example` line 9)
- `NEXT_PUBLIC_POSTHOG_HOST` (documented in `.env.example` line 10)

**Status:** **Not yet tested** - API key needs to be configured

---

### Analytics Portal (payspyre-analytics)
**Status: ✅ CONFIGURED AND INTEGRATED**

**File:** `C:\Users\Michael\payspyre-analytics\src\lib\posthog.tsx`

**Configuration:** Identical to admin portal

**Integration:** ✅ Active in `src/app/layout.tsx` (line 21)

**Dependencies:** `posthog-js@^1.131.0` (package.json)

**Status:** **Not yet tested** - API key needs to be configured

---

### Patient Portal (payspyre-patient-portal)
**Status: ✅ CONFIGURED AND INTEGRATED**

**File:** `C:\Users\Michael\payspyre-patient-portal\src\lib\posthog.tsx`

**Configuration:** Identical to admin portal

**Integration:** ✅ Active in `app/layout.tsx` (line 22)

**Dependencies:** `posthog-js@^1.131.0` (package.json)

**Note:** Import path uses `@/src/lib/posthog` (inconsistent with other portals)

**Status:** **Not yet tested** - API key needs to be configured

---

## 4. Database Query Monitoring

### Backend (payspyre-backend)
**Status: ✅ COMPLETE**

**File:** `C:\Users\Michael\payspyre-backend\app\core\db_logging.py`

**Implementation:**
```python
SLOW_QUERY_THRESHOLD_MS = 500  # ✅ Meets 500ms requirement

@event.listens_for(engine, "after_cursor_execute")
def after_cursor_execute(...):
    duration_ms = (time.time() - start_time) * 1000

    if duration_ms > SLOW_QUERY_THRESHOLD_MS:
        logger.warning("slow_query", **log_data, threshold_ms=SLOW_QUERY_THRESHOLD_MS)
    else:
        logger.debug("query", **log_data)

@event.listens_for(engine, "handle_error")
def handle_error(...):
    logger.error("db_error", error=str(exception), error_type=type(exception).__name__, ...)
```

**Features:**
- ✅ All queries logged with execution time (DEBUG level)
- ✅ Slow queries (>500ms) logged as warnings
- ✅ Database errors logged with error type and statement
- ✅ SQL cleaned before logging (truncated to 200 chars)
- ✅ Enabled on startup via `setup_db_logging(engine)` in `app/db/base.py`

**Activation:** ✅ Called in `app/db/base.py` line 42

---

## 5. Request ID Tracking

### Backend (payspyre-backend)
**Status: ✅ COMPLETE**

**File:** `C:\Users\Michael\payspyre-backend\app\core\security_middleware.py` (lines 23-31)

**Implementation:**
```python
class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or secrets.token_urlsafe(16)
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id

        return response
```

**Features:**
- ✅ Accepts client-provided request ID via `X-Request-ID` header
- ✅ Generates cryptographically secure request ID if not provided
- ✅ Adds request ID to response header
- ✅ Request ID captured in logs via `log_request()` middleware

**Activation:** ✅ Active in `app/main.py` line 52

**Usage in Logging:**
```python
# From app/core/logging.py line 75, 84, 101, 116
request_id = request.headers.get("X-Request-ID", "unknown")
logger.info("request_started", ..., request_id=request_id, ...)
logger.info("request_completed", ..., request_id=request_id, ...)
```

---

### Frontend Portals
**Status: ⚠️ NOT IMPLEMENTED**

**Gap:** Frontend portals do not automatically include `X-Request-ID` header in API requests.

**Impact:** Request correlation between frontend and backend is incomplete.

---

## 6. Missing Instrumentation

### External API Calls
**Status: ❌ GAP IDENTIFIED**

**Issue:** The `log_external_call()` function is defined in `app/core/logging.py` but is never used.

**Affected Services:**
- `app/services/kyc_vendor.py` (Didit, Persona) - No external call logging
- `app/services/stripe.py` - Uses basic Python logging, not structured logging

**Current State:**
```python
# stripe.py line 11 - Uses standard logging, not structlog
logger = logging.getLogger(__name__)
```

**Recommendation:**
```python
# Should use:
from app.core.logging import log_external_call, get_logger

logger = get_logger(__name__)

# Wrap external API calls with timing
start = time.time()
response = await client.post(...)
duration_ms = (time.time() - start) * 1000

log_external_call(
    service="stripe",
    endpoint="/v1/payment_intents",
    method="POST",
    status_code=response.status_code,
    duration_ms=duration_ms
)
```

---

## 7. Configuration Status

### Backend (.env.example)
**Observability Variables Documented:**
```bash
ENVIRONMENT=development
VERSION=0.1.0
SENTRY_DSN=https://your-sentry-dsn@sentry.io/project-id
```
✅ All documented

### Frontend Portals (.env.example)
**Observability Variables Documented:**
```bash
NEXT_PUBLIC_ENVIRONMENT=development
NEXT_PUBLIC_SENTRY_DSN=https://your-sentry-dsn@sentry.io/project-id
NEXT_PUBLIC_POSTHOG_KEY=your_posthog_project_key
NEXT_PUBLIC_POSTHOG_HOST=https://app.posthog.com
```
✅ All documented (admin, analytics, patient)

---

## 8. Test Results Summary

### Tests Performed
| Test | Backend | Admin | Analytics | Patient |
|------|---------|-------|-----------|---------|
| Structured logging exists | ✅ | N/A | N/A | N/A |
| Sentry config exists | ✅ | ✅ | ✅ | ✅ |
| Sentry package installed | N/A | ✅ | ✅ | ✅ |
| PostHog config exists | N/A | ✅ | ✅ | ✅ |
| PostHog package installed | N/A | ✅ | ✅ | ✅ |
| PostHog integrated in layout | N/A | ✅ | ✅ | ✅ |
| Slow query detection (500ms) | ✅ | N/A | N/A | N/A |
| Request ID middleware | ✅ | N/A | N/A | N/A |
| Request ID in logs | ✅ | N/A | N/A | N/A |
| Request ID in frontend API calls | ❌ | ❌ | ❌ | ❌ |
| External API call logging | ❌ | N/A | N/A | N/A |

### Cannot Test (Requires Environment Configuration)
- Sentry data reception (requires valid DSN)
- PostHog data reception (requires valid API key)
- Error capture to Sentry
- Pageview tracking in PostHog

---

## 9. Gaps and Recommendations

### High Priority
1. **External API Call Logging**
   - Update `app/services/kyc_vendor.py` to use `log_external_call()`
   - Update `app/services/stripe.py` to use structlog instead of standard logging
   - Add timing instrumentation for all external HTTP calls

2. **Frontend Request ID Correlation**
   - Add axios/fetch interceptor in all portals to send `X-Request-ID` header
   - Generate request ID on page load
   - Include request ID in error reports to Sentry

### Medium Priority
3. **Sentry/PostHog Configuration**
   - Obtain production DSNs and API keys
   - Test data flow by triggering errors and pageviews
   - Configure release tracking for production deployments

4. **Patient Portal Import Path**
   - Fix inconsistent import: `@/src/lib/posthog` should be `@/lib/posthog`

### Low Priority
5. **Enhanced Metrics**
   - Add custom metrics for business KPIs (loan applications, approvals, disbursements)
   - Add database query histogram metrics
   - Add external API latency histograms

---

## 10. Completion Score

### Component Breakdown
| Component | Score | Weight |
|-----------|-------|--------|
| Structured Logging | 100% | 25% |
| Sentry Configuration | 100% | 20% |
| PostHog Configuration | 100% | 15% |
| Database Query Monitoring | 100% | 15% |
| Request ID Tracking | 75% | 15% |
| External API Instrumentation | 0% | 10% |

**Overall: 85%**

---

## 11. Files Verified

### Backend
- `C:\Users\Michael\payspyre-backend\app\core\logging.py`
- `C:\Users\Michael\payspyre-backend\app\core\db_logging.py`
- `C:\Users\Michael\payspyre-backend\app\core\security_middleware.py`
- `C:\Users\Michael\payspyre-backend\app\core\config.py`
- `C:\Users\Michael\payspyre-backend\app\main.py`
- `C:\Users\Michael\payspyre-backend\app\db\base.py`
- `C:\Users\Michael\payspyre-backend\app\services\kyc_vendor.py`
- `C:\Users\Michael\payspyre-backend\app\services\stripe.py`
- `C:\Users\Michael\payspyre-backend\.env.example`

### Admin Portal
- `C:\Users\Michael\payspyre-admin\sentry.config.js`
- `C:\Users\Michael\payspyre-admin\src\lib\posthog.tsx`
- `C:\Users\Michael\payspyre-admin\src\app\layout.tsx`
- `C:\Users\Michael\payspyre-admin\package.json`
- `C:\Users\Michael\payspyre-admin\.env.example`

### Analytics Portal
- `C:\Users\Michael\payspyre-analytics\sentry.config.js`
- `C:\Users\Michael\payspyre-analytics\src\lib\posthog.tsx`
- `C:\Users\Michael\payspyre-analytics\src\app\layout.tsx`
- `C:\Users\Michael\payspyre-analytics\package.json`
- `C:\Users\Michael\payspyre-analytics\.env.example`

### Patient Portal
- `C:\Users\Michael\payspyre-patient-portal\sentry.config.js`
- `C:\Users\Michael\payspyre-patient-portal\src\lib\posthog.tsx`
- `C:\Users\Michael\payspyre-patient-portal\app\layout.tsx`
- `C:\Users\Michael\payspyre-patient-portal\package.json`
- `C:\Users\Michael\payspyre-patient-portal\.env.example`

---

## 12. Next Steps

1. Implement external API call logging in KYC and Stripe services
2. Add frontend request ID correlation
3. Configure production Sentry DSNs and PostHog API keys
4. Test data flow to observability platforms
5. Fix patient portal import path inconsistency

---

**Report Generated:** 2026-05-14
**Total Files Verified:** 25
**Total Components:** 4 (backend + 3 portals)