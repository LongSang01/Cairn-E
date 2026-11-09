"""HTTP middleware: per-request correlation ids and one structured access line."""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from cairn.logcontext import correlation_id

access_logger = logging.getLogger("cairn.server.access")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestIdMiddleware:
    """Assign every HTTP request a correlation id, echoed as `X-Request-ID`.

    The id is taken from the caller's `X-Request-ID` header when it looks sane
    (an upstream proxy can forward one) and generated otherwise.
    It is kept in a contextvar for the duration of the request so log records
    emitted while handling it carry the same id, and the request finishes with a
    single access line whose structured fields show up in JSON log mode.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        request_id = headers.get("x-request-id", "")
        if not _REQUEST_ID_RE.fullmatch(request_id):
            request_id = uuid.uuid4().hex[:16]
        token = correlation_id.set(request_id)

        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"X-Request-ID", request_id.encode("latin-1")),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            method = scope.get("method", "?")
            path = scope.get("path", "?")
            access_logger.info(
                "%s %s -> %d in %.1fms rid=%s",
                method,
                path,
                status_code,
                duration_ms,
                request_id,
                extra={
                    "request_id": request_id,
                    "method": method,
                    "path": path,
                    "status": status_code,
                    "duration_ms": duration_ms,
                },
            )
            correlation_id.reset(token)
