"""End-to-end verification tests with the real numbers/dates checks.

No network access: FakeProvider stands in for the model, the block cache lives in a temp
directory, tracing is disabled and job output is cleaned.
Run: python tests/test_verification_e2e.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config as app_config  # noqa: E402
from app import pipeline  # noqa: E402
from app.cache import TranslationCache  # noqa: E402
from app.llm.base import LLMRequest, LLMResponse  # noqa: E402
from app.llm.fake_provider import FakeProvider  # noqa: E402
from app.llm.translator import LLMTranslator  # noqa: E402
from app.verification import VerificationConfig  # noqa: E402
from tests import make_fixtures  # noqa: E402

FAILURES: list[str] = []
_JOB_DIRS: list[Path] = []
ENABLED = VerificationConfig(enabled=True, max_retries=1)
AMOUNTS = ["收入增长15%", "人民幣7,518億元"]
CLEAN_BLOCKS = ["2024年3月12日", "15%", "1,234.5", "合同于2024年12月1日生效,金额1,234.5元"]


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


class ScriptedProvider:
    """Answers with one FakeProvider per call (the last one repeats) and records requests."""

    def __init__(self, *scripts: FakeProvider) -> None:
        self._scripts = list(scripts)
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        index = min(len(self.requests), len(self._scripts) - 1)
        self.requests.append(request)
        return self._scripts[index].complete(request)


def _last_user_text(request: LLMRequest) -> str:
    return next(m["content"] for m in reversed(request.messages) if m["role"] == "user")


def test_drop_numbers_retried_then_flagged() -> None:
    provider = ScriptedProvider(FakeProvider(drop_numbers=True))
    translator = LLMTranslator(provider, model="fake-model", verification=ENABLED)
    translator.translate_blocks(AMOUNTS, "zh", "en")
    check(len(provider.requests) == 2, f"expected 2 calls, got {len(provider.requests)}")
    if len(provider.requests) < 2:
        return
    second = _last_user_text(provider.requests[1])
    check("[numbers]" in second, f"second request lacks a numbers correction: {second}")
    last = translator.last_verification
    check(last is not None, "last_verification must be set")
    if last is None:
        return
    check(len(last.flagged) == 2, f"both segments must be flagged, got {len(last.flagged)}")
    check(all(o.failed == ("numbers",) for o in last.flagged), "failed must be ['numbers']")
    check(last.retried == 2, f"retried {last.retried}")


def test_drop_numbers_then_clean_recovers() -> None:
    provider = ScriptedProvider(FakeProvider(drop_numbers=True), FakeProvider())
    translator = LLMTranslator(provider, model="fake-model", verification=ENABLED)
    result = translator.translate_blocks(AMOUNTS, "zh", "en")
    check(len(provider.requests) == 2, "exactly one retry expected")
    last = translator.last_verification
    check(last is not None and not last.flagged, "recovered batch must not be flagged")
    check(last is not None and last.retried == 2, "both segments retried")
    check(result == [f"[en] {b}" for b in AMOUNTS], f"unexpected output {result}")


def test_clean_output_has_no_false_positive() -> None:
    for tgt in ("en", "fr"):
        provider = ScriptedProvider(FakeProvider())
        translator = LLMTranslator(provider, model="fake-model", verification=ENABLED)
        result = translator.translate_blocks(CLEAN_BLOCKS, "zh", tgt)
        check(len(provider.requests) == 1, f"zh->{tgt}: one call expected")
        last = translator.last_verification
        check(last is not None and not last.flagged, f"zh->{tgt}: false positive flagged")
        check(
            last is not None and last.retried == 0 and last.verified == len(CLEAN_BLOCKS),
            f"zh->{tgt}: all segments must pass first time",
        )
        check("15%" in result[1] and "1,234.5" in result[2], f"zh->{tgt}: numbers kept")
        if tgt == "en":
            check("12 March 2024" in result[0], f"date not rendered: {result[0]}")
        else:
            check("12 mars 2024" in result[0], f"date not rendered: {result[0]}")


def test_run_job_every_flag_is_explained() -> None:
    fixture = make_fixtures.FIXTURES_DIR / "contract_zh.pdf"
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "cache.json"
        orig_get, orig_cache = pipeline.get_translator, pipeline.TranslationCache
        pipeline.get_translator = lambda config: LLMTranslator(  # type: ignore[assignment]
            FakeProvider(), model="fake-model"
        )
        pipeline.TranslationCache = lambda **scope: TranslationCache(cache_path, **scope)  # type: ignore[assignment]
        try:
            for tgt in ("en", "fr"):
                config = dict(app_config.load_config())
                config["provider"] = "fake"
                config["tracing"] = {"enabled": False}
                config["verification"] = {"enabled": True, "max_retries": 1}
                result = pipeline.run_job([fixture], "zh", tgt, config)
                _JOB_DIRS.append(pipeline.OUTPUT_DIR / result.job_id)
                check(result.stats.verified > 0, f"zh->{tgt}: verification must have run")
                if result.review_path:
                    data = json.loads(Path(result.review_path).read_text(encoding="utf-8"))
                    for entry in data["segments"]:
                        check(bool(entry["failed_checks"]), f"unexplained flag {entry}")
                check(result.review_count == 0, f"zh->{tgt}: {result.review_count} flagged")
        finally:
            pipeline.get_translator = orig_get  # type: ignore[assignment]
            pipeline.TranslationCache = orig_cache  # type: ignore[assignment]


def _cleanup() -> None:
    for path in _JOB_DIRS:
        shutil.rmtree(path, ignore_errors=True)


def main() -> int:
    tests = [
        test_drop_numbers_retried_then_flagged,
        test_drop_numbers_then_clean_recovers,
        test_clean_output_has_no_false_positive,
        test_run_job_every_flag_is_explained,
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
