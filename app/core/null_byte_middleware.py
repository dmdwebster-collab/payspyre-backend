"""Reject NUL bytes in JSON request bodies.

A NUL byte in a string field is never legitimate JSON input, but it reaches two
sinks that raise instead of returning a clean error:
  * psycopg2 - "A string literal cannot contain NUL (0x00) characters" on any query;
  * passlib/bcrypt - "bcrypt does not allow NULL bytes in password".
Both surface as a 500. This middleware turns that whole class into a clean 422.

Implemented as a pure-ASGI middleware (not BaseHTTPMiddleware) so it can buffer and
REPLAY the request body without breaking downstream body parsing. It only inspects
``application/json`` request bodies, so multipart/file uploads are never buffered.
"""
from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# A NUL reaches the body two ways: a raw 0x00 byte, or - far more common, since
# clients send valid JSON - a unicode escape that json decodes back into 0x00 before
# it hits the DB/bcrypt. The escape is the six bytes backslash-u-0-0-0-0 (json uses a
# lowercase "u"; we also guard the uppercase form defensively). Neither is legitimate.
_RAW_NUL = b"\x00"
_ESCAPED_NUL_LOWER = b"\\u0000"
_ESCAPED_NUL_UPPER = b"\\U0000"


class RejectNullBytesMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "").upper() not in _BODY_METHODS:
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        content_type = headers.get(b"content-type", b"")
        if b"application/json" not in content_type.lower():
            return await self.app(scope, receive, send)

        # Buffer the (small) JSON body so we can scan it and then replay it.
        chunks: list[bytes] = []
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
                more_body = message.get("more_body", False)
            elif message["type"] == "http.disconnect":
                more_body = False
        body = b"".join(chunks)

        if _RAW_NUL in body or _ESCAPED_NUL_LOWER in body or _ESCAPED_NUL_UPPER in body:
            await _reject(send)
            return

        # Replay the buffered body to the downstream app exactly once.
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay, send)


async def _reject(send: Send) -> None:
    payload = json.dumps(
        {"detail": "Request body contains an invalid NUL (0x00) byte."}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 422,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})
