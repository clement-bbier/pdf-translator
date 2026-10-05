"""Unit tests for app/verification: structural, glossary checks, run_checks, settings.

No network access. Run: python tests/test_verification_checks.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config as app_config  # noqa: E402
from app.verification import (  # noqa: E402
    CHECK_NAMES,
    TermProvider,
    VerificationConfig,
    collect_required_terms,
    run_checks,
    verification_config,
)
from app.verification.glossary import check_glossary  # noqa: E402
from app.verification.structure import check_json, check_segment_count  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


class _TermStub:
    """Provider exposing required_terms."""

    def required_terms(self, segment: str, src: str, tgt: str) -> list[str]:
        return ["Board", "board"] if "董事会" in segment else []


class _NoTerms:
    """Provider without required_terms (context only)."""

    def context_for(self, segments: list[str], src: str, tgt: str) -> str:
        return ""


def test_check_names() -> None:
    check(
        CHECK_NAMES == ("json_valid", "segment_count", "glossary_terms", "numbers", "dates"),
        "names",
    )


def test_json_valid() -> None:
    parsed, findings = check_json('{"0": "a"}')
    check(parsed == {"0": "a"} and findings == [], "valid object")

    parsed, findings = check_json("not json")
    check(parsed is None and len(findings) == 1, "invalid json")
    check(findings[0].check == "json_valid" and findings[0].key is None, "invalid json batch level")

    parsed, findings = check_json('["a"]')
    check(parsed is None and findings[0].key is None, "json list rejected")

    parsed, findings = check_json('{"0": "a", "1": 5}')
    check(parsed is not None and "1" in parsed, "dict still returned")
    check(len(findings) == 1 and findings[0].key == "1", "non-string value flagged for its key")
    check(findings[0].message == "value is not a string", "non-string message")


def test_segment_count() -> None:
    check(check_segment_count(["0", "1"], {"0": "a", "1": "b"}) == [], "complete answer")

    missing = check_segment_count(["0", "1"], {"0": "a"})
    check(len(missing) == 1 and missing[0].key == "1", "missing key")
    check(missing[0].message == "segment missing from the answer", "missing message")

    extra = check_segment_count(["0"], {"0": "a", "9": "z"})
    check(len(extra) == 1 and extra[0].key is None, "extra key is batch level")
    check(extra[0].message == "unexpected key '9' in the answer", "extra message")

    both = check_segment_count(["0", "1"], {"0": "a", "7": "z"})
    check(len(both) == 2, "missing and extra reported together")


def test_glossary() -> None:
    check(
        check_glossary("0", "The  BOARD met", ["board"]) == [],
        "case-insensitive, whitespace collapsed",
    )
    check(check_glossary("0", "a b", ["a\n  b"]) == [], "whitespace runs collapsed in the term")
    found = check_glossary("0", "The council met", ["board", "Audit"])
    check(
        [f.message for f in found]
        == ["missing glossary term: board", "missing glossary term: Audit"],
        "misses",
    )
    check(
        all(f.check == "glossary_terms" and f.key == "0" for f in found), "glossary finding fields"
    )
    check(check_glossary("0", "x", []) == [], "no terms is a no-op")


def test_collect_required_terms() -> None:
    segments = {"0": "董事会决定", "1": "other"}
    terms = collect_required_terms([_TermStub(), _NoTerms()], segments, "zh", "en")
    check(terms == {"0": ["Board", "board"]}, f"collected terms: {terms}")
    check(isinstance(_TermStub(), TermProvider), "protocol is runtime checkable")
    check(not isinstance(_NoTerms(), TermProvider), "context-only provider is not a TermProvider")
    check(
        collect_required_terms([_NoTerms()], segments, "zh", "en") == {}, "provider without method"
    )
    check(collect_required_terms([], segments, "zh", "en") == {}, "no providers")
    twice = collect_required_terms([_TermStub(), _TermStub()], segments, "zh", "en")
    check(twice == {"0": ["Board", "board"]}, "terms de-duplicated across providers")


def test_run_checks() -> None:
    request = {"0": "你好", "1": "再见"}
    ok = run_checks(request, json.dumps({"0": "Hello", "1": "Goodbye"}), "zh", "en")
    check(ok.ok and ok.translations == {"0": "Hello", "1": "Goodbye"}, "all pass")
    check(ok.failed_keys() == set() and ok.batch_findings == (), "nothing failed")
    passed, failed = ok.checks_for("0")
    check(failed == () and "json_valid" in passed and "segment_count" in passed, "checks_for pass")

    bad = run_checks(request, "oops", "zh", "en")
    check(
        not bad.ok and bad.translations == {} and len(bad.findings) == 1,
        "invalid JSON short-circuits",
    )
    check(bad.batch_findings == bad.findings, "batch finding")

    missing = run_checks(request, json.dumps({"0": "Hello"}), "zh", "en", {"0": ["Hi"]})
    check(missing.failed_keys() == {"0", "1"}, f"failed keys: {missing.failed_keys()}")
    check(missing.translations == {"0": "Hello"}, "only present keys are returned")
    passed, failed = missing.checks_for("0")
    check(
        failed == ("glossary_terms",) and "segment_count" in passed,
        f"checks_for 0: {passed} {failed}",
    )
    passed, failed = missing.checks_for("1")
    check(failed == ("segment_count",) and passed == (), f"checks_for 1: {passed} {failed}")
    check(len(missing.findings_for("1")) == 1, "findings_for")

    extra = run_checks({"0": "你好"}, json.dumps({"0": "Hello", "5": "x"}), "zh", "en")
    check(
        len(extra.batch_findings) == 1 and extra.failed_keys() == set(),
        "extra key is batch level only",
    )


def test_verification_config() -> None:
    check(verification_config({}) == VerificationConfig(False, 1), "missing section gives defaults")
    check(
        verification_config({"verification": None}) == VerificationConfig(False, 1), "null section"
    )
    cfg = verification_config({"verification": {"enabled": True, "max_retries": 2}})
    check(cfg == VerificationConfig(True, 2), "valid section")

    for bad in (
        {"max_retries": 4},
        {"max_retries": -1},
        {"max_retries": True},
        {"max_retries": "1"},
        {"enabled": "yes"},
        {"retries": 1},
    ):
        try:
            verification_config({"verification": bad})
        except ValueError as error:
            check("invalid 'verification' section" in str(error), f"message for {bad}")
        else:
            check(False, f"no ValueError for {bad}")


def main() -> int:
    """Run every test_* function in this module."""
    app_config.force_utf8_stdout()
    tests = [
        test_check_names,
        test_json_valid,
        test_segment_count,
        test_glossary,
        test_collect_required_terms,
        test_run_checks,
        test_verification_config,
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
