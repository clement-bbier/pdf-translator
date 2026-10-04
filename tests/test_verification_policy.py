"""Tests for the retry-then-flag policy in LLMTranslator, the retry prompt and the trace.

No network access, nothing written to the repo. Run: python tests/test_verification_policy.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.llm.base import LLMRequest, LLMResponse, request_fingerprint  # noqa: E402
from app.llm.fake_provider import FakeProvider  # noqa: E402
from app.llm.translator import LLMTranslator  # noqa: E402
from app.translator import TranslationError  # noqa: E402
from app.verification import VerificationConfig  # noqa: E402
from app.verification.findings import Finding  # noqa: E402
from app.verification.retry_prompt import (  # noqa: E402
    load_retry_template,
    render_corrections,
    retry_messages,
)

FAILURES: list[str] = []

BLOCKS = ["甲方", "乙方", "丙方"]
ENABLED = VerificationConfig(enabled=True, max_retries=1)


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


class TermStub:
    """A term provider that requires a term the fake never emits."""

    def required_terms(self, segment: str, src: str, tgt: str) -> list[str]:
        return ["Party A"]


def _last_user_text(request: LLMRequest) -> str:
    return next(m["content"] for m in reversed(request.messages) if m["role"] == "user")


def test_passing_batch() -> None:
    provider = ScriptedProvider(FakeProvider())
    translator = LLMTranslator(provider, model="fake-model", verification=ENABLED)
    result = translator.translate_blocks(BLOCKS, "zh", "en")
    check(len(provider.requests) == 1, "passing batch must make exactly one call")
    check(result == [f"[en] {b}" for b in BLOCKS], f"unexpected output {result}")
    last = translator.last_verification
    check(last is not None and last.attempts == 1, "one attempt expected")
    check(last is not None and last.verified == 3, "3 segments verified")
    check(last is not None and last.retried == 0, "no segment retried")
    check(last is not None and not last.flagged, "nothing flagged")


def test_retry_carries_corrections_and_recovers() -> None:
    provider = ScriptedProvider(FakeProvider(wrong_segment_count=True), FakeProvider())
    translator = LLMTranslator(provider, model="fake-model", verification=ENABLED)
    result = translator.translate_blocks(BLOCKS, "zh", "en")
    check(len(provider.requests) == 2, f"expected 2 calls, got {len(provider.requests)}")
    second = _last_user_text(provider.requests[1])
    check("- segment 2 [segment_count]: segment missing from the answer" in second, second)
    check("failed automatic checks" in second, "retry template text missing")
    check("Target language: en" in second, "second request lost the target language")
    check("{corrections}" not in second, "placeholder left in the prompt")
    check(_last_user_text(provider.requests[0]) != second, "retry must differ from first call")
    last = translator.last_verification
    check(last is not None and not last.flagged, "recovered batch must not be flagged")
    check(last is not None and last.retried == 3, "3 segments retried")
    check(last is not None and last.attempts == 2, "two attempts expected")
    check(result == [f"[en] {b}" for b in BLOCKS], f"unexpected output {result}")


def test_failing_twice_is_flagged() -> None:
    provider = ScriptedProvider(FakeProvider(wrong_segment_count=True))
    translator = LLMTranslator(provider, model="fake-model", verification=ENABLED)
    result = translator.translate_blocks(BLOCKS, "zh", "en")
    check(len(provider.requests) == 2, "must stop after one retry")
    last = translator.last_verification
    check(last is not None, "last_verification must be set")
    if last is None:
        return
    flagged = last.flagged
    check([o.key for o in flagged] == ["2"], f"flagged keys {[o.key for o in flagged]}")
    check(flagged[0].translation is None, "missing segment has no translation")
    check(flagged[0].failed == ("segment_count",), f"failed {flagged[0].failed}")
    check(result[2] == BLOCKS[2], "missing translation falls back to the source")
    check(result[0] == "[en] 甲方", "passing segments keep their translation")


def test_invalid_json_twice_raises() -> None:
    provider = ScriptedProvider(FakeProvider(invalid_json=True))
    translator = LLMTranslator(provider, model="fake-model", verification=ENABLED)
    try:
        translator.translate_blocks(BLOCKS, "zh", "en")
    except TranslationError as error:
        check("2 attempt(s)" in str(error), f"unexpected message {error}")
    else:
        check(False, "invalid JSON twice must raise TranslationError")
    check(len(provider.requests) == 2, "invalid JSON is retried once")
    check(translator.last_verification is None, "no verification result after a raise")


def test_invalid_json_then_clean_recovers() -> None:
    provider = ScriptedProvider(FakeProvider(invalid_json=True), FakeProvider())
    translator = LLMTranslator(provider, model="fake-model", verification=ENABLED)
    result = translator.translate_blocks(BLOCKS, "zh", "en")
    check(result == [f"[en] {b}" for b in BLOCKS], f"unexpected output {result}")
    second = _last_user_text(provider.requests[1])
    check("- whole answer [json_valid]" in second, second)


def test_glossary_term_retried_then_flagged() -> None:
    provider = ScriptedProvider(FakeProvider())
    translator = LLMTranslator(
        provider, model="fake-model", verification=ENABLED, term_providers=[TermStub()]
    )
    translator.translate_blocks(BLOCKS, "zh", "en")
    check(len(provider.requests) == 2, "missing glossary term must trigger a retry")
    last = translator.last_verification
    check(last is not None and len(last.flagged) == 3, "all segments must be flagged")
    check(
        last is not None and all("glossary_terms" in o.failed for o in last.flagged),
        "flag must name glossary_terms",
    )
    check(
        last is not None and all(o.translation is not None for o in last.flagged),
        "flagged segments keep their translation",
    )


def test_zero_retries_flags_without_retry() -> None:
    provider = ScriptedProvider(FakeProvider(wrong_segment_count=True))
    translator = LLMTranslator(
        provider, model="fake-model", verification=VerificationConfig(True, 0)
    )
    translator.translate_blocks(BLOCKS, "zh", "en")
    check(len(provider.requests) == 1, "max_retries=0 must not retry")
    last = translator.last_verification
    check(last is not None and len(last.flagged) == 1, "segment still flagged")
    check(last is not None and last.retried == 0, "nothing retried")


def test_disabled_is_identical_to_plain() -> None:
    disabled_options = [None, VerificationConfig(False, 1)]
    for label, fault in [
        ("clean", {}),
        ("wrong_count", {"wrong_segment_count": True}),
        ("invalid_json", {"invalid_json": True}),
    ]:
        plain_provider = ScriptedProvider(FakeProvider(**fault))
        plain = LLMTranslator(plain_provider, model="fake-model")
        try:
            expected: object = plain.translate_blocks(BLOCKS, "zh", "en")
        except TranslationError as error:
            expected = f"error: {error}"
        expected_ids = [request_fingerprint(r) for r in plain_provider.requests]
        for option in disabled_options:
            provider = ScriptedProvider(FakeProvider(**fault))
            translator = LLMTranslator(provider, model="fake-model", verification=option)
            try:
                actual: object = translator.translate_blocks(BLOCKS, "zh", "en")
            except TranslationError as error:
                actual = f"error: {error}"
            ids = [request_fingerprint(r) for r in provider.requests]
            check(actual == expected, f"{label}/{option}: output differs")
            check(ids == expected_ids, f"{label}/{option}: requests differ")
            check(translator.last_verification is None, f"{label}/{option}: verification ran")


def test_trace_line_is_content_free() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sub" / "verification.jsonl"
        provider = ScriptedProvider(FakeProvider(wrong_segment_count=True))
        translator = LLMTranslator(provider, model="fake-model", verification=ENABLED)
        translator.enable_verification(ENABLED, trace_path=path)
        translator.translate_blocks(BLOCKS, "zh", "en")
        lines = path.read_text(encoding="utf-8").splitlines()
        check(len(lines) == 1, f"one line per batch expected, got {len(lines)}")
        record = json.loads(lines[0])
        for field in (
            "timestamp",
            "kind",
            "model",
            "prompt_version",
            "input_ids",
            "attempts",
            "segments",
        ):
            check(field in record, f"trace line misses {field}")
        check(record["kind"] == "verification", "kind must be verification")
        check(record["timestamp"].endswith("Z"), "timestamp must be UTC with Z")
        check(record["attempts"] == 2 and len(record["input_ids"]) == 2, "attempts/input_ids")
        expected_ids = [request_fingerprint(r) for r in provider.requests]
        check(record["input_ids"] == expected_ids, "input_ids must be request fingerprints")
        segment = record["segments"][2]
        check(segment["flagged"] is True and segment["failed"] == ["segment_count"], str(segment))
        check(
            set(segment) == {"key", "passed", "failed", "retried", "flagged"},
            f"unexpected segment fields {set(segment)}",
        )
        raw = lines[0]
        check(all(b not in raw for b in BLOCKS), "trace line must not contain source text")
        check("missing from the answer" not in raw, "trace line must not carry messages")


def test_retry_prompt_helpers() -> None:
    template = load_retry_template("v1")
    check("{corrections}" in template and template.count("{") == 1, "template placeholder")
    try:
        load_retry_template("v999")
    except ValueError as error:
        check("prompts/v999/retry_corrections.md" in str(error), f"message: {error}")
    else:
        check(False, "unknown version must raise ValueError")
    text = render_corrections(
        [Finding("numbers", "1", "bad {x} here"), Finding("json_valid", None, "oops")]
    )
    check("{" not in text and "}" not in text, "braces must be neutralised")
    check("- segment 1 [numbers]: bad (x) here" in text, text)
    check("- whole answer [json_valid]: oops" in text, text)
    base = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    out = retry_messages(base, template, [Finding("numbers", "1", "m")])
    check(base[1]["content"] == "u", "input messages must not be mutated")
    check(out[1]["content"].startswith("u\n\n") and "- segment 1" in out[1]["content"], "append")
    check(out[0]["content"] == "s", "system message untouched")


def main() -> int:
    tests = [
        test_passing_batch,
        test_retry_carries_corrections_and_recovers,
        test_failing_twice_is_flagged,
        test_invalid_json_twice_raises,
        test_invalid_json_then_clean_recovers,
        test_glossary_term_retried_then_flagged,
        test_zero_retries_flags_without_retry,
        test_disabled_is_identical_to_plain,
        test_trace_line_is_content_free,
        test_retry_prompt_helpers,
    ]
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
    raise SystemExit(main())
