"""Tests for the HTTP Basic access guard, /healthz and the APP_PROVIDER override.

Run: python tests/test_access.py
"""

from __future__ import annotations

import base64
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import access  # noqa: E402
from app import config as app_config  # noqa: E402

FAILURES: list[str] = []
COUNT = 0


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    global COUNT
    COUNT += 1
    if not condition:
        FAILURES.append(message)


@contextmanager
def env(name: str, value: str | None) -> Iterator[None]:
    """Set or remove an environment variable, restoring the original afterwards."""
    original = os.environ.get(name)
    try:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        yield
    finally:
        if original is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = original


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _startup_records(password: str | None) -> list[logging.LogRecord]:
    from app.main import app

    handler = _Capture()
    loggers = [logging.getLogger("app.main"), logging.getLogger("app.access")]
    previous = [(lg, lg.level) for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    try:
        with env(access.PASSWORD_ENV, password), TestClient(app):
            pass
    finally:
        for lg, level in previous:
            lg.removeHandler(handler)
            lg.setLevel(level)
    return handler.records


def _blob(response) -> str:
    return response.text + repr(dict(response.headers))


def test_unprotected() -> None:
    from app.main import app

    for value in (None, ""):
        with env(access.PASSWORD_ENV, value), TestClient(app) as client:
            check(client.get("/").status_code == 200, f"unprotected GET / ({value!r})")
            health = client.get("/healthz")
            check(health.status_code == 200, "unprotected /healthz status")
            check(health.json() == {"status": "ok"}, "unprotected /healthz body")


def test_protected() -> None:
    from app.main import app

    password = ":".join(["Pw", "with", "colons"])
    with env(access.PASSWORD_ENV, password), TestClient(app) as client:
        denied = client.get("/")
        check(denied.status_code == 401, "no header -> 401")
        check(
            denied.headers.get("www-authenticate", "").startswith("Basic"),
            "401 carries WWW-Authenticate Basic",
        )
        check(denied.json() == {"detail": "Authentication required."}, "401 body")
        health = client.get("/healthz")
        check(health.status_code == 200, "/healthz open when protected")
        check(health.json() == {"status": "ok"}, "/healthz body when protected")

        ok_root = client.get("/", headers=basic("anyone", password))
        check(ok_root.status_code == 200, "correct password on /")
        ok_status = client.get("/auth/status", headers=basic("", password))
        check(ok_status.status_code == 200, "correct password on /auth/status")

        responses = [denied, health, ok_status]
        wrong = client.get("/", headers=basic("u", "wrong"))
        check(wrong.status_code == 401, "wrong password -> 401")
        responses.append(wrong)
        check(client.get("/docs").status_code == 401, "/docs is protected")

        for bad in (
            "Bearer x",
            "Basic !!!notbase64",
            "Basic " + base64.b64encode(b"nocolon").decode(),
            "Basic",
            "",
        ):
            r = client.get("/", headers={"Authorization": bad})
            check(r.status_code == 401, f"malformed header {bad!r} -> 401")
            responses.append(r)

        lower = client.get(
            "/", headers={"Authorization": "basic " + basic("u", password)["Authorization"][6:]}
        )
        check(lower.status_code == 200, "scheme is case-insensitive")

        for r in responses:
            check(password not in _blob(r), "password absent from responses")

    uni = "pässwörd-日本"
    with env(access.PASSWORD_ENV, uni), TestClient(app) as client:
        check(client.get("/", headers=basic("u", uni)).status_code == 200, "non-ASCII password")
        check(client.get("/", headers=basic("u", "x")).status_code == 401, "non-ASCII wrong")


def test_startup_logs() -> None:
    records = _startup_records(None)
    warnings = [
        r for r in records if r.levelno == logging.WARNING and "unprotected" in r.getMessage()
    ]
    check(len(warnings) == 1, f"exactly one unprotected warning, got {len(warnings)}")

    password = "-".join(["log", "leak", "canary", "123"])
    records = _startup_records(password)
    check(
        all(password not in r.getMessage() for r in records),
        "password never in startup logs",
    )
    check(
        not any("unprotected" in r.getMessage() for r in records),
        "no unprotected warning when protected",
    )


def test_check_basic_auth() -> None:
    good = basic("u", "pw")["Authorization"]
    check(access.check_basic_auth(good, "pw"), "valid header")
    check(not access.check_basic_auth(good, "other"), "wrong password")
    check(not access.check_basic_auth(None, "pw"), "missing header")
    check(not access.check_basic_auth("Basic %%%", "pw"), "invalid base64")
    bad_utf8 = "Basic " + base64.b64encode(b"u:\xff\xfe").decode()
    check(not access.check_basic_auth(bad_utf8, "pw"), "invalid utf-8")
    check(access.check_basic_auth(basic("u", "a:b")["Authorization"], "a:b"), "colon in password")
    with env(access.PASSWORD_ENV, "   "):
        check(access.configured_password() is None, "blank password means unprotected")


def test_provider_override() -> None:
    try:
        with env(app_config.PROVIDER_ENV, "fake"):
            check(app_config.load_config(reload=True)["provider"] == "fake", "override applies")
        with env(app_config.PROVIDER_ENV, ""):
            config = app_config.load_config(reload=True)
            check(config["provider"] != "", "empty override keeps yaml value")
        with env(app_config.PROVIDER_ENV, None):
            yaml_provider = app_config.load_config(reload=True)["provider"]
        with env(app_config.PROVIDER_ENV, ""):
            check(app_config.load_config(reload=True)["provider"] == yaml_provider, "empty = yaml")
    finally:
        with env(app_config.PROVIDER_ENV, None):
            app_config.load_config(reload=True)


def main() -> int:
    """Run every test in this module."""
    app_config.force_utf8_stdout()
    for test in (
        test_unprotected,
        test_protected,
        test_startup_logs,
        test_check_basic_auth,
        test_provider_override,
    ):
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append(f"{test.__name__} raised {type(exc).__name__}: {exc}")
    for failure in FAILURES:
        print(f"FAIL: {failure}")
    print(f"{COUNT} tests, {len(FAILURES)} failures")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
