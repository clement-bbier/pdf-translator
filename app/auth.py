"""OAuth2 device-code flow (RFC 8628) against the internal LLM gateway.

The full device authorization grant is implemented here:

1. Device authorization: ``request_device_code()`` POSTs to the device
   authorization endpoint and receives a ``device_code``, a ``user_code``, a
   ``verification_uri`` (plus ``verification_uri_complete`` when the gateway
   provides one), an ``expires_in`` and a polling ``interval``.
2. User approval: the caller shows the ``verification_uri`` and ``user_code``
   so the user can approve the request in their browser with their existing
   corporate SSO session. This application never handles credentials.
3. Polling: ``poll_for_token()`` polls the token endpoint at the gateway's
   interval, honouring ``authorization_pending`` (keep waiting), ``slow_down``
   (back off), and failing cleanly on ``expired_token`` / ``access_denied``.
4. In-memory storage: the access and refresh tokens live in module state only,
   for the lifetime of the process. Nothing is ever written to disk.
5. Proactive refresh: ``get_token()`` refreshes the access token when it is
   within ``REFRESH_MARGIN_SECONDS`` of expiry, transparently to callers, and
   serialises concurrent callers so a burst triggers exactly one refresh.

Endpoint URLs, client_id and scopes come exclusively from environment
variables (.env), never hardcoded. ``get_token()`` is the only function
``GatewayTranslator`` (translator.py) uses; it must never touch the flow
directly.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# Refresh the access token once it is this close to expiring.
REFRESH_MARGIN_SECONDS = 60.0

# Fallback polling interval when the gateway omits it (RFC 8628 §3.2).
DEFAULT_POLL_INTERVAL = 5.0

# Extra delay added to the interval on a `slow_down` response (RFC 8628 §3.5).
SLOW_DOWN_INCREMENT = 5.0

HTTP_TIMEOUT = 30.0


class AuthError(Exception):
    """Raised when the device-code flow fails or no valid session is available."""


@dataclass(slots=True)
class DeviceCodeResponse:
    """The gateway's answer to a device authorization request.

    ``verification_uri_complete`` embeds the user code in the URL; gateways may
    omit it, in which case the user types ``user_code`` at ``verification_uri``.
    """

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: float
    verification_uri_complete: str | None = None


@dataclass(slots=True)
class _Session:
    """The tokens currently held, in memory only."""

    access_token: str
    refresh_token: str | None
    expires_at: float  # time.monotonic() based, comparable across refreshes


# Deployment model: this is a local POC — one process serves one user, whose own
# SSO login produces the token held here, and who consumes their own quota. If
# this is ever deployed as a shared server for several users, this module-level
# token must become per-user-session storage (e.g. keyed by an HTTP session
# cookie), because a module global would otherwise hand one user's token to
# everyone hitting the same instance. Not implemented today — see the
# "Deployment model" section of README.md.
_session: _Session | None = None
_lock = threading.Lock()

# Non-sensitive view of the flow, for the UI: idle | pending | authenticated | failed.
_flow_status: str = "idle"
_flow_detail: str = ""


def _require_env(name: str) -> str:
    """Return an environment variable, or raise a clear AuthError naming it.

    Read at call time, never at import: .env is loaded by app.config, and tests
    need to set these after the module is already imported.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise AuthError(
            f"{name} is not set. Fill it in your .env file "
            "(see .env.example) before connecting to the gateway."
        )
    return value


def _post_form(url: str, data: dict[str, str]) -> httpx.Response:
    """POST a form-encoded body. Isolated so tests can route it through a MockTransport."""
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        return client.post(url, data=data)


def _error_code(response: httpx.Response) -> str:
    """Extract the OAuth2 ``error`` code from a response, or '' when absent."""
    try:
        payload = response.json()
    except ValueError:
        return ""
    return str(payload.get("error", "")) if isinstance(payload, dict) else ""


def _store_token_response(payload: dict) -> _Session:
    """Turn a successful token response into the stored session."""
    access_token = payload.get("access_token")
    if not access_token:
        raise AuthError("gateway token response contained no access_token")

    expires_in = float(payload.get("expires_in", 3600))
    session = _Session(
        access_token=str(access_token),
        refresh_token=payload.get("refresh_token"),
        expires_at=time.monotonic() + expires_in,
    )

    global _session
    _session = session
    logger.info("gateway session established (expires in %.0fs)", expires_in)
    return session


def request_device_code() -> DeviceCodeResponse:
    """Start the flow: ask the gateway for a device code and a user code."""
    endpoint = _require_env("GATEWAY_DEVICE_ENDPOINT")
    data = {
        "client_id": _require_env("GATEWAY_CLIENT_ID"),
        "scope": _require_env("GATEWAY_SCOPES"),
    }

    try:
        response = _post_form(endpoint, data)
    except httpx.HTTPError as error:
        raise AuthError(f"device authorization request failed: {error}") from error

    if response.status_code >= 400:
        raise AuthError(
            f"device authorization rejected by the gateway ({response.status_code})"
        )

    try:
        payload = response.json()
    except ValueError as error:
        raise AuthError("malformed device authorization response") from error

    required = ("device_code", "user_code", "verification_uri")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise AuthError(
            f"device authorization response missing {', '.join(missing)}"
        )

    return DeviceCodeResponse(
        device_code=str(payload["device_code"]),
        user_code=str(payload["user_code"]),
        verification_uri=str(payload["verification_uri"]),
        verification_uri_complete=(
            str(payload["verification_uri_complete"])
            if payload.get("verification_uri_complete")
            else None
        ),
        expires_in=int(payload.get("expires_in", 600)),
        interval=float(payload.get("interval", DEFAULT_POLL_INTERVAL)),
    )


def poll_for_token(
    device_code: str,
    interval: float = DEFAULT_POLL_INTERVAL,
    expires_in: int = 600,
) -> _Session:
    """Poll the token endpoint until the user approves, denies, or the code expires.

    Blocks for as long as the user takes to approve, so callers that must stay
    responsive should run it off the request thread (see ``start_device_flow``).
    """
    endpoint = _require_env("GATEWAY_TOKEN_ENDPOINT")
    client_id = _require_env("GATEWAY_CLIENT_ID")

    deadline = time.monotonic() + expires_in
    current_interval = max(interval, 1.0)

    while True:
        if time.monotonic() >= deadline:
            raise AuthError("the device code expired before the request was approved")

        time.sleep(current_interval)

        try:
            response = _post_form(
                endpoint,
                {
                    "grant_type": DEVICE_CODE_GRANT,
                    "device_code": device_code,
                    "client_id": client_id,
                },
            )
        except httpx.HTTPError as error:
            raise AuthError(f"token request failed: {error}") from error

        if response.status_code < 400:
            payload = response.json()
            if not isinstance(payload, dict):
                raise AuthError("malformed token response")
            return _store_token_response(payload)

        error_code = _error_code(response)
        if error_code == "authorization_pending":
            continue
        if error_code == "slow_down":
            current_interval += SLOW_DOWN_INCREMENT
            logger.info("gateway asked to slow down, interval now %.0fs", current_interval)
            continue
        if error_code == "expired_token":
            raise AuthError("the device code expired before the request was approved")
        if error_code == "access_denied":
            raise AuthError("the authorization request was denied")

        raise AuthError(
            f"token request rejected by the gateway ({response.status_code}"
            + (f", {error_code}" if error_code else "")
            + ")"
        )


def refresh_token() -> _Session:
    """Exchange the stored refresh token for a fresh access token."""
    current = _session
    if current is None or not current.refresh_token:
        raise AuthError("no refresh token available; sign in again")

    endpoint = _require_env("GATEWAY_TOKEN_ENDPOINT")
    try:
        response = _post_form(
            endpoint,
            {
                "grant_type": "refresh_token",
                "refresh_token": current.refresh_token,
                "client_id": _require_env("GATEWAY_CLIENT_ID"),
            },
        )
    except httpx.HTTPError as error:
        raise AuthError(f"token refresh failed: {error}") from error

    if response.status_code >= 400:
        raise AuthError(f"token refresh rejected by the gateway ({response.status_code})")

    payload = response.json()
    if not isinstance(payload, dict):
        raise AuthError("malformed refresh response")

    # A gateway may omit refresh_token on refresh: keep the existing one.
    payload.setdefault("refresh_token", current.refresh_token)
    return _store_token_response(payload)


def get_token() -> str:
    """Return a valid access token, refreshing it first if it is about to expire.

    The only public entry point used by ``GatewayTranslator``. Raises AuthError
    when no session exists, so the caller can trigger the device flow.
    """
    with _lock:
        current = _session
        if current is None:
            raise AuthError(
                "not signed in to the gateway: start the device-code flow first"
            )

        # Re-checked inside the lock: concurrent callers that all saw an expiring
        # token queue here, and only the first one actually refreshes.
        if current.expires_at - time.monotonic() <= REFRESH_MARGIN_SECONDS:
            return refresh_token().access_token

        return current.access_token


def is_authenticated() -> tuple[bool, int | None]:
    """Return (authenticated, seconds until expiry). Never returns the token itself."""
    current = _session
    if current is None:
        return False, None

    remaining = int(current.expires_at - time.monotonic())
    if remaining <= 0:
        # Still authenticated if a refresh token can renew it without the user.
        return bool(current.refresh_token), 0
    return True, remaining


def flow_status() -> tuple[str, str]:
    """Return (status, detail) of the device flow, for the UI. Never includes a token."""
    return _flow_status, _flow_detail


def start_device_flow() -> DeviceCodeResponse:
    """Request a device code and poll for the token in a background thread.

    Returns as soon as the user code is available so the caller can display it;
    the token arrives later, observable through ``is_authenticated()``.
    """
    global _flow_status, _flow_detail

    device = request_device_code()
    _flow_status, _flow_detail = "pending", "waiting for approval"

    def _poll() -> None:
        global _flow_status, _flow_detail
        try:
            poll_for_token(device.device_code, device.interval, device.expires_in)
            _flow_status, _flow_detail = "authenticated", ""
        except AuthError as error:
            _flow_status, _flow_detail = "failed", str(error)
            logger.warning("device flow failed: %s", error)

    threading.Thread(target=_poll, name="device-code-poll", daemon=True).start()
    return device


def reset() -> None:
    """Drop the in-memory session and flow state (used by tests and sign-out)."""
    global _session, _flow_status, _flow_detail
    with _lock:
        _session = None
    _flow_status, _flow_detail = "idle", ""
