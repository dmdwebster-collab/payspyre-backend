import logging
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

from app.core.config import settings
from app.core.logging import setup_logging, log_request, get_logger
from app.api.v1.api import api_router
from app.core.security_middleware import SecurityHeadersMiddleware, RequestIDMiddleware, CsrfMiddleware
from app.core.null_byte_middleware import RejectNullBytesMiddleware
from app.core.auth import RLSAuthMiddleware
from app.core.rate_limit import limiter, classify_endpoint, check_endpoint_rate_limit

# Setup structured logging
setup_logging()
logger = get_logger(__name__)

# Setup Sentry if DSN is configured
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
    logger.info("sentry_initialized", dsn_configured=True)

# Interactive docs + the OpenAPI schema expose the full internal API surface
# (admin endpoints, webhook routes, request shapes). Disable them in production so
# they aren't anonymous reconnaissance; keep them on elsewhere for development.
_docs_enabled = settings.ENVIRONMENT != "production"
app = FastAPI(
    title="PaySpyre API",
    description="PaySpyre Financial Backend API",
    version="0.1.0",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(SlowAPIMiddleware)
app.add_middleware(RLSAuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)

if settings.CSRF_ENABLED:
    app.add_middleware(CsrfMiddleware)

# Reject NUL bytes in JSON bodies (turns a 500 at the DB/bcrypt layer into a 422).
# Inside CORS so preflight is handled first; only inspects application/json bodies.
app.add_middleware(RejectNullBytesMiddleware)

# Shared with the early-return CORS helper below so a short-circuited response
# (e.g. a 429 from the rate limiter) advertises the same exposed headers.
_CORS_EXPOSE_HEADERS = ["X-Request-ID", "Retry-After", "X-Process-Time-ms"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-CSRF-Token",
        "X-Request-ID",
        "X-Didit-Signature",
        "X-Persona-Signature",
        "Idempotency-Key",
        "X-Alvero-Tenant",
        "X-Alvero-Timestamp",
        "X-Alvero-Signature",
    ],
    expose_headers=_CORS_EXPOSE_HEADERS,
    max_age=600,
)


def _early_return_cors_headers(request: Request) -> dict[str, str]:
    """CORS headers for a response that short-circuits CORSMiddleware.

    The rate limiter runs OUTSIDE CORSMiddleware in the stack, so its early 429
    returns before CORS can attach its headers — a cross-origin browser client
    would then see an opaque "Failed to fetch" instead of the 429. We reproduce
    the same simple-request decision CORSMiddleware makes (mirror an allowed
    Origin back, advertise credentials and exposed headers) so the policy is
    identical; no allow-list is widened here.
    """
    origin = request.headers.get("origin")
    if origin is None or origin not in settings.cors_origins_list:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Expose-Headers": ", ".join(_CORS_EXPOSE_HEADERS),
        "Vary": "Origin",
    }


@app.middleware("http")
async def endpoint_rate_limiter(request: Request, call_next):
    if not settings.RATE_LIMIT_ENABLED:
        return await call_next(request)

    if request.url.path in {"/", "/health", "/api/v1/health/db", "/docs", "/openapi.json", "/metrics"}:
        return await call_next(request)

    endpoint_type = classify_endpoint(request.url.path, request.method)
    # check_endpoint_rate_limit raises HTTPException(429) when the limit is hit.
    # An HTTPException raised inside HTTP middleware is NOT caught by FastAPI's
    # exception handlers, so it would surface to the client as a 500. Convert it
    # to a clean 429 JSON response here (with Retry-After) so rate-limited callers
    # get a correct, actionable status instead of an opaque server error.
    try:
        check_endpoint_rate_limit(request, endpoint_type)
    except HTTPException as exc:
        headers = {"Retry-After": str(settings.RATE_LIMIT_AUTH_WINDOW)}
        if exc.headers:
            headers.update(exc.headers)
        # This early return short-circuits CORSMiddleware (which sits inside us),
        # so carry the CORS headers ourselves — otherwise a browser sees an
        # opaque network error instead of the 429.
        headers.update(_early_return_cors_headers(request))
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=headers,
        )

    return await call_next(request)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    """Structured logging middleware for all requests."""
    return await log_request(request, call_next)


@app.get("/")
async def root():
    return {"service": "PaySpyre API", "status": "ok"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/csrf-token", status_code=status.HTTP_200_OK)
async def get_csrf_token():
    import secrets
    from fastapi import Response

    token = secrets.token_urlsafe(32)
    response = Response(status_code=status.HTTP_200_OK)
    response.set_cookie(
        key="csrf_token",
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=3600,
    )
    response.headers["X-CSRF-Token"] = token
    return response


@app.on_event("startup")
async def startup_event():
    from app.db.base import engine

    logger.info("application_startup", environment=settings.ENVIRONMENT)

    # Log database info
    try:
        with engine.connect() as conn:
            if engine.dialect.name == "postgresql":
                result = conn.execute("SELECT VERSION() as version")
            else:
                result = conn.execute("SELECT sqlite_version() as version")
            version = result.fetchone()[0]
            logger.info("database_connected", dialect=engine.dialect.name, version=version)
    except Exception as e:
        logger.error("database_connection_error", error=str(e))

    # The V1 NotificationQueue polling thread was removed in P7.4c (its
    # `notifications` table never existed in V2 — the loop only produced
    # `relation "notifications" does not exist` log spam). Magic-link sends
    # travel through ``RealNotificationDispatcher`` (P7.4) directly from the
    # applicant endpoints; no background poller is needed.


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("application_shutdown")


app.include_router(api_router, prefix="/api/v1")

# Applicant-facing API (patient magic-link auth) — P6.5. Separate surface/prefix
# from the internal /api/v1 routers.
from app.api.applicant.v1.router import applicant_router  # noqa: E402

app.include_router(applicant_router, prefix="/api/applicant/v1")

# Clinic/practice-facing API — P9.x. Separate surface/prefix.
from app.api.clinic.v1.router import clinic_router  # noqa: E402

app.include_router(clinic_router, prefix="/api/clinic/v1")

# Vendor webhook API (HMAC-authenticated) — P6.6. Separate surface/prefix.
from app.api.webhooks.v1.router import webhook_router  # noqa: E402

app.include_router(webhook_router, prefix="/api/webhooks/v1")

# Prometheus §11 KPI metrics — H-8. Scraped at /metrics (no prefix).
from app.api.metrics import metrics_router  # noqa: E402

app.include_router(metrics_router)

# Alvero server-to-server API (HMAC-authenticated).
from app.api.alvero import router as alvero_router  # noqa: E402

app.include_router(alvero_router)
