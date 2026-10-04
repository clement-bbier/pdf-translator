"""Tests for CachedProvider and the model/prompt-scoped TranslationCache.

No network access. Run: python tests/test_llm_cache.py
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config as app_config  # noqa: E402
from app.cache import TranslationCache  # noqa: E402
from app.llm.base import LLMRequest, LLMResponse  # noqa: E402
from app.llm.caching import CachedProvider  # noqa: E402
from app.llm.errors import TranslationError  # noqa: E402
from app.llm.factory import build_provider  # noqa: E402
from app.llm.fake_provider import FakeProvider  # noqa: E402
from app.llm.translator import LLMTranslator  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


class SpyProvider:
    """Counts calls; optionally raises."""

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return LLMResponse(
            text=f"answer-{self.calls}",
            model=request.model,
            input_tokens=3,
            output_tokens=4,
            finish_reason="stop",
        )


def _request() -> LLMRequest:
    return LLMRequest(
        messages=[{"role": "user", "content": "hello"}],
        model="m1",
        temperature=0.0,
        max_tokens=100,
        prompt_version="v1",
    )


def test_hit_and_persistence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "c.json"
        spy = SpyProvider()
        provider = CachedProvider(spy, path)
        first = provider.complete(_request())
        second = provider.complete(_request())
        check(spy.calls == 1, "same request twice must call inner once")
        check(not first.cache_hit, "first response is not a hit")
        check(second.cache_hit, "second response is a hit")
        check(second.text == first.text, "hit text identical")
        check(second.output_tokens == 4 and second.finish_reason == "stop", "metadata kept")
        check(not Path(str(path) + ".tmp").exists(), "no leftover tmp file")

        spy2 = SpyProvider()
        third = CachedProvider(spy2, path).complete(_request())
        check(third.cache_hit and spy2.calls == 0, "new provider on same file still hits")
        check(third.text == first.text, "persisted text identical")


def test_key_sensitivity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spy = SpyProvider()
        provider = CachedProvider(spy, Path(tmp) / "c.json")
        provider.complete(_request())
        base = _request()
        variants = {
            "model": dataclasses.replace(base, model="m2"),
            "prompt_version": dataclasses.replace(base, prompt_version="v2"),
            "temperature": dataclasses.replace(base, temperature=0.5),
            "max_tokens": dataclasses.replace(base, max_tokens=101),
            "message": dataclasses.replace(base, messages=[{"role": "user", "content": "hellO"}]),
        }
        for name, variant in variants.items():
            before = spy.calls
            response = provider.complete(variant)
            check(spy.calls == before + 1 and not response.cache_hit, f"{name} change must miss")


def test_exceptions_not_cached() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spy = SpyProvider(fail=True)
        provider = CachedProvider(spy, Path(tmp) / "c.json")
        for _ in range(2):
            try:
                provider.complete(_request())
            except RuntimeError:
                pass
        check(spy.calls == 2, "failures must not be cached")


def test_corrupt_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "c.json"
        path.write_text("{not json", encoding="utf-8")
        spy = SpyProvider()
        provider = CachedProvider(spy, path)
        response = provider.complete(_request())
        check(spy.calls == 1 and not response.cache_hit, "corrupt file starts empty")
        check(provider.complete(_request()).cache_hit, "cache works after recovery")


def test_translation_cache_scoping() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.json"
        TranslationCache(path, model="a", prompt_version="v1").set_many({"x": "y"}, "zh", "en")
        check(
            TranslationCache(path, model="a", prompt_version="v1").get_many(["x"], "zh", "en")
            == {"x": "y"},
            "same model/prompt hits",
        )
        check(
            TranslationCache(path, model="b", prompt_version="v1").get_many(["x"], "zh", "en")
            == {},
            "other model misses",
        )
        check(
            TranslationCache(path, model="a", prompt_version="v2").get_many(["x"], "zh", "en")
            == {},
            "other prompt version misses",
        )
        default_path = Path(tmp) / "d.json"
        default = TranslationCache(default_path)
        default.set_many({"k": "v"}, "en", "fr")
        check(
            TranslationCache(default_path).get_many(["k"], "en", "fr") == {"k": "v"},
            "default cache round-trips",
        )
        check(len(default) == 1, "len unchanged")


class CountingFake:
    """Wraps a FakeProvider and counts inner calls."""

    def __init__(self, **kwargs: bool) -> None:
        self.calls = 0
        self._fake = FakeProvider(**kwargs)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return self._fake.complete(request)


def test_rejected_response_is_invalidated() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "c.json"
        bad = CountingFake(invalid_json=True)
        translator = LLMTranslator(CachedProvider(bad, path), model="fake-model")
        for _ in range(2):
            try:
                translator.translate_blocks(["hello"], "en", "fr")
                check(False, "invalid JSON must raise TranslationError")
            except TranslationError:
                pass
        check(bad.calls == 2, "rejected response must not stay cached")

        good = CountingFake()
        translator = LLMTranslator(CachedProvider(good, path), model="fake-model")
        first = translator.translate_blocks(["hello"], "en", "fr")
        second = translator.translate_blocks(["hello"], "en", "fr")
        check(good.calls == 1 and first == second, "valid response still hits the cache")


def test_wrapper_order_trace_and_cache() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        trace = Path(tmp) / "trace.jsonl"
        config = {
            "cache": {"enabled": True, "path": str(Path(tmp) / "c.json")},
            "tracing": {"enabled": True, "path": str(trace)},
        }
        provider = build_provider("fake", config)
        request = _request()
        provider.complete(request)
        provider.complete(request)
        lines = [json.loads(x) for x in trace.read_text(encoding="utf-8").splitlines()]
        check(len(lines) == 2, "two trace lines")
        check(
            lines[0]["cache_hit"] is False and lines[0]["estimated_cost_usd"] is None,
            "first call: miss, unpriced model has null cost",
        )
        check(
            lines[1]["cache_hit"] is True and lines[1]["estimated_cost_usd"] == 0.0,
            "second call: hit, zero cost",
        )


def main() -> int:
    """Run every test_* function in this module."""
    app_config.force_utf8_stdout()
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        before = len(FAILURES)
        try:
            test()
        except Exception as error:  # noqa: BLE001 - report and continue
            FAILURES.append(f"{test.__name__} raised {type(error).__name__}: {error}")
        print(f"[{'OK' if len(FAILURES) == before else 'FAIL'}] {test.__name__}")
    print(f"\n{len(tests)} tests, {len(FAILURES)} failure(s)")
    for failure in FAILURES:
        print(f"  - {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
