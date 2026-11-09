"""HTTP Basic authentication for the UI channel, enforced by the server itself.

The UI used to sit behind an nginx sidecar that rendered ``.htpasswd`` from the same
``web.auth`` block and injected ``X-Cairn-UI-Token`` into proxied requests. That proxy
existed only for authentication, so the logic lives here now and the sidecar is gone:

* ``Authorization: Basic ...`` is validated for every UI request (pages, API, SSE).
* ``/healthz`` and ``/internal/*`` are exempt: the first is a liveness probe, the second
  is the worker channel and keeps its own ``CAIRN_WORKER_TOKEN`` contract.
* Without ``web.auth`` the UI stays open, matching a deployment that never configured a
  proxy. The CLI refuses to bind a non-loopback host in that state unless the operator
  explicitly passes ``--allow-insecure``.

Passwords are compared in memory as written in ``dispatch.yaml``. The old proxy hashed
them for ``.htpasswd``, but that was a storage format, not an extra secret: the file
already holds the plaintext, and Python 3.13 removed the ``crypt`` module that produced
those hashes.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import ValidationError
from starlette.responses import PlainTextResponse

from cairn.contracts.web import WebAuthConfig

LOG = logging.getLogger("cairn.server.auth")

WEB_CONFIG_ENV = "CAIRN_WEB_CONFIG"
UI_TOKEN_ENV = "CAIRN_UI_TOKEN"
# Paths that never carry UI credentials: the liveness probe and the worker channel.
PUBLIC_PATHS = frozenset({"/healthz"})
WORKER_PREFIXES = ("/internal/",)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class WebAuth:
    """The credential set, normalised once so request handling stays allocation-free."""

    realm: str
    users: tuple[tuple[bytes, bytes], ...]

    def matches(self, user: str, password: str) -> bool:
        supplied_user = user.encode("utf-8")
        supplied_password = password.encode("utf-8")
        allowed = False
        # Compare against every user instead of returning on the first miss, so the
        # response time does not reveal which user names exist.
        for name, secret in self.users:
            user_ok = hmac.compare_digest(name, supplied_user)
            password_ok = hmac.compare_digest(secret, supplied_password)
            allowed |= user_ok & password_ok
        return allowed


def load_web_auth(path: Path) -> WebAuth | None:
    """Read the ``web.auth`` block of ``dispatch.yaml``; ``None`` means "no auth"."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ValueError(f"cannot read web config {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"cannot parse web config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"web config {path} must be a mapping")
    web = raw.get("web")
    if web is None:
        return None
    if not isinstance(web, dict):
        raise ValueError(f"web config {path}: 'web' must be a mapping")
    raw_auth = web.get("auth")
    if raw_auth is None:
        return None
    try:
        config = WebAuthConfig.model_validate(raw_auth)
    except ValidationError as exc:
        raise ValueError(f"web config {path}: {exc}") from exc
    if not config.users:
        return None
    return WebAuth(
        realm=config.realm,
        users=tuple(
            (user.name.encode("utf-8"), user.password.encode("utf-8")) for user in config.users
        ),
    )


_cache: dict[Path, tuple[int, int, WebAuth | None]] = {}


def _cached(path: Path) -> WebAuth | None:
    """Re-read only when mtime/size changes, so static requests do not parse YAML."""
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    hit = _cache.get(path)
    if hit and hit[:2] == key:
        return hit[2]
    auth = load_web_auth(path)
    _cache[path] = (key[0], key[1], auth)
    return auth


def current_web_auth() -> WebAuth | None:
    """Resolve the configured UI credentials from ``CAIRN_WEB_CONFIG``."""
    raw_path = os.environ.get(WEB_CONFIG_ENV, "").strip()
    if not raw_path:
        return None
    return _cached(Path(raw_path))


def _header(scope: dict[str, Any], name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def _basic_credentials(scope: dict[str, Any]) -> tuple[str, str] | None:
    header = _header(scope, b"authorization").strip()
    if not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    user, separator, password = decoded.partition(":")
    if not separator:
        return None
    return user, password


def _is_cross_site(scope: dict[str, Any]) -> bool:
    """Cheap CSRF guard: reject state-changing requests from another origin.

    Modern browsers send ``Sec-Fetch-Site``; older ones only send ``Origin`` for
    cross-origin requests, so both are checked. A request with neither (curl, the
    dispatcher) is not a browser and is allowed through.
    """
    if scope.get("method") in _SAFE_METHODS:
        return False
    site = _header(scope, b"sec-fetch-site").lower()
    if site:
        return site == "cross-site"
    origin = _header(scope, b"origin")
    if not origin:
        return False
    return urlsplit(origin).netloc != _header(scope, b"host")


class UIAuthMiddleware:
    """Pure ASGI middleware, so SSE responses are streamed, not buffered."""

    def __init__(
        self,
        app: Any,
        auth_loader: Callable[[], WebAuth | None] | None = None,
    ) -> None:
        self.app = app
        self._load = auth_loader or current_web_auth

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in PUBLIC_PATHS or path.startswith(WORKER_PREFIXES):
            await self.app(scope, receive, send)
            return
        try:
            auth = self._load()
        except (ValueError, OSError) as exc:
            # Fail closed: a broken auth file must not silently open the UI.
            LOG.error("web auth configuration is unusable: %s", exc)
            await PlainTextResponse("authentication unavailable", 503)(scope, receive, send)
            return
        if auth is None:
            # No credentials configured. If the legacy UI token is set, leave the
            # request unauthenticated so ``require_ui_channel`` still demands it.
            if not os.environ.get(UI_TOKEN_ENV, "").strip():
                scope.setdefault("state", {})["ui_authenticated"] = True
            await self.app(scope, receive, send)
            return
        if _is_cross_site(scope):
            await PlainTextResponse("cross-site request", 403)(scope, receive, send)
            return
        # Legacy UI token: keep scripted clients working now that no proxy injects it.
        # It is a custom header, so a cross-site page cannot set it without CORS.
        expected_token = os.environ.get(UI_TOKEN_ENV, "").strip()
        if expected_token and hmac.compare_digest(
            expected_token.encode("utf-8"), _header(scope, b"x-cairn-ui-token").encode("utf-8")
        ):
            scope.setdefault("state", {})["ui_authenticated"] = True
            await self.app(scope, receive, send)
            return
        credentials = _basic_credentials(scope)
        if credentials is not None and auth.matches(*credentials):
            scope.setdefault("state", {})["ui_authenticated"] = True
            await self.app(scope, receive, send)
            return
        await PlainTextResponse(
            "authentication required",
            status_code=401,
            headers={"WWW-Authenticate": f'Basic realm="{auth.realm}"'},
        )(scope, receive, send)
