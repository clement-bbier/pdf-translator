"""Tests for app.llm: fake/anthropic providers, LLMTranslator, factory.

No network access. Run: python tests/test_llm_providers.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402
import httpx2  # noqa: E402  # the HTTP library the anthropic SDK is built on

from app import config as app_config  # noqa: E402
from app.llm.anthropic_provider import AnthropicProvider, model_accepts_sampling  # noqa: E402
from app.llm.base import LLMRequest, request_fingerprint  # noqa: E402
from app.llm.fake_provider import FakeProvider  # noqa: E402
from app.llm.translator import LLMTranslator  # noqa: E402
from app.translator import MockTranslator, TranslationError, get_translator  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def _request(payload: dict, tgt: str = "en", extra_system: str = "") -> LLMRequest:
    system = "sys" + extra_system
    user = (
        f"Source language: zh\nTarget language: {tgt}\n\n{json.dumps(payload, ensure_ascii=False)}"
    )
    return LLMRequest(
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        model="m",
    )


# --------------------------------------------------------------------------- #
# FakeProvider
# --------------------------------------------------------------------------- #


def test_fake_deterministic_and_prefix() -> None:
    """Same input, same output; values carry the target prefix."""
    request = _request({"0": "你好", "1": "Article 5"})
    first = FakeProvider().complete(request)
    second = FakeProvider().complete(request)
    check(first.text == second.text, "FakeProvider not deterministic")
    result = json.loads(first.text)
    check(result["0"].startswith("[en] "), f"missing prefix: {result['0']!r}")
    check(list(result) == ["0", "1"], "keys changed")
    check(first.model == "fake-model", "wrong model id")


def test_fake_glossary_and_dates() -> None:
    """Glossary lines apply; dates are rendered per target language."""
    glossary = "\n- 甲方 => Party A\n- 甲方公司 => Party A Corp"
    request = _request({"0": "甲方公司 and 甲方 signed on 2024年3月12日"}, extra_system=glossary)
    result = json.loads(FakeProvider().complete(request).text)
    check("Party A Corp and Party A signed" in result["0"], f"glossary: {result['0']!r}")
    check("12 March 2024" in result["0"], f"en date: {result['0']!r}")

    fr = json.loads(FakeProvider().complete(_request({"0": "2024年3月1日"}, tgt="fr")).text)
    check("1 mars 2024" in fr["0"], f"fr date: {fr['0']!r}")

    de = json.loads(FakeProvider().complete(_request({"0": "2024年3月1日"}, tgt="de")).text)
    check("2024年3月1日" in de["0"], f"other tgt date should be unchanged: {de['0']!r}")


def test_fake_fault_flags() -> None:
    """Each fault flag produces its fault."""
    request = _request({"0": "Article 12", "1": "2024"})
    dropped = json.loads(FakeProvider(drop_numbers=True).complete(request).text)
    check(not any(ch.isdigit() for v in dropped.values() for ch in v), "digits not dropped")

    wrong = json.loads(FakeProvider(wrong_segment_count=True).complete(request).text)
    check(list(wrong) == ["0"], f"last key not dropped: {list(wrong)}")
    single = json.loads(FakeProvider(wrong_segment_count=True).complete(_request({"0": "a"})).text)
    check("999" in single, "extra key not added for a single-key map")

    broken = FakeProvider(invalid_json=True).complete(request).text
    try:
        json.loads(broken)
        parsed = True
    except ValueError:
        parsed = False
    check(not parsed, "invalid_json should not parse")


def test_fake_token_counts_and_page() -> None:
    """Token counts follow the len // 4 rule; image requests return plain text."""
    request = _request({"0": "abcdefgh"})
    response = FakeProvider().complete(request)
    total = sum(len(m["content"]) for m in request.messages)
    check(response.input_tokens == total // 4, "input_tokens mismatch")
    check(response.output_tokens == len(response.text) // 4, "output_tokens mismatch")
    check(response.finish_reason == "end_turn", "finish_reason")

    translator = LLMTranslator(FakeProvider(), model="fake-model")
    text = translator.translate_page_image(b"\x89PNGdata", "zh", "en")
    check(text.startswith("[en] ") and text.count("\n\n") == 2, "page text shape")


def test_fingerprint_stable() -> None:
    """Fingerprint is stable and sensitive to content."""
    a = _request({"0": "x"})
    b = _request({"0": "x"})
    c = _request({"0": "y"})
    check(request_fingerprint(a) == request_fingerprint(b), "fingerprint unstable")
    check(request_fingerprint(a) != request_fingerprint(c), "fingerprint insensitive")


# --------------------------------------------------------------------------- #
# LLMTranslator
# --------------------------------------------------------------------------- #


def test_translator_round_trip_and_faults() -> None:
    """Round trip over FakeProvider; bad provider output raises TranslationError."""
    translator = LLMTranslator(FakeProvider(), model="fake-model")
    result = translator.translate_blocks(["a", "b", "c"], "zh", "en")
    check(result == ["[en] a", "[en] b", "[en] c"], f"round trip: {result}")
    check(translator.translate_blocks([], "zh", "en") == [], "empty input")

    for flag in ("invalid_json", "wrong_segment_count"):
        bad = LLMTranslator(FakeProvider(**{flag: True}), model="fake-model")
        try:
            bad.translate_blocks(["a", "b"], "zh", "en")
            raised = False
        except TranslationError:
            raised = True
        check(raised, f"{flag} should raise TranslationError")

    try:
        translator.translate_page_image(b"", "zh", "en")
        raised = False
    except TranslationError:
        raised = True
    check(raised, "empty page image should raise")


# --------------------------------------------------------------------------- #
# AnthropicProvider
# --------------------------------------------------------------------------- #


def _message_json(text: str = "hello") -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


def _provider(handler) -> AnthropicProvider:
    client = anthropic.Anthropic(
        api_key="test-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    return AnthropicProvider(client=client)


def _simple_request(model: str) -> LLMRequest:
    return LLMRequest(
        messages=[
            {"role": "system", "content": "be precise"},
            {"role": "user", "content": "hi"},
        ],
        model=model,
        temperature=0,
    )


def test_anthropic_temperature_and_system() -> None:
    """Temperature only for models accepting sampling; system is top-level."""
    bodies: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=_message_json())

    provider = _provider(handler)
    response = provider.complete(_simple_request("claude-haiku-4-5-20251001"))
    provider.complete(_simple_request("claude-sonnet-5-5"))

    check(bodies[0].get("temperature") == 0, "temperature missing for haiku")
    check("temperature" not in bodies[1], "temperature sent for sonnet-5")
    check(bodies[0].get("system") == "be precise", f"system: {bodies[0].get('system')!r}")
    check(all(m["role"] != "system" for m in bodies[0]["messages"]), "system in messages")
    check(bodies[0]["max_tokens"] == 4096, "max_tokens not sent")
    check(response.text == "hello", "text")
    check(response.input_tokens == 11 and response.output_tokens == 7, "usage")
    check(response.finish_reason == "end_turn", "finish_reason")
    check(model_accepts_sampling("claude-opus-5-1") is False, "opus-5 sampling")
    check(model_accepts_sampling("claude-haiku-4-5-20251001") is True, "haiku sampling")


def test_anthropic_image_part() -> None:
    """Neutral image parts become Anthropic base64 image blocks."""
    bodies: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=_message_json())

    request = LLMRequest(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "t"},
                    {"type": "image", "media_type": "image/png", "data": "QUJD"},
                ],
            }
        ],
        model="claude-haiku-4-5-20251001",
    )
    _provider(handler).complete(request)
    part = bodies[0]["messages"][0]["content"][1]
    check(
        part
        == {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
        },
        f"image part: {part}",
    )
    check("system" not in bodies[0], "empty system should be omitted")


def test_anthropic_retry_and_errors() -> None:
    """429 is retried then succeeds; 400 fails after one attempt."""
    calls = {"n": 0}

    def flaky(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx2.Response(
                429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "x"}}
            )
        return httpx2.Response(200, json=_message_json())

    import app.llm.retry as retry_module

    original_sleep = retry_module.time.sleep
    retry_module.time.sleep = lambda seconds: None  # type: ignore[attr-defined]
    try:
        response = _provider(flaky).complete(_simple_request("claude-haiku-4-5-20251001"))
        check(response.text == "hello" and calls["n"] == 2, f"429 retry: calls={calls['n']}")

        bad_calls = {"n": 0}

        def bad(request: httpx2.Request) -> httpx2.Response:
            bad_calls["n"] += 1
            return httpx2.Response(
                400,
                json={
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "SECRET"},
                },
            )

        try:
            _provider(bad).complete(_simple_request("claude-haiku-4-5-20251001"))
            message = None
        except TranslationError as error:
            message = str(error)
        check(message is not None, "400 should raise TranslationError")
        check(bad_calls["n"] == 1, f"400 attempts: {bad_calls['n']}")
        check(message is not None and "SECRET" not in message, "response body echoed")

        always = {"n": 0}

        def down(request: httpx2.Request) -> httpx2.Response:
            always["n"] += 1
            return httpx2.Response(
                529, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}}
            )

        try:
            _provider(down).complete(_simple_request("claude-haiku-4-5-20251001"))
            raised = False
        except TranslationError:
            raised = True
        check(raised and always["n"] == 3, f"529 should exhaust 3 attempts: {always['n']}")
    finally:
        retry_module.time.sleep = original_sleep  # type: ignore[attr-defined]


def test_anthropic_missing_key() -> None:
    """A missing ANTHROPIC_API_KEY raises TranslationError."""
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        try:
            AnthropicProvider()
            raised = False
        except TranslationError:
            raised = True
        check(raised, "missing key should raise TranslationError")
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def test_factory_providers() -> None:
    """fake and anthropic build LLMTranslators; mock is unchanged."""
    check(isinstance(get_translator({"provider": "mock"}), MockTranslator), "mock")
    fake = get_translator({"provider": "fake"})
    check(isinstance(fake, LLMTranslator), "fake")

    saved = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        translator = get_translator({"provider": "anthropic", "llm": {"temperature": 0.2}})
        check(isinstance(translator, LLMTranslator), "anthropic")
        check(translator.model == "claude-haiku-4-5-20251001", f"model: {translator.model}")
    finally:
        if saved is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved


def main() -> int:
    """Run every test_* function in this module."""
    app_config.force_utf8_stdout()
    tests = [
        test_fake_deterministic_and_prefix,
        test_fake_glossary_and_dates,
        test_fake_fault_flags,
        test_fake_token_counts_and_page,
        test_fingerprint_stable,
        test_translator_round_trip_and_faults,
        test_anthropic_temperature_and_system,
        test_anthropic_image_part,
        test_anthropic_retry_and_errors,
        test_anthropic_missing_key,
        test_factory_providers,
    ]
    original_sleep = time.sleep
    for test in tests:
        before = len(FAILURES)
        try:
            test()
        except Exception as error:  # noqa: BLE001 - report and continue
            FAILURES.append(f"{test.__name__} raised {type(error).__name__}: {error}")
        finally:
            time.sleep = original_sleep
        print(f"[{'OK' if len(FAILURES) == before else 'FAIL'}] {test.__name__}")

    print(f"\n{len(tests)} tests, {len(FAILURES)} failure(s)")
    for failure in FAILURES:
        print(f"  - {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
