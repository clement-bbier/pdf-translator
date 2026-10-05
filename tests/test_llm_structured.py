"""Tests for structured outputs on block translation requests.

Claude Sonnet 5.5 sometimes answered with a first JSON object, a correction
note and a second object, which failed json_valid twice. Block requests now
carry a JSON schema of the batch; the Anthropic provider sends it as
output_config.format, the gateway provider keeps its body unchanged.
No network. Run: python tests/test_llm_structured.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.llm.anthropic_provider import AnthropicProvider  # noqa: E402
from app.llm.base import LLMRequest, LLMResponse  # noqa: E402
from app.llm.fake_provider import FakeProvider  # noqa: E402
from app.llm.translator import LLMTranslator, batch_response_format  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


class SpyProvider:
    """Records requests and answers through FakeProvider."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []
        self._inner = FakeProvider()

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return self._inner.complete(request)


class FakeMessages:
    """Stands in for client.messages; returns a fixed text answer."""

    def __init__(self) -> None:
        self.kwargs: dict = {}

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"0": "ok"}')],
            model=kwargs["model"],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            stop_reason="end_turn",
        )


def test_batch_schema() -> None:
    """Exactly the batch keys, all strings, nothing else."""
    schema = batch_response_format(["0", "1"])
    check(schema["type"] == "json_schema", "type")
    body = schema["schema"]
    check(body["required"] == ["0", "1"], "required keys")
    check(body["additionalProperties"] is False, "no extra keys")
    check(body["properties"] == {"0": {"type": "string"}, "1": {"type": "string"}}, "properties")


def test_translator_sets_schema_on_block_requests_only() -> None:
    """Block requests carry the schema; the page-image request does not."""
    spy = SpyProvider()
    translator = LLMTranslator(spy, model=FakeProvider.MODEL_ID)
    translator.translate_blocks(["甲", "乙"], "zh", "en")
    check(spy.requests[-1].response_format == batch_response_format(["0", "1"]), "blocks schema")
    translator.translate_page_image(b"png-bytes", "zh", "en")
    check(spy.requests[-1].response_format is None, "page image request has no schema")


def test_anthropic_sends_output_config() -> None:
    """The schema becomes output_config.format; no schema, no output_config."""
    messages = FakeMessages()
    provider = AnthropicProvider(client=SimpleNamespace(messages=messages))
    request = LLMRequest(
        messages=[{"role": "user", "content": "x"}],
        model="claude-haiku-4-5-20251001",
        temperature=0,
        response_format=batch_response_format(["0"]),
    )
    provider.complete(request)
    check(
        messages.kwargs.get("output_config")
        == {"format": {"type": "json_schema", "schema": batch_response_format(["0"])["schema"]}},
        f"output_config: {messages.kwargs.get('output_config')}",
    )
    request.response_format = None
    provider.complete(request)
    check("output_config" not in messages.kwargs, "output_config sent without a schema")


def main() -> int:
    tests = [
        test_batch_schema,
        test_translator_sets_schema_on_block_requests_only,
        test_anthropic_sends_output_config,
    ]
    for test in tests:
        failures_before = len(FAILURES)
        try:
            test()
        except Exception as error:  # noqa: BLE001 - report and continue
            FAILURES.append(f"{test.__name__} raised {type(error).__name__}: {error}")
        status = "OK" if len(FAILURES) == failures_before else "FAIL"
        print(f"[{status}] {test.__name__}")

    print(f"\n{len(tests)} tests, {len(FAILURES)} failure(s)")
    for failure in FAILURES:
        print(f"  - {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
