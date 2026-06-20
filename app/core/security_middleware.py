import secrets
from typing import Callable, Awaitable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = "default-src 'self'"

        return response


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get("X-Request-ID") or secrets.token_urlsafe(16)
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id

        return response


class CsrfMiddleware(BaseHTTPMiddleware):
    # CSRF protects cookie/ambient-auth flows. The exemptions below are surfaces
    # that legitimately can't carry a CSRF token:
    #  - inbound vendor webhooks (HMAC-verified instead) live under /api/webhooks/v1
    #    (the old "/api/v1/kyc/webhooks" prefix was stale — webhooks were never there).
    #  - the unauthenticated staff-auth endpoints and the bearer-JWT applicant auth flow.
    exempt_paths = {"/health", "/", "/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/auth/forgot-password", "/api/v1/auth/reset-password", "/api/v1/auth/verify-email"}
    exempt_prefixes = ("/api/webhooks/v1", "/api/applicant/v1/auth")

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path in self.exempt_paths or request.url.path.startswith(self.exempt_prefixes):
            return await call_next(request)

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            csrf_token = request.headers.get("X-CSRF-Token")
            if not csrf_token:
                return Response(
                    content='{"detail": "CSRF token missing"}',
                    status_code=403,
                    media_type="application/json",
                )

            session_csrf = request.cookies.get("csrf_token")
            if not session_csrf or not secrets.compare_digest(csrf_token, session_csrf):
                return Response(
                    content='{"detail": "Invalid CSRF token"}',
                    status_code=403,
                    media_type="application/json",
                )

        return await call_next(request)