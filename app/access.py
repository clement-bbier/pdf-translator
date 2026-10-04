"""Optional HTTP Basic access guard for the web UI, driven by APP_PASSWORD.

Unrelated to app/auth.py (the gateway OAuth device flow). When APP_PASSWORD is
set and non-empty, every HTTP route except /healthz requires Basic credentials
(any user name, the password must match). When unset or empty, nothing changes.

HTTP Basic sends the password base64-encoded on every request: serve it behind
TLS (reverse proxy). The password is never logged or echoed.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import secrets
from typing import Any

PASSWORD_ENV = "APP_PASSWORD"
HEALTH_PATH = "/healthz"
_CHALLENGE = b'Basic realm="PDF Translator", charset="UTF-8"'


def configured_password() -> str | None:
    """Return the configured password, or None when unset or empty."""
    value = os.environ.get(PASSWORD_ENV, "").strip()
    return value or None


def check_basic_auth(header: str | None, password: str) -> bool:
    """True when ``header`` is a valid Basic credential for ``password``."""
    if not header:
        return False
    scheme, _, encoded = header.strip().partition(" ")
    if scheme.lower() != "basic" or not encoded.strip():
        return False
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    if ":" not in decoded:
        return False
    given = decoded.split(":", 1)[1]
    return secrets.compare_digest(given.encode("utf-8"), password.encode("utf-8"))


class AccessGuardMiddleware:
    """Pure ASGI middleware (keeps streaming responses and background tasks intact)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        password = configured_password()
        if password is None:
            await self.app(scope, receive, send)
            return

        header = None
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                header = value.decode("latin-1")
                break
        if check_basic_auth(header, password):
            await self.app(scope, receive, send)
            return

        body = json.dumps({"detail": "Authentication required."}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", _CHALLENGE),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def log_access_mode(logger: logging.Logger) -> None:
    """Log the protection mode once (never the password)."""
    if configured_password() is None:
        logger.warning(
            "APP_PASSWORD is not set: the web UI is unprotected (intended for local use only)"
        )
    else:
        logger.info("access protection enabled (HTTP Basic)")
