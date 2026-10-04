"""Tests for app.llm.tracing.TracingProvider (JSONL trace of LLM calls).

No network access. Run: python tests/test_llm_tracing.py
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config as app_config  # noqa: E402
from app.config import ModelPrice  # noqa: E402
from app.llm.base import LLMRequest, LLMResponse, request_fingerprint  # noqa: E402
from app.llm.tracing import TracingProvider  # noqa: E402

FAILURES: list[str] = []

HAIKU = "claude-haiku-4-5-20251001"
PRICES = {HAIKU: ModelPrice(input=1.0, output=5.0)}
SOURCE_TEXT = "SECRET-SOURCE-TEXT-xyz"
OUTPUT_TEXT = "SECRET-OUTPUT-TEXT-abc"
FIELDS = {
    "timestamp",
    "input_id",
    "prompt_version",
    "model",
    "params",
    "context_sources",
    "output",
    "duration_ms",
    "input_tokens",
    "output_tokens",
    "estimated_cost_usd",
    "cache_hit",
    "status",
    "error_type",
}


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


class SpyProvider:
    """Inner provider returning a canned response or raising."""

    def __init__(self, response: LLMResponse | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _request(text: str = SOURCE_TEXT, version: str = "v1", model: str = HAIKU) -> LLMRequest:
    return LLMRequest(
        messages=[{"role": "user", "content": text}],
        model=model,
        temperature=0.0,
        max_tokens=100,
        prompt_version=version,
    )


def _response(model: str = HAIKU, cache_hit: bool = False) -> LLMResponse:
    return LLMResponse(
        text=OUTPUT_TEXT,
        model=model,
        input_tokens=1000,
        output_tokens=1000,
        cache_hit=cache_hit,
    )


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_two_calls_two_lines() -> None:
    """Each call appends one valid JSON line carrying every field."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sub" / "trace.jsonl"
        provider = TracingProvider(SpyProvider(_response()), path, PRICES)
        provider.complete(_request())
        provider.complete(_request())
        records = _lines(path)
        check(len(records) == 2, f"expected 2 lines, got {len(records)}")
        for record in records:
            check(set(record) == FIELDS, f"fields: {sorted(set(record) ^ FIELDS)}")
            check(record["status"] == "ok", "status")
            check(record["error_type"] is None, "error_type")
            check(record["output"] == OUTPUT_TEXT, "output")
            check(record["params"] == {"temperature": 0.0, "max_tokens": 100}, "params")
            check(record["timestamp"].endswith("Z"), "timestamp")
            check(isinstance(record["duration_ms"], int), "duration_ms")
            check(record["cache_hit"] is False, "cache_hit")
        raw = path.read_text(encoding="utf-8")
        check(SOURCE_TEXT not in raw, "input messages must not be written")


def test_input_id() -> None:
    """input_id is stable for identical requests and changes with content."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "trace.jsonl"
        provider = TracingProvider(SpyProvider(_response()), path, PRICES)
        provider.complete(_request())
        provider.complete(_request())
        provider.complete(_request(version="v2"))
        provider.complete(_request(text="other"))
        ids = [record["input_id"] for record in _lines(path)]
        check(ids[0] == ids[1], "identical requests must share input_id")
        check(ids[0] == request_fingerprint(_request()), "input_id is the fingerprint")
        check(ids[2] != ids[0], "prompt_version must change input_id")
        check(ids[3] != ids[0], "message must change input_id")


def test_cost() -> None:
    """Cost from the price table; null for unknown model; 0.0 on cache hit."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "trace.jsonl"
        TracingProvider(SpyProvider(_response()), path, PRICES).complete(_request())
        TracingProvider(SpyProvider(_response(model="unknown")), path, PRICES).complete(_request())
        TracingProvider(SpyProvider(_response(cache_hit=True)), path, PRICES).complete(_request())
        known, unknown, hit = _lines(path)
        check(known["estimated_cost_usd"] == 0.006, f"cost: {known['estimated_cost_usd']}")
        check(unknown["estimated_cost_usd"] is None, "unknown model cost must be null")
        check(hit["cache_hit"] is True, "cache_hit flag")
        check(hit["estimated_cost_usd"] == 0.0, f"cache hit cost: {hit['estimated_cost_usd']}")


def test_error_is_traced_and_reraised() -> None:
    """An inner exception is traced as an error, then re-raised unchanged."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "trace.jsonl"
        boom = ValueError("boom")
        provider = TracingProvider(SpyProvider(error=boom), path, PRICES)
        raised = None
        try:
            provider.complete(_request(model="req-model"))
        except ValueError as error:
            raised = error
        check(raised is boom, "original exception must be re-raised")
        (record,) = _lines(path)
        check(record["status"] == "error", "status")
        check(record["error_type"] == "ValueError", f"error_type: {record['error_type']}")
        check(record["model"] == "req-model", "model falls back to the request model")
        check(record["output"] is None, "output must be null")
        check(record["input_tokens"] == 0 and record["output_tokens"] == 0, "tokens")
        check(record["cache_hit"] is False, "cache_hit")


def test_unwritable_path_does_not_fail() -> None:
    """An OSError while writing the trace never fails the call."""
    with tempfile.TemporaryDirectory() as tmp:
        blocker = Path(tmp) / "file"
        blocker.write_text("x", encoding="utf-8")
        provider = TracingProvider(SpyProvider(_response()), blocker / "trace.jsonl", PRICES)
        response = provider.complete(_request())
        check(response.text == OUTPUT_TEXT, "call must still return the response")


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_no_content_in_logs() -> None:
    """Neither source nor output text reaches the 'app' logger."""
    logger = logging.getLogger("app")
    handler = _Capture()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            good = TracingProvider(SpyProvider(_response()), Path(tmp) / "t.jsonl", PRICES)
            good.complete(_request())
            failing = TracingProvider(
                SpyProvider(error=RuntimeError(SOURCE_TEXT)), Path(tmp) / "t.jsonl", PRICES
            )
            try:
                failing.complete(_request())
            except RuntimeError:
                pass
            blocker = Path(tmp) / "file"
            blocker.write_text("x", encoding="utf-8")
            bad = TracingProvider(SpyProvider(_response()), blocker / "t.jsonl", PRICES)
            bad.complete(_request())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    joined = "\n".join(handler.messages)
    check(any("trace write failed" in m for m in handler.messages), "warning not logged")
    check(SOURCE_TEXT not in joined, "source text leaked into logs")
    check(OUTPUT_TEXT not in joined, "output text leaked into logs")


def main() -> int:
    """Run every test_* function in this module."""
    app_config.force_utf8_stdout()
    tests = [
        test_two_calls_two_lines,
        test_input_id,
        test_cost,
        test_error_is_traced_and_reraised,
        test_unwritable_path_does_not_fail,
        test_no_content_in_logs,
    ]
    for test in tests:
        before = len(FAILURES)
        try:
            test()
        except Exception as error:  # noqa: BLE001
            FAILURES.append(f"{test.__name__} raised {type(error).__name__}: {error}")
        print(f"[{'OK' if len(FAILURES) == before else 'FAIL'}] {test.__name__}")
    print(f"\n{len(tests)} tests, {len(FAILURES)} failure(s)")
    for failure in FAILURES:
        print(f"  - {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
