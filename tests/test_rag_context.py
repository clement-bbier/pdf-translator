"""Tests for app.rag.context and the RAG wiring in the prompt path.

No network, FakeProvider only, TM cache in a temporary directory.
Run: python tests/test_rag_context.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config as app_config  # noqa: E402
from app.llm.base import LLMRequest, LLMResponse, request_fingerprint  # noqa: E402
from app.llm.fake_provider import FakeProvider  # noqa: E402
from app.llm.prompts import SYSTEM_PROMPT, blocks_messages  # noqa: E402
from app.llm.translator import LLMTranslator  # noqa: E402
from app.rag import glossary as glossary_module  # noqa: E402
from app.rag.context import RagContext, build_rag_context  # noqa: E402
from app.rag.glossary import Glossary, GlossaryContextProvider  # noqa: E402
from app.translator import MockTranslator, get_translator  # noqa: E402

FAILURES: list[str] = []
GLOSSARY_FILE = ROOT / "data" / "glossary" / "finance_zh.csv"
TM_SEGMENT = "本公司宣派末期股息"


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


class SpyProvider:
    """Wraps FakeProvider and records every request."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []
        self._inner = FakeProvider()

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return self._inner.complete(request)


def _glossary_context() -> RagContext:
    return RagContext([GlossaryContextProvider(Glossary.load(GLOSSARY_FILE))])


def test_blocks_messages_byte_identical() -> None:
    """Empty context reproduces the pre-change prompt exactly."""
    payload = {"0": "你好", "1": "Article 5"}
    expected_user = 'Source language: zh\nTarget language: en\n\n{"0": "你好", "1": "Article 5"}'
    expected = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": expected_user},
    ]
    check(blocks_messages(payload, "zh", "en") == expected, "default prompt changed")
    check(blocks_messages(payload, "zh", "en", "") == expected, "empty context changed prompt")
    with_context = blocks_messages(payload, "zh", "en", "CTX")
    check(
        with_context[1]["content"]
        == 'Source language: zh\nTarget language: en\n\nCTX\n\n{"0": "你好", "1": "Article 5"}',
        "context not placed between header and payload",
    )


def test_no_rag_requests_identical() -> None:
    """No provider, or providers finding nothing, send the same messages."""
    blocks = ["你好", "Article 5"]
    spies = []
    for context in (None, _glossary_context(), RagContext([])):
        spy = SpyProvider()
        LLMTranslator(spy, model="m", context_provider=context).translate_blocks(blocks, "zh", "en")
        spies.append(spy)
    base = spies[0].requests[0]
    for spy in spies[1:]:
        request = spy.requests[0]
        check(request.messages == base.messages, "messages differ without matches")
        check(request_fingerprint(request) == request_fingerprint(base), "fingerprint differs")
        check(request.context_sources == {}, "sources not empty without matches")


def test_glossary_in_prompt_and_sources() -> None:
    """A matched term yields the block before the payload, the fake applies it."""
    spy = SpyProvider()
    translator = LLMTranslator(spy, model="m", context_provider=_glossary_context())
    result = translator.translate_blocks([TM_SEGMENT], "zh", "en")
    request = spy.requests[0]
    user = request.messages[1]["content"]
    block = user.find("Required terminology")
    payload = user.find(json.dumps({"0": TM_SEGMENT}, ensure_ascii=False))
    check(0 <= block < payload, "terminology block must precede the payload")
    check("final dividend" in result[0], f"term not applied: {result[0]!r}")
    check(request.context_sources == {"glossary": 1}, f"sources: {request.context_sources}")
    check(TM_SEGMENT not in json.dumps(request.context_sources), "sources must not hold text")


def test_both_providers_sources() -> None:
    """Both providers on: both names with counts, no content."""
    with tempfile.TemporaryDirectory() as tmp:
        config = {"rag": {"glossary": True, "translation_memory": True, "tm_min_score": 0.0}}
        context = build_rag_context(config, cache_dir=Path(tmp))
        check(context is not None, "context expected")
        if context is None:
            return
        rendered = context.render([TM_SEGMENT], "zh", "en")
    check(set(rendered.sources) == {"glossary", "translation_memory"}, f"{rendered.sources}")
    check(all(isinstance(v, int) and v > 0 for v in rendered.sources.values()), "bad counts")
    check("Required terminology" in rendered.text, "glossary block missing")
    check("Examples of validated translations" in rendered.text, "TM block missing")
    check(context.context_for([TM_SEGMENT], "zh", "en") == rendered.text, "context_for differs")


def test_build_returns_none_when_off() -> None:
    """Both switches off: no context."""
    config = {"rag": {"glossary": False, "translation_memory": False}}
    check(build_rag_context(config) is None, "expected None")


def test_mock_does_not_touch_rag_files() -> None:
    """The mock provider never loads RAG data."""
    original = glossary_module.Glossary.load

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("RAG file loaded for mock provider")

    glossary_module.Glossary.load = boom  # type: ignore[assignment,method-assign]
    try:
        translator = get_translator({"provider": "mock"})
        check(isinstance(translator, MockTranslator), "mock expected")
    finally:
        glossary_module.Glossary.load = original  # type: ignore[method-assign]


def test_get_translator_fake_wires_context() -> None:
    """The fake provider gets the glossary by default and reports sources."""
    translator = get_translator({"provider": "fake"})
    check(isinstance(translator, LLMTranslator), "LLMTranslator expected")
    spy = SpyProvider()
    translator._provider = spy  # type: ignore[attr-defined]
    translator.translate_blocks([TM_SEGMENT], "zh", "en")
    check(spy.requests[0].context_sources == {"glossary": 1}, "glossary not wired")


def test_required_terms_propagated() -> None:
    """required_terms is keyed by payload index."""
    rendered = _glossary_context().render(["你好", TM_SEGMENT], "zh", "en")
    check(list(rendered.required_terms) == ["1"], f"keys: {list(rendered.required_terms)}")
    check("final dividend" in rendered.required_terms["1"], f"{rendered.required_terms}")


def main() -> int:
    """Run every test in this module."""
    app_config.force_utf8_stdout()
    tests = [
        test_blocks_messages_byte_identical,
        test_no_rag_requests_identical,
        test_glossary_in_prompt_and_sources,
        test_both_providers_sources,
        test_build_returns_none_when_off,
        test_mock_does_not_touch_rag_files,
        test_get_translator_fake_wires_context,
        test_required_terms_propagated,
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
