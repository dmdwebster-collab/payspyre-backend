"""
Structured logging configuration for PaySpyre backend.

Provides JSON-formatted logs with request context for production observability.
"""
import logging
import sys
from datetime import datetime
from typing import Any
from pythonjsonlogger import jsonlogger

import structlog
from fastapi import Request
from sqlalchemy.orm import Session

# Configure structlog
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer() if __debug__ else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO if not __debug__ else logging.DEBUG),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

# Standard Python logging setup for non-structlog code
def setup_logging() -> None:
    """Configure standard Python logging with JSON formatter."""
    handler = logging.StreamHandler(sys.stdout)

    if __debug__:
        # Dev: readable format
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    else:
        # Prod: JSON format
        formatter = jsonlogger.JsonFormatter(
            '%(asctime)s %(name)s %(levelname)s %(message)s',
            timestamp=True
        )

    handler.setFormatter(formatter)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO if not __debug__ else logging.DEBUG)
    root_logger.addHandler(handler)

    # Configure uvicorn loggers
    for logger_name in ['uvicorn', 'uvicorn.access', 'uvicorn.error']:
        logger = logging.getLogger(logger_name)
        logger.handlers = [handler]
        logger.propagate = False


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)


# Request logging middleware context
async def log_request(request: Request, call_next):
    """Log all API requests with duration and context."""
    import time

    start_time = time.time()
    logger = get_logger("api.request")

    # Extract request context
    request_id = request.headers.get("X-Request-ID", "unknown")
    user_agent = request.headers.get("User-Agent", "unknown")

    # Log request
    logger.info(
        "request_started",
        method=request.method,
        path=request.url.path,
        query_params=str(request.url.query) if request.url.query else None,
        request_id=request_id,
        user_agent=user_agent,
        client_host=request.client.host if request.client else None,
    )

    # Process request
    try:
        response = await call_next(request)
        duration_ms = (time.time() - start_time) * 1000

        # Log response
        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
            request_id=request_id,
        )

        # Add duration header
        response.headers["X-Process-Time-ms"] = str(round(duration_ms, 2))
        return response

    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        logger.error(
            "request_failed",
            method=request.method,
            path=request.url.path,
            error=str(e),
            error_type=type(e).__name__,
            duration_ms=round(duration_ms, 2),
            request_id=request_id,
        )
        raise


# Database query logging
class QueryLogger:
    """Context manager for logging slow database queries."""

    def __init__(self, operation: str, threshold_ms: float = 1000.0):
        self.operation = operation
        self.threshold_ms = threshold_ms
        self.logger = get_logger("db.query")
        self.start_time = None

    def __enter__(self):
        import time
        self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        import time
        duration_ms = (time.time() - self.start_time) * 1000

        if duration_ms > self.threshold_ms:
            self.logger.warning(
                "slow_query",
                operation=self.operation,
                duration_ms=round(duration_ms, 2),
                threshold_ms=self.threshold_ms,
            )

        return False


# External API call logging
def log_external_call(
    service: str,
    endpoint: str,
    method: str = "POST",
    status_code: int | None = None,
    duration_ms: float | None = None,
    error: str | None = None,
) -> None:
    """Log external API calls (Stripe, Didit, Persona, etc.)."""
    logger = get_logger("external_api")

    log_data = {
        "service": service,
        "endpoint": endpoint,
        "method": method,
    }

    if status_code:
        log_data["status_code"] = status_code

    if duration_ms is not None:
        log_data["duration_ms"] = round(duration_ms, 2)

    if error:
        log_data["error"] = error
        logger.error("external_api_error", **log_data)
    else:
        logger.info("external_api_call", **log_data)