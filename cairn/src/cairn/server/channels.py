"""Separate the trusted UI channel from the untrusted worker channel.

Both callers reach the same FastAPI app: a browser that authenticated with Basic Auth,
and the pi guard extension through the loopback port that ``docker-compose.yaml``
publishes so a host-networked worker can reach it. The network path cannot tell them
apart — a compromised worker can call anything loopback answers — so the separation is
carried by the request context:

* ``/internal/*`` is the worker's channel. It exposes exactly what the guard needs (run
  a check, poll one approval) and requires ``CAIRN_WORKER_TOKEN``.
* Endpoints that can weaken the guard — writing tool rules, deciding an approval,
  editing scope or constraints — require an authenticated UI request, which
  ``UIAuthMiddleware`` marks in ``request.state``.

When ``web.auth`` is not configured, the UI channel stays open; enabling authentication
is the explicit act that turns the separation on.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, Request

WORKER_TOKEN_ENV = "CAIRN_WORKER_TOKEN"


def _expected(env_name: str) -> str:
    return os.environ.get(env_name, "").strip()


def _matches(expected: str, supplied: str | None) -> bool:
    return hmac.compare_digest(expected.encode("utf-8"), (supplied or "").encode("utf-8"))


def require_worker_channel(
    x_cairn_worker_token: str | None = Header(default=None),
) -> None:
    """Gate ``/internal/*``: only the guard extension, holding the worker token."""
    expected = _expected(WORKER_TOKEN_ENV)
    if not expected:
        return
    if not _matches(expected, x_cairn_worker_token):
        raise HTTPException(status_code=401, detail="worker token required")


def require_ui_channel(request: Request) -> None:
    """Gate the endpoints that could be used to switch the guard off.

    A worker that reached these could write an "allow everything" rule or approve its
    own pending command, which would make the reviewer advisory rather than enforced.
    ``UIAuthMiddleware`` marks requests that passed Basic Auth (or an unauthenticated
    deployment where no credentials are configured).
    """
    if not getattr(request.state, "ui_authenticated", False):
        raise HTTPException(status_code=403, detail="ui channel required")
