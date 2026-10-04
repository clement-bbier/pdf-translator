"""Tests for app.rag.glossary: exact longest-match glossary and context provider.

No network access. Run: python tests/test_rag_glossary.py
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.llm.base import LLMRequest  # noqa: E402
from app.llm.fake_provider import GLOSSARY_LINE_RE, FakeProvider  # noqa: E402
from app.rag.glossary import Glossary, GlossaryContextProvider  # noqa: E402

FAILURES: list[str] = []
DATA = ROOT / "data" / "glossary" / "finance_zh.csv"


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def _glossary(rows: list[tuple[str, str, str]]) -> Glossary:
    """Build a glossary from (source, fr, en) rows through a temp CSV."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "g.csv"
        lines = ["source,target_fr,target_en,domain"] + [f"{s},{f},{e},t" for s, f, e in rows]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return Glossary.load(path)


def test_real_file_loads() -> None:
    """The shipped glossary has 59 terms."""
    check(len(Glossary.load(DATA)) == 59, "real glossary should have 59 rows")


def test_longest_match() -> None:
    """末期股息 wins over 股息."""
    glossary = Glossary.load(DATA)
    matches = glossary.find("本公司宣派末期股息", "en")
    check([m.source for m in matches] == ["末期股息"], f"longest: {matches}")
    check(matches[0].target == "final dividend", "target en")
    check((matches[0].start, matches[0].end) == (5, 9), "offsets")


def test_non_overlap_and_order() -> None:
    """Adjacent terms are both found; overlapping candidates are not reused."""
    glossary = _glossary([("甲乙", "ab", "AB"), ("乙丙", "bc", "BC"), ("丁", "d", "D")])
    found = glossary.find("甲乙丙丁", "en")
    check([m.source for m in found] == ["甲乙", "丁"], f"overlap: {found}")
    adjacent = glossary.find("丁甲乙", "en")
    check([m.source for m in adjacent] == ["丁", "甲乙"], f"adjacent: {adjacent}")


def test_no_match_and_languages() -> None:
    """No match, fr vs en targets, unsupported target."""
    glossary = Glossary.load(DATA)
    check(glossary.find("hello world", "en") == [], "no match")
    check(glossary.find("末期股息", "fr")[0].target == "dividende final", "fr target")
    check(glossary.find("末期股息", "en")[0].target == "final dividend", "en target")
    check(glossary.find("末期股息", "de") == [], "unsupported tgt")


def test_duplicate_rejected() -> None:
    """A duplicate source raises ValueError naming the line."""
    try:
        _glossary([("甲", "a", "A"), ("甲", "b", "B")])
    except ValueError as error:
        check("line 3" in str(error), f"line number missing: {error}")
    else:
        check(False, "duplicate not rejected")
    try:
        Glossary.load(ROOT / "data" / "glossary" / "missing.csv")
    except FileNotFoundError:
        pass
    else:
        check(False, "missing file not raised")


def test_render_and_fake_provider() -> None:
    """Block format matches GLOSSARY_LINE_RE and FakeProvider applies it."""
    provider = GlossaryContextProvider(Glossary.load(DATA))
    segments = ["本公司宣派末期股息", "末期股息 and 股息", "nothing"]
    text, count = provider.render(segments, "zh", "en")
    lines = text.split("\n")
    check(lines[0] == "Required terminology (use these target terms exactly):", "header")
    check(lines[1:] == ["- 末期股息 => final dividend", "- 股息 => dividend"], f"lines: {lines}")
    check(count == 2, f"count: {count}")
    check(not text.endswith("\n"), "trailing newline")
    check(all(re.match(GLOSSARY_LINE_RE, line) for line in lines[1:]), "regex mismatch")
    check(provider.context_for(segments, "zh", "en") == text, "context_for")
    check(provider.render(["nothing"], "zh", "en") == ("", 0), "empty render")
    check(provider.render(segments, "en", "en") == ("", 0), "src not supported")

    payload = {"0": "本公司宣派末期股息"}
    user = (
        "Source language: zh\nTarget language: en\n\n"
        + provider.context_for(["本公司宣派末期股息"], "zh", "en")
        + "\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    request = LLMRequest(
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": user}],
        model="m",
    )
    result = json.loads(FakeProvider().complete(request).text)
    check("final dividend" in result["0"], f"fake did not apply: {result}")


def test_simplified_does_not_match() -> None:
    """Documented limitation: Traditional-only glossary."""
    glossary = Glossary.load(DATA)
    check(len(glossary.find("董事會", "en")) == 1, "traditional should match")
    check(glossary.find("董事会", "en") == [], "simplified must not match")


def test_required_terms() -> None:
    """Keys are str(index); only segments with matches appear."""
    provider = GlossaryContextProvider(Glossary.load(DATA))
    required = provider.required_terms(["x", "末期股息和股息", "y"], "zh", "en")
    check(required == {"1": ["final dividend", "dividend"]}, f"required: {required}")
    check(provider.required_terms(["末期股息"], "fr", "en") == {}, "src filter")


def main() -> int:
    tests = [
        test_real_file_loads,
        test_longest_match,
        test_non_overlap_and_order,
        test_no_match_and_languages,
        test_duplicate_rejected,
        test_render_and_fake_provider,
        test_simplified_does_not_match,
        test_required_terms,
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
