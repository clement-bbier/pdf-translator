"""Tests for app.llm.answer: model answers wrapped in a Markdown code fence.

Found by the first real end-to-end run: Claude Haiku 4.5 returned
```json ... ``` around the JSON object, so every batch failed json_valid
twice and was left untranslated. No network.
Run: python tests/test_llm_answer.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.llm.answer import strip_code_fence  # noqa: E402
from app.llm.fake_provider import FakeProvider  # noqa: E402
from app.llm.translator import LLMTranslator  # noqa: E402
from app.verification import run_checks  # noqa: E402
from app.verification.settings import VerificationConfig  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def test_strip_code_fence() -> None:
    """A fence around the whole answer is removed; anything else is untouched."""
    body = '{"0": "Final dividend"}'
    cases = {
        body: body,
        f"```json\n{body}\n```": body,
        f"```\n{body}\n```": body,
        f"  ```json\n{body}\n```  \n": body,
        f"```json\r\n{body}\r\n```": body,
        f"```JSON\n{body}```": body,
        f"Here it is:\n```json\n{body}\n```": f"Here it is:\n```json\n{body}\n```",
        '{"0": "uses ``` inside"}': '{"0": "uses ``` inside"}',
    }
    for given, expected in cases.items():
        check(strip_code_fence(given) == expected, f"strip_code_fence({given!r})")


def test_translator_accepts_fenced_answer() -> None:
    """The unverified path parses a fenced answer instead of failing the batch."""
    translator = LLMTranslator(FakeProvider(fenced_json=True), model=FakeProvider.MODEL_ID)
    [result] = translator.translate_blocks(["董事會"], "zh", "en")
    check(result.startswith("[en] "), f"unexpected translation {result!r}")


def test_verification_accepts_fenced_answer() -> None:
    """json_valid passes on a fenced object; the batch is neither retried nor flagged."""
    report = run_checks({"0": "5.30"}, '```json\n{"0": "5.30"}\n```', "zh", "en")
    check(not report.findings, f"unexpected findings {report.findings}")

    translator = LLMTranslator(
        FakeProvider(fenced_json=True),
        model=FakeProvider.MODEL_ID,
        verification=VerificationConfig(enabled=True, max_retries=1),
    )
    translator.translate_blocks(["每股5.30港元"], "zh", "en")
    outcome = translator.last_verification
    check(outcome is not None, "verification did not run")
    if outcome is not None:
        flagged = [segment for segment in outcome.segments if segment.flagged]
        check(not flagged, "a clean fenced answer must not be flagged")


def test_invalid_json_inside_fence_still_fails() -> None:
    """Stripping the fence never hides a broken object."""
    report = run_checks({"0": "x"}, '```json\n{"0": "x"\n```', "zh", "en")
    check(any(f.check == "json_valid" for f in report.findings), "broken JSON must fail")
    check(json.loads(strip_code_fence('```\n{"a": "b"}\n```')) == {"a": "b"}, "round trip")


def main() -> int:
    tests = [
        test_strip_code_fence,
        test_translator_accepts_fenced_answer,
        test_verification_accepts_fenced_answer,
        test_invalid_json_inside_fence_still_fails,
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
