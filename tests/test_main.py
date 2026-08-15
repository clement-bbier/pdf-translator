"""Integration tests for the FastAPI app: TestClient, provider=mock, no network.

Run: python tests/test_main.py
"""

from __future__ import annotations

import io
import sys
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import config as app_config  # noqa: E402
from tests import make_fixtures  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def _force_mock_provider() -> None:
    """Ensure config.yaml is loaded with provider=mock regardless of the file's value."""
    config = app_config.load_config()
    config["provider"] = "mock"


def _wait_for_done(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    """Poll GET /jobs/{id} until status is done or error, or raise on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}")
        check(response.status_code == 200, f"GET /jobs/{job_id} should return 200")
        data = response.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(0.2)
    raise TimeoutError(f"job {job_id} did not finish within {timeout}s")


def _open_client() -> TestClient:
    from app.main import app

    return TestClient(app)


# --------------------------------------------------------------------------- #
# 1. full cycle: POST /jobs -> poll -> done
# --------------------------------------------------------------------------- #


def test_create_job_and_poll_to_done() -> None:
    """A valid upload is accepted (202), and polling reaches status=done."""
    _force_mock_provider()
    fixture = make_fixtures.FIXTURES_DIR / "simple.pdf"

    with _open_client() as client:
        with fixture.open("rb") as handle:
            response = client.post(
                "/jobs",
                files=[("files", ("simple.pdf", handle, "application/pdf"))],
                data={"src": "zh", "tgt": "fr"},
            )
        check(response.status_code == 202, f"expected 202, got {response.status_code}")
        job_id = response.json().get("job_id")
        check(bool(job_id), "response should contain a job_id")

        data = _wait_for_done(client, job_id)
        check(data["status"] == "done", f"expected status=done, got {data['status']!r}")
        check("stats" in data, "done payload should include stats")

        # cleanup: download to purge job dirs
        client.get(f"/jobs/{job_id}/download")


# --------------------------------------------------------------------------- #
# 2. invalid uploads are rejected with 400, nothing created
# --------------------------------------------------------------------------- #


def test_too_many_files_rejected() -> None:
    """More than max_files uploads -> 400, no job created."""
    _force_mock_provider()
    config = app_config.load_config()
    max_files = int(config.get("max_files", 10))
    fixture = make_fixtures.FIXTURES_DIR / "simple.pdf"

    with _open_client() as client:
        handles = [fixture.open("rb") for _ in range(max_files + 1)]
        try:
            files_payload = [
                ("files", (f"file_{i}.pdf", handle, "application/pdf"))
                for i, handle in enumerate(handles)
            ]
            response = client.post(
                "/jobs", files=files_payload, data={"src": "zh", "tgt": "fr"}
            )
        finally:
            for handle in handles:
                handle.close()

        check(response.status_code == 400, f"expected 400, got {response.status_code}")


def test_invalid_file_content_rejected() -> None:
    """A non-PDF file (wrong magic bytes) -> 400, no job created."""
    _force_mock_provider()
    fake_pdf = io.BytesIO(b"this is not a pdf file at all")

    with _open_client() as client:
        response = client.post(
            "/jobs",
            files=[("files", ("fake.pdf", fake_pdf, "application/pdf"))],
            data={"src": "zh", "tgt": "fr"},
        )
        check(response.status_code == 400, f"expected 400, got {response.status_code}")


# --------------------------------------------------------------------------- #
# 3. download before completion -> 404
# --------------------------------------------------------------------------- #


def test_download_before_done_returns_404() -> None:
    """Downloading a job that is still pending/running returns 404.

    TestClient runs the ASGI app (including BackgroundTasks) on its own worker
    thread and blocks the calling thread until the whole request/response cycle
    finishes — so a normal mock job leaves no observable pending window. To create
    one, run_job is patched to block on an Event; a second Python thread makes the
    POST /jobs call (and therefore blocks on it), while the main test thread polls
    for job creation, asserts the 404, then releases the event.
    """
    import app.main as main_module

    _force_mock_provider()
    fixture = make_fixtures.FIXTURES_DIR / "simple.pdf"

    release = threading.Event()
    original_run_job = main_module.run_job

    def blocking_run_job(*args, **kwargs):
        release.wait(timeout=10)
        return original_run_job(*args, **kwargs)

    main_module.run_job = blocking_run_job
    try:
        with _open_client() as client:
            post_result: dict = {}

            def do_post() -> None:
                with fixture.open("rb") as handle:
                    response = client.post(
                        "/jobs",
                        files=[("files", ("simple.pdf", handle, "application/pdf"))],
                        data={"src": "zh", "tgt": "fr"},
                    )
                post_result["response"] = response

            post_thread = threading.Thread(target=do_post)
            post_thread.start()

            job_id = None
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and job_id is None:
                with main_module._jobs_lock:
                    if main_module._jobs:
                        job_id = next(iter(main_module._jobs))
                if job_id is None:
                    time.sleep(0.05)
            check(job_id is not None, "job should be registered before run_job unblocks")

            download_response = client.get(f"/jobs/{job_id}/download")
            check(
                download_response.status_code == 404,
                f"expected 404 before completion, got {download_response.status_code}",
            )

            release.set()
            post_thread.join(timeout=10)

            _wait_for_done(client, job_id)
            client.get(f"/jobs/{job_id}/download")
    finally:
        main_module.run_job = original_run_job


# --------------------------------------------------------------------------- #
# 4. download after done -> valid zip, files present, dirs purged
# --------------------------------------------------------------------------- #


def test_download_after_done_returns_valid_zip_and_purges() -> None:
    """After done, download returns a valid zip and removes the job's directories."""
    _force_mock_provider()
    fixture = make_fixtures.FIXTURES_DIR / "simple.pdf"

    with _open_client() as client:
        with fixture.open("rb") as handle:
            response = client.post(
                "/jobs",
                files=[("files", ("simple.pdf", handle, "application/pdf"))],
                data={"src": "zh", "tgt": "fr"},
            )
        job_id = response.json()["job_id"]
        _wait_for_done(client, job_id)

        download_response = client.get(f"/jobs/{job_id}/download")
        check(download_response.status_code == 200, "download should return 200 when done")
        check(
            download_response.headers.get("content-type") == "application/zip",
            "download should be a zip",
        )

        archive = zipfile.ZipFile(io.BytesIO(download_response.content))
        names = archive.namelist()
        check(len(names) >= 1, "zip should contain at least one translated PDF")
        check(all(name.endswith(".pdf") for name in names), "zip entries should be PDFs")

        from app.main import OUTPUT_DIR, TMP_DIR

        check(not (TMP_DIR / job_id).exists(), "tmp/<job_id> should be purged after download")
        check(not (OUTPUT_DIR / job_id).exists(), "output/<job_id> should be purged after download")

        # A second download of the same job should now 404 (job forgotten).
        second = client.get(f"/jobs/{job_id}/download")
        check(second.status_code == 404, "downloading an already-purged job should 404")


# --------------------------------------------------------------------------- #
# 5. unknown job id -> 404
# --------------------------------------------------------------------------- #


def test_unknown_job_status_returns_404() -> None:
    """GET /jobs/<unknown> returns 404."""
    with _open_client() as client:
        response = client.get("/jobs/does-not-exist")
        check(response.status_code == 404, f"expected 404, got {response.status_code}")


# --------------------------------------------------------------------------- #
# 6. security hardening: purge on failure, no leaks in errors
# --------------------------------------------------------------------------- #


def test_failed_job_purges_directories() -> None:
    """A job failing mid-run leaves neither tmp/<id> nor output/<id> behind."""
    import app.main as main_module

    _force_mock_provider()
    fixture = make_fixtures.FIXTURES_DIR / "simple.pdf"
    original_run_job = main_module.run_job

    def exploding_run_job(files, src, tgt, config, progress_cb=None, job_id=None):
        # Simulate a crash after partial output was already written.
        out_dir = main_module.OUTPUT_DIR / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "partial.pdf").write_bytes(b"%PDF-partial")
        raise RuntimeError("secret-internal-detail should never surface")

    main_module.run_job = exploding_run_job  # type: ignore[assignment]
    try:
        with _open_client() as client:
            with fixture.open("rb") as handle:
                response = client.post(
                    "/jobs",
                    files=[("files", ("simple.pdf", handle, "application/pdf"))],
                    data={"src": "zh", "tgt": "fr"},
                )
            job_id = response.json()["job_id"]
            data = _wait_for_done(client, job_id)

            check(data["status"] == "error", f"job should fail, got {data['status']!r}")
            check(
                "secret-internal-detail" not in str(data),
                "the exception message must not surface in the job status",
            )
            check(
                not (main_module.TMP_DIR / job_id).exists(),
                "tmp/<job_id> should be purged after a failed job",
            )
            check(
                not (main_module.OUTPUT_DIR / job_id).exists(),
                "output/<job_id> should be purged after a failed job",
            )
    finally:
        main_module.run_job = original_run_job  # type: ignore[assignment]


def test_validation_error_hides_user_filename() -> None:
    """A rejected upload's 400 must not echo the user's file name back."""
    _force_mock_provider()
    sensitive_name = "SECRET_clientname_contract.pdf"

    with _open_client() as client:
        response = client.post(
            "/jobs",
            files=[("files", (sensitive_name, io.BytesIO(b"not a pdf at all"), "application/pdf"))],
            data={"src": "zh", "tgt": "fr"},
        )

    check(response.status_code == 400, f"expected 400, got {response.status_code}")
    check(
        "SECRET_clientname" not in response.text,
        f"the file name leaked into the error response: {response.text[:120]}",
    )
    check(
        "file 1" in response.json().get("detail", ""),
        "the error should reference the file by position instead",
    )


def test_unhandled_exception_returns_generic_500() -> None:
    """An unexpected exception yields a generic 500: no token, id or path leaks."""
    import app.main as main_module

    original_purge = main_module._purge_expired_jobs

    def exploding_purge() -> None:
        raise RuntimeError("token=super-secret client_id=internal-id C:\\private\\path")

    main_module._purge_expired_jobs = exploding_purge  # type: ignore[assignment]
    try:
        from app.main import app as fastapi_app

        with TestClient(fastapi_app, raise_server_exceptions=False) as client:
            response = client.get("/jobs/any-id")

        check(response.status_code == 500, f"expected 500, got {response.status_code}")
        check(
            response.json() == {"detail": "Internal server error."},
            f"expected a generic body, got {response.text[:120]}",
        )
        for fragment in ("super-secret", "internal-id", "private"):
            check(
                fragment not in response.text,
                f"exception content leaked into the 500 response: {fragment}",
            )
    finally:
        main_module._purge_expired_jobs = original_purge  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 7. authentication endpoints
# --------------------------------------------------------------------------- #


def test_auth_status_not_required_with_mock_provider() -> None:
    """With provider=mock the UI is told no sign-in is needed."""
    _force_mock_provider()

    with _open_client() as client:
        response = client.get("/auth/status")
        check(response.status_code == 200, f"expected 200, got {response.status_code}")
        data = response.json()

    check(data["provider"] == "mock", f"expected provider=mock, got {data['provider']!r}")
    check(data["required"] is False, "mock provider must not require authentication")
    check("access_token" not in response.text, "status must never expose a token")


def test_auth_status_and_start_with_gateway_provider() -> None:
    """With provider=gateway the flow is required, and /auth/start returns the user code."""
    import app.main as main_module

    config = app_config.load_config()
    original_provider = config.get("provider")
    config["provider"] = "gateway"

    original_is_authenticated = main_module.auth.is_authenticated
    original_flow_status = main_module.auth.flow_status
    original_start = main_module.auth.start_device_flow

    main_module.auth.is_authenticated = lambda: (False, None)  # type: ignore[assignment]
    main_module.auth.flow_status = lambda: ("idle", "")  # type: ignore[assignment]
    main_module.auth.start_device_flow = lambda: main_module.auth.DeviceCodeResponse(  # type: ignore[assignment]
        device_code="secret-device-code",
        user_code="WXYZ-1234",
        verification_uri="https://auth.example/device",
        verification_uri_complete="https://auth.example/device?code=WXYZ-1234",
        expires_in=900,
        interval=5.0,
    )

    try:
        with _open_client() as client:
            status = client.get("/auth/status")
            check(status.status_code == 200, f"expected 200, got {status.status_code}")
            status_data = status.json()
            check(status_data["required"] is True, "gateway provider must require authentication")
            check(status_data["authenticated"] is False, "should report no session yet")

            start = client.post("/auth/start")
            check(start.status_code == 200, f"expected 200, got {start.status_code}")
            start_data = start.json()

            check(start_data["user_code"] == "WXYZ-1234", "user_code should be returned")
            check(
                start_data["verification_uri"] == "https://auth.example/device",
                "verification_uri should be returned",
            )
            # The device_code is a bearer-equivalent secret: it must stay server-side.
            check(
                "secret-device-code" not in start.text,
                "the device_code must never be sent to the browser",
            )
    finally:
        main_module.auth.is_authenticated = original_is_authenticated  # type: ignore[assignment]
        main_module.auth.flow_status = original_flow_status  # type: ignore[assignment]
        main_module.auth.start_device_flow = original_start  # type: ignore[assignment]
        if original_provider is not None:
            config["provider"] = original_provider


def test_auth_start_reports_missing_configuration() -> None:
    """A misconfigured .env surfaces as a 503 with an actionable message."""
    import app.main as main_module

    original_start = main_module.auth.start_device_flow

    def missing_env() -> None:
        raise main_module.auth.AuthError("GATEWAY_DEVICE_ENDPOINT is not set")

    main_module.auth.start_device_flow = missing_env  # type: ignore[assignment]

    try:
        with _open_client() as client:
            response = client.post("/auth/start")
            check(response.status_code == 503, f"expected 503, got {response.status_code}")
            check(
                "GATEWAY_DEVICE_ENDPOINT" in response.json().get("detail", ""),
                "the 503 should name the missing variable",
            )
    finally:
        main_module.auth.start_device_flow = original_start  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def _cleanup() -> None:
    """Remove the shared translation cache populated by these tests."""
    default_cache = ROOT / "tmp" / "cache.json"
    if default_cache.is_file():
        default_cache.unlink()


def main() -> int:
    """Run every test_* function in this module."""
    app_config.force_utf8_stdout()
    make_fixtures.main([])

    tests = [
        test_create_job_and_poll_to_done,
        test_too_many_files_rejected,
        test_invalid_file_content_rejected,
        test_download_before_done_returns_404,
        test_download_after_done_returns_valid_zip_and_purges,
        test_unknown_job_status_returns_404,
        test_failed_job_purges_directories,
        test_validation_error_hides_user_filename,
        test_unhandled_exception_returns_generic_500,
        test_auth_status_not_required_with_mock_provider,
        test_auth_status_and_start_with_gateway_provider,
        test_auth_start_reports_missing_configuration,
    ]

    for test in tests:
        failures_before = len(FAILURES)
        try:
            test()
        except Exception as error:  # noqa: BLE001 - report and continue
            FAILURES.append(f"{test.__name__} raised {type(error).__name__}: {error}")
        status = "OK" if len(FAILURES) == failures_before else "FAIL"
        print(f"[{status}] {test.__name__}")

    _cleanup()

    print(f"\n{len(tests)} tests, {len(FAILURES)} failure(s)")
    for failure in FAILURES:
        print(f"  - {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
