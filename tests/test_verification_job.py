"""Job-level tests for verification: review.json, warnings, stats, cache, web status and zip.

No network access; a scripted fake provider stands in for the model, the block cache
lives in a temp directory and tracing is disabled. Run: python tests/test_verification_job.py
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import config as app_config  # noqa: E402
from app import pipeline  # noqa: E402
from app.cache import TranslationCache  # noqa: E402
from app.llm.base import LLMRequest, LLMResponse  # noqa: E402
from app.llm.fake_provider import FakeProvider  # noqa: E402
from app.llm.translator import LLMTranslator  # noqa: E402
from app.translator import MockTranslator  # noqa: E402
from tests import make_fixtures  # noqa: E402

FAILURES: list[str] = []
_JOB_DIRS: list[Path] = []
FIXTURE = make_fixtures.FIXTURES_DIR / "simple.pdf"


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


class FaultyProvider:
    """FakeProvider with wrong_segment_count on every call; counts the calls."""

    def __init__(self) -> None:
        self._fake = FakeProvider(wrong_segment_count=True)
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return self._fake.complete(request)


class _Patched:
    """Patch get_translator and the block cache for the duration of a with block."""

    def __init__(self, provider: object | None, cache_dir: Path) -> None:
        self._provider = provider
        self._cache_dir = cache_dir

    def __enter__(self) -> None:
        self._orig_get = pipeline.get_translator
        self._orig_cache = pipeline.TranslationCache
        provider = self._provider
        cache_path = self._cache_dir / "cache.json"
        if provider is not None:
            pipeline.get_translator = lambda config: LLMTranslator(  # type: ignore[assignment]
                provider,  # type: ignore[arg-type]
                model="fake-model",
            )
        pipeline.TranslationCache = lambda: TranslationCache(cache_path)  # type: ignore[assignment]

    def __exit__(self, *exc: object) -> None:
        pipeline.get_translator = self._orig_get  # type: ignore[assignment]
        pipeline.TranslationCache = self._orig_cache  # type: ignore[assignment]


def _config(*, enabled: bool, provider: str = "fake") -> dict:
    config = dict(app_config.load_config())
    config["provider"] = provider
    config["batch_size"] = 50
    config["tracing"] = {"enabled": False}
    config["verification"] = {"enabled": enabled, "max_retries": 1}
    return config


def _track(result: pipeline.JobResult) -> None:
    _JOB_DIRS.append(pipeline.OUTPUT_DIR / result.job_id)


def test_flagged_segments_in_review_json() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        provider = FaultyProvider()
        with _Patched(provider, Path(tmp)):
            result = pipeline.run_job([FIXTURE], "zh", "en", _config(enabled=True))
            _track(result)
            cache = TranslationCache(Path(tmp) / "cache.json")
            check(result.review_count > 0, "review_count must be > 0")
            check(result.stats.flagged > 0, "stats.flagged must be > 0")
            check(result.stats.verified > 0, "stats.verified must be > 0")
            check(result.stats.retried > 0, "stats.retried must be > 0")
            check("flagged=" in result.stats.summary(), "summary must show the counters")
            check(provider.calls >= 2, "a failing batch must be retried")
            check(
                any("flagged for human review" in w for w in result.warnings),
                f"missing review warning in {result.warnings}",
            )
            review_warning = next(w for w in result.warnings if "human review" in w)
            check("[" not in review_warning, "the warning carries counts only")
            check(result.review_path is not None, "review_path must be set")
            path = Path(result.review_path or "")
            check(path.is_file() and path.parent.name == result.job_id, "review.json in job dir")
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in (
                "job_id",
                "source_language",
                "target_language",
                "flagged_count",
                "segments",
            ):
                check(key in data, f"review.json misses {key}")
            check(data["flagged_count"] == result.review_count == len(data["segments"]), "count")
            check(data["source_language"] == "zh" and data["target_language"] == "en", "langs")
            entry = data["segments"][0]
            for key in ("segment_id", "page", "file", "source", "translation", "failed_checks"):
                check(key in entry, f"review entry misses {key}")
            check(entry["failed_checks"] == ["segment_count"], f"{entry['failed_checks']}")
            check(entry["translation"] is None, "a segment missing everywhere has no translation")
            check(entry["page"] >= 1, "page is 1-based")
            flagged_sources = [segment["source"] for segment in data["segments"]]
            check(
                not cache.get_many(flagged_sources, "zh", "en"),
                "flagged texts must not be cached",
            )
            check(len(cache) > 0, "unflagged texts are still cached")


def test_disabled_and_mock_have_no_review() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with _Patched(None, Path(tmp)):
            result = pipeline.run_job([FIXTURE], "zh", "fr", _config(enabled=True, provider="mock"))
            _track(result)
            check(result.review_count == 0, "mock must never be flagged")
            check(result.review_path is None, "no review.json for the mock")
            check(
                not (pipeline.OUTPUT_DIR / result.job_id / "review.json").exists(),
                "review.json must not exist",
            )
            check("verified" not in result.stats.summary(), "mock summary unchanged")
            check(isinstance(MockTranslator(), MockTranslator), "sanity")

        provider = FaultyProvider()
        with _Patched(provider, Path(tmp) / "second"):
            result = pipeline.run_job([FIXTURE], "zh", "en", _config(enabled=False))
            _track(result)
            check(provider.calls > 0, "provider used")
            check(result.review_count == 0, "disabled verification flags nothing")
            check("verified" not in result.stats.summary(), "disabled summary unchanged")


def test_web_status_zip_and_notice() -> None:
    from app import main as main_module

    with tempfile.TemporaryDirectory() as tmp:
        config = app_config.load_config()
        saved = {k: config.get(k) for k in ("provider", "tracing", "verification", "batch_size")}
        config["tracing"] = {"enabled": False}
        config["verification"] = {"enabled": True, "max_retries": 1}
        config["batch_size"] = 50
        try:
            with _Patched(FaultyProvider(), Path(tmp)), TestClient(main_module.app) as client:
                config["provider"] = "fake"
                with FIXTURE.open("rb") as handle:
                    response = client.post(
                        "/jobs",
                        files=[("files", ("simple.pdf", handle, "application/pdf"))],
                        data={"src": "zh", "tgt": "en"},
                    )
                job_id = response.json()["job_id"]
                _JOB_DIRS.append(pipeline.OUTPUT_DIR / job_id)
                deadline = time.monotonic() + 60
                data: dict = {}
                while time.monotonic() < deadline:
                    data = client.get(f"/jobs/{job_id}").json()
                    if data["status"] in ("done", "error"):
                        break
                    time.sleep(0.2)
                check(data.get("status") == "done", f"job status {data.get('status')}")
                check(data.get("review_count", 0) > 0, f"review_count missing in {data}")
                download = client.get(f"/jobs/{job_id}/download")
                archive = zipfile.ZipFile(io.BytesIO(download.content))
                check("review.json" in archive.namelist(), f"zip entries {archive.namelist()}")
                review = json.loads(archive.read("review.json").decode("utf-8"))
                check(review["flagged_count"] == data["review_count"], "zip review count")
                page = client.get("/").text
                check("need human review" in page, "UI must carry the review notice")
                check('id="review"' in page, "UI must have the review element")
        finally:
            for key, value in saved.items():
                if value is None:
                    config.pop(key, None)
                else:
                    config[key] = value


def _cleanup() -> None:
    for path in _JOB_DIRS:
        shutil.rmtree(path, ignore_errors=True)


def main() -> int:
    tests = [
        test_flagged_segments_in_review_json,
        test_disabled_and_mock_have_no_review,
        test_web_status_zip_and_notice,
    ]
    for test in tests:
        before = len(FAILURES)
        try:
            test()
        except Exception as error:  # noqa: BLE001 - report and continue
            FAILURES.append(f"{test.__name__} raised {type(error).__name__}: {error}")
        print(f"[{'OK' if len(FAILURES) == before else 'FAIL'}] {test.__name__}")
    _cleanup()
    print(f"\n{len(tests)} tests, {len(FAILURES)} failure(s)")
    for failure in FAILURES:
        print(f"  - {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
