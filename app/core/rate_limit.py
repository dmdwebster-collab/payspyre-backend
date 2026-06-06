import time
from collections import defaultdict
from typing import Optional

from fastapi import Request, HTTPException
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.status import HTTP_429_TOO_MANY_REQUESTS


class MultiKeyLimiter(Limiter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_requests = defaultdict(lambda: {"count": 0, "reset_time": time.time()})

    def _get_user_id(self, request: Request) -> Optional[str]:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            from jose import jwt
            from app.core.config import settings

            # Try the staff/admin secret first, then the patient/applicant secret.
            # Patient tokens are signed with PATIENT_JWT_SECRET (patient_auth_service),
            # so decoding them only with JWT_SECRET_KEY silently failed — all
            # authenticated patient traffic fell back to per-IP limiting and the
            # per-account throttle was dead (security review #5). Both token types
            # use JWT_ALGORITHM and carry `sub`.
            for secret in (settings.JWT_SECRET_KEY, settings.PATIENT_JWT_SECRET):
                try:
                    payload = jwt.decode(token, secret, algorithms=[settings.JWT_ALGORITHM])
                    return str(payload.get("sub"))
                except Exception:
                    continue
        return None

    def check_user_rate_limit(self, user_id: str, limit: int, window: int) -> bool:
        now = time.time()
        user_data = self.user_requests[user_id]

        if now - user_data["reset_time"] >= window:
            user_data["count"] = 0
            user_data["reset_time"] = now

        if user_data["count"] >= limit:
            return False

        user_data["count"] += 1
        return True


limiter = MultiKeyLimiter(key_func=get_remote_address)


def get_rate_limit_key(request: Request) -> str:
    user_id = limiter._get_user_id(request)
    if user_id:
        return f"user:{user_id}"
    return f"ip:{get_remote_address(request)}"


def check_endpoint_rate_limit(request: Request, endpoint_type: str):
    limits = {
        "auth": (5, 60),
        "read": (100, 60),
        "write": (30, 60),
        "webhook": (1000, 60),
    }

    limit, window = limits.get(endpoint_type, (50, 60))
    user_id = limiter._get_user_id(request)

    if user_id:
        if not limiter.check_user_rate_limit(user_id, limit, window):
            raise HTTPException(
                status_code=HTTP_429_TOO_MANY_REQUESTS,
                detail=f"User rate limit exceeded: {limit} requests per {window} seconds",
                headers={"Retry-After": str(window - (time.time() - limiter.user_requests[user_id]["reset_time"]))},
            )
    else:
        ip_key = f"ip:{get_remote_address(request)}"
        if not limiter.check_user_rate_limit(ip_key, limit, window):
            raise HTTPException(
                status_code=HTTP_429_TOO_MANY_REQUESTS,
                detail=f"IP rate limit exceeded: {limit} requests per {window} seconds",
                headers={"Retry-After": str(window - (time.time() - limiter.user_requests[ip_key]["reset_time"]))},
            )


def classify_endpoint(path: str, method: str) -> str:
    path_lower = path.lower()

    if "/webhooks" in path_lower:
        return "webhook"

    if "/auth" in path_lower or "/login" in path_lower or "/token" in path_lower:
        return "auth"

    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        return "write"

    return "read"