import logging
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
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

app = FastAPI(
    title="PaySpyre API",
    description="PaySpyre Financial Backend API",
    version="0.1.0",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(SlowAPIMiddleware)
app.add_middleware(RLSAuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)

if settings.CSRF_ENABLED:
    app.add_middleware(CsrfMiddleware)

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
    ],
    expose_headers=["X-Request-ID", "Retry-After", "X-Process-Time-ms"],
    max_age=600,
)


@app.middleware("http")
async def endpoint_rate_limiter(request: Request, call_next):
    if not settings.RATE_LIMIT_ENABLED:
        return await call_next(request)

    if request.url.path in {"/", "/health", "/api/v1/health/db", "/docs", "/openapi.json"}:
        return await call_next(request)

    endpoint_type = classify_endpoint(request.url.path, request.method)
    check_endpoint_rate_limit(request, endpoint_type)

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
    from app.services.notifications import NotificationQueue
    from app.db.base import SessionLocal, engine
    import asyncio

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

    async def process_notification_queue():
        while True:
            try:
                db = SessionLocal()
                queue = NotificationQueue(db)
                await queue.process_queue()
                await queue.retry_pending_webhooks()
                db.close()
                await asyncio.sleep(settings.NOTIFICATION_QUEUE_PROCESSING_INTERVAL)
            except Exception as e:
                logger.error("notification_queue_error", error=str(e))
                await asyncio.sleep(settings.NOTIFICATION_QUEUE_PROCESSING_INTERVAL)

    import threading
    queue_thread = threading.Thread(
        target=lambda: asyncio.run(process_notification_queue()),
        daemon=True,
    )
    queue_thread.start()
    logger.info("notification_queue_started")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("application_shutdown")


app.include_router(api_router, prefix="/api/v1")

# Applicant-facing API (patient magic-link auth) — P6.5. Separate surface/prefix
# from the internal /api/v1 routers.
from app.api.applicant.v1.router import applicant_router  # noqa: E402

app.include_router(applicant_router, prefix="/api/applicant/v1")
