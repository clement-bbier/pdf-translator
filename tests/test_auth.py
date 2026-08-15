"""Unit tests for the OAuth2 device-code client (auth.py).

Every HTTP exchange goes through httpx.MockTransport: no real network access,
no real gateway, no waiting. Run: python tests/test_auth.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app import auth  # noqa: E402
from app import config as app_config  # noqa: E402

FAILURES: list[str] = []

# Placeholder gateway settings: fake hosts, never contacted (MockTransport
# intercepts every request), and deliberately not real internal URLs.
_FAKE_ENV = {
    "GATEWAY_DEVICE_ENDPOINT": "https://auth.example/oauth2/devicecode",
    "GATEWAY_TOKEN_ENDPOINT": "https://auth.example/oauth2/token",
    "GATEWAY_CLIENT_ID": "test-client-id",
    "GATEWAY_SCOPES": "openid profile test.scope",
}


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def _set_env() -> None:
    """Point auth at the fake gateway for one test."""
    for key, value in _FAKE_ENV.items():
        os.environ[key] = value


def _patch_transport(transport: httpx.MockTransport) -> None:
    """Route auth's httpx.Client through the given MockTransport."""
    original_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    auth.httpx.Client = fake_client  # type: ignore[attr-defined]


def _patch_sleep() -> None:
    """Make polling instant: the device flow sleeps between every poll."""
    auth.time.sleep = lambda seconds: None  # type: ignore[attr-defined]


def _token_response(**overrides) -> httpx.Response:
    """A successful token response, with optional field overrides."""
    body = {
        "access_token": "access-token-1",
        "refresh_token": "refresh-token-1",
        "expires_in": 3600,
    }
    body.update(overrides)
    return httpx.Response(200, json=body)


def _oauth_error(code: str, status: int = 400) -> httpx.Response:
    """An RFC 6749 / RFC 8628 style error response."""
    return httpx.Response(status, json={"error": code})


# --------------------------------------------------------------------------- #
# 1. device authorization request
# --------------------------------------------------------------------------- #


def test_request_device_code_parses_all_fields() -> None:
    """The device response is parsed, including the optional complete URI."""
    _set_env()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "device_code": "dev-code-1",
                "user_code": "WXYZ-1234",
                "verification_uri": "https://auth.example/device",
                "verification_uri_complete": "https://auth.example/device?code=WXYZ-1234",
                "expires_in": 900,
                "interval": 7,
            },
        )

    _patch_transport(httpx.MockTransport(handler))
    device = auth.request_device_code()

    check(device.device_code == "dev-code-1", "device_code not parsed")
    check(device.user_code == "WXYZ-1234", "user_code not parsed")
    check(
        device.verification_uri == "https://auth.example/device",
        "verification_uri not parsed",
    )
    check(
        device.verification_uri_complete == "https://auth.example/device?code=WXYZ-1234",
        "verification_uri_complete not parsed",
    )
    check(device.expires_in == 900, f"expires_in not parsed: {device.expires_in}")
    check(device.interval == 7, f"interval not parsed: {device.interval}")

    check(captured["url"] == _FAKE_ENV["GATEWAY_DEVICE_ENDPOINT"], "wrong device endpoint")
    check("client_id=test-client-id" in captured["body"], "client_id not sent")
    check("scope=" in captured["body"], "scope not sent")


def test_request_device_code_defaults_interval() -> None:
    """A gateway omitting `interval` falls back to the RFC default."""
    _set_env()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "device_code": "dev-code-2",
                "user_code": "ABCD-5678",
                "verification_uri": "https://auth.example/device",
            },
        )

    _patch_transport(httpx.MockTransport(handler))
    device = auth.request_device_code()

    check(
        device.interval == auth.DEFAULT_POLL_INTERVAL,
        f"interval should default to {auth.DEFAULT_POLL_INTERVAL}, got {device.interval}",
    )
    check(device.verification_uri_complete is None, "absent complete URI should be None")


# --------------------------------------------------------------------------- #
# 2. polling outcomes
# --------------------------------------------------------------------------- #


def test_poll_authorization_pending_then_success() -> None:
    """`authorization_pending` keeps polling; the token is stored on approval."""
    _set_env()
    auth.reset()
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return _oauth_error("authorization_pending")
        return _token_response()

    _patch_transport(httpx.MockTransport(handler))
    _patch_sleep()

    session = auth.poll_for_token("dev-code-1", interval=1.0, expires_in=600)

    check(attempts["count"] == 3, f"expected 3 polls, got {attempts['count']}")
    check(session.access_token == "access-token-1", "access token not stored")
    authenticated, _ = auth.is_authenticated()
    check(authenticated, "session should be authenticated after a successful poll")


def test_poll_slow_down_increases_interval() -> None:
    """`slow_down` backs the polling interval off before continuing."""
    _set_env()
    auth.reset()
    attempts = {"count": 0}
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return _oauth_error("slow_down")
        return _token_response()

    _patch_transport(httpx.MockTransport(handler))
    auth.time.sleep = lambda seconds: delays.append(seconds)  # type: ignore[attr-defined]

    auth.poll_for_token("dev-code-1", interval=5.0, expires_in=600)

    check(len(delays) >= 2, f"expected at least 2 sleeps, got {delays}")
    check(
        delays[1] == delays[0] + auth.SLOW_DOWN_INCREMENT,
        f"slow_down should add {auth.SLOW_DOWN_INCREMENT}s: {delays}",
    )


def test_poll_expired_token_raises() -> None:
    """`expired_token` fails cleanly with AuthError."""
    _set_env()
    auth.reset()

    _patch_transport(httpx.MockTransport(lambda request: _oauth_error("expired_token")))
    _patch_sleep()

    raised = False
    try:
        auth.poll_for_token("dev-code-1", interval=1.0, expires_in=600)
    except auth.AuthError:
        raised = True

    check(raised, "expired_token should raise AuthError")
    authenticated, _ = auth.is_authenticated()
    check(not authenticated, "a failed flow must not leave a session behind")


def test_poll_access_denied_raises() -> None:
    """`access_denied` fails cleanly with AuthError."""
    _set_env()
    auth.reset()

    _patch_transport(httpx.MockTransport(lambda request: _oauth_error("access_denied")))
    _patch_sleep()

    raised = False
    try:
        auth.poll_for_token("dev-code-1", interval=1.0, expires_in=600)
    except auth.AuthError:
        raised = True

    check(raised, "access_denied should raise AuthError")


# --------------------------------------------------------------------------- #
# 3. get_token, refresh and concurrency
# --------------------------------------------------------------------------- #


def test_get_token_without_session_raises() -> None:
    """get_token() with no active session raises a clear AuthError."""
    _set_env()
    auth.reset()

    raised = False
    try:
        auth.get_token()
    except auth.AuthError:
        raised = True

    check(raised, "get_token without a session should raise AuthError")


def test_get_token_returns_valid_token_without_refresh() -> None:
    """A token comfortably far from expiry is returned as-is, with no HTTP call."""
    _set_env()
    auth.reset()
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _token_response(access_token="should-not-be-used")

    _patch_transport(httpx.MockTransport(handler))
    auth._session = auth._Session(  # type: ignore[attr-defined]
        access_token="still-valid",
        refresh_token="refresh-token-1",
        expires_at=time.monotonic() + 3600,
    )

    token = auth.get_token()
    check(token == "still-valid", f"expected the stored token, got {token!r}")
    check(calls["count"] == 0, "a still-valid token must not trigger a refresh call")


def test_get_token_refreshes_near_expiry() -> None:
    """A token inside the refresh margin is renewed transparently."""
    _set_env()
    auth.reset()
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        body = request.content.decode()
        check("grant_type=refresh_token" in body, "refresh must use grant_type=refresh_token")
        check("refresh_token=refresh-token-1" in body, "stored refresh token not sent")
        return _token_response(access_token="access-token-2")

    _patch_transport(httpx.MockTransport(handler))
    # Just inside the margin: expires in less than REFRESH_MARGIN_SECONDS.
    auth._session = auth._Session(  # type: ignore[attr-defined]
        access_token="about-to-expire",
        refresh_token="refresh-token-1",
        expires_at=time.monotonic() + (auth.REFRESH_MARGIN_SECONDS / 2),
    )

    token = auth.get_token()
    check(token == "access-token-2", f"expected the refreshed token, got {token!r}")
    check(calls["count"] == 1, f"expected exactly 1 refresh call, got {calls['count']}")


def test_concurrent_get_token_refreshes_once() -> None:
    """A burst of concurrent callers triggers exactly one refresh, not one each."""
    _set_env()
    auth.reset()
    calls = {"count": 0}
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            calls["count"] += 1
        # Slow enough that every thread piles up on the auth lock.
        time.sleep(0.05)
        return _token_response(access_token="access-token-refreshed", expires_in=3600)

    _patch_transport(httpx.MockTransport(handler))
    auth._session = auth._Session(  # type: ignore[attr-defined]
        access_token="about-to-expire",
        refresh_token="refresh-token-1",
        expires_at=time.monotonic() + (auth.REFRESH_MARGIN_SECONDS / 2),
    )

    results: list[str] = []
    results_lock = threading.Lock()

    def worker() -> None:
        token = auth.get_token()
        with results_lock:
            results.append(token)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    check(
        calls["count"] == 1,
        f"8 concurrent callers should trigger exactly 1 refresh, got {calls['count']}",
    )
    check(len(results) == 8, f"every caller should get a token, got {len(results)}")
    check(
        all(token == "access-token-refreshed" for token in results),
        "every caller should receive the refreshed token",
    )


# --------------------------------------------------------------------------- #
# 4. configuration and information leaks
# --------------------------------------------------------------------------- #


def test_missing_env_var_names_the_variable() -> None:
    """A missing gateway variable raises AuthError naming exactly which one."""
    _set_env()
    auth.reset()
    previous = os.environ.pop("GATEWAY_DEVICE_ENDPOINT", None)

    message = ""
    try:
        auth.request_device_code()
    except auth.AuthError as error:
        message = str(error)
    finally:
        if previous is not None:
            os.environ["GATEWAY_DEVICE_ENDPOINT"] = previous

    check(
        "GATEWAY_DEVICE_ENDPOINT" in message,
        f"the error should name the missing variable, got {message!r}",
    )


def test_is_authenticated_never_leaks_the_token() -> None:
    """is_authenticated() exposes only a boolean and a remaining lifetime."""
    _set_env()
    auth.reset()
    auth._session = auth._Session(  # type: ignore[attr-defined]
        access_token="super-secret-token",
        refresh_token="refresh-token-1",
        expires_at=time.monotonic() + 1800,
    )

    authenticated, expires_in = auth.is_authenticated()
    check(authenticated is True, "should report an authenticated session")
    check(isinstance(expires_in, int), "expires_in should be an int")
    check(
        "super-secret-token" not in repr((authenticated, expires_in)),
        "is_authenticated must never expose the token",
    )

    status, detail = auth.flow_status()
    check(
        "super-secret-token" not in status + detail,
        "flow_status must never expose the token",
    )


def test_reset_clears_the_session() -> None:
    """reset() drops the in-memory session entirely."""
    _set_env()
    auth._session = auth._Session(  # type: ignore[attr-defined]
        access_token="token",
        refresh_token="refresh",
        expires_at=time.monotonic() + 3600,
    )
    auth.reset()

    authenticated, expires_in = auth.is_authenticated()
    check(not authenticated, "reset should clear the session")
    check(expires_in is None, "reset should clear the expiry")


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def main() -> int:
    """Run every test_* function in this module."""
    app_config.force_utf8_stdout()

    tests = [
        test_request_device_code_parses_all_fields,
        test_request_device_code_defaults_interval,
        test_poll_authorization_pending_then_success,
        test_poll_slow_down_increases_interval,
        test_poll_expired_token_raises,
        test_poll_access_denied_raises,
        test_get_token_without_session_raises,
        test_get_token_returns_valid_token_without_refresh,
        test_get_token_refreshes_near_expiry,
        test_concurrent_get_token_refreshes_once,
        test_missing_env_var_names_the_variable,
        test_is_authenticated_never_leaks_the_token,
        test_reset_clears_the_session,
    ]

    original_client = httpx.Client
    original_sleep = time.sleep
    original_env = {key: os.environ.get(key) for key in _FAKE_ENV}

    for test in tests:
        failures_before = len(FAILURES)
        try:
            test()
        except Exception as error:  # noqa: BLE001 - report and continue
            FAILURES.append(f"{test.__name__} raised {type(error).__name__}: {error}")
        finally:
            auth.httpx.Client = original_client  # type: ignore[attr-defined]
            auth.time.sleep = original_sleep  # type: ignore[attr-defined]
        status = "OK" if len(FAILURES) == failures_before else "FAIL"
        print(f"[{status}] {test.__name__}")

    auth.reset()
    for key, value in original_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    print(f"\n{len(tests)} tests, {len(FAILURES)} failure(s)")
    for failure in FAILURES:
        print(f"  - {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
