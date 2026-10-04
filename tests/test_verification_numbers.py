"""Unit tests for app/verification/numbers.py.

No network access. Run: python tests/test_verification_numbers.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config as app_config  # noqa: E402
from app.verification.numbers import check_numbers, extract_numbers, find_currencies  # noqa: E402

FAILURES: list[str] = []

NBSP = " "
NNBSP = " "


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def passes(source: str, translation: str, src: str = "zh", tgt: str = "en") -> bool:
    return check_numbers("0", source, translation, src, tgt) == []


def values(text: str, lang: str = "en") -> list[set[Decimal]]:
    return [set(m.values) for m in extract_numbers(text, lang)]


def test_scaled_amounts() -> None:
    check(passes("人民幣7,518億元", "RMB751.8 billion"), "億 to billion")
    check(passes("142.23億港元", "HK$14,223 million"), "億 to million")
    check(not passes("142.23億港元", "HK$14,223 thousand"), "wrong scale fails")
    check(passes("营收10万元", "revenue of 100,000 yuan"), "万 scaled")
    check(passes("营收10万元", "revenue of 10 thousand yuan"), "万 mantissa and scale")
    check(passes("3百万", "3 million"), "百万")
    check(passes("5 million", "500万", src="en", tgt="zh"), "million to 万")
    check(passes("2 milliards", "2 billion", src="fr", tgt="en"), "milliards")
    check(passes("2 Md", "2 billion", src="fr", tgt="en"), "Md")
    check(values("7,518億") == [{Decimal(7518), Decimal(751800000000)}], "values of 億")


def test_percent() -> None:
    for translation in ("15 per cent", "15 %", "15%", "15 percent"):
        check(passes("增长15%", translation), f"percent form {translation}")
    check(passes("增长15%", "15 pour cent", tgt="fr"), "pour cent")
    check(passes("增长２个百分点", "2 percentage points"), "percentage points")
    check(not passes("增长15%", "growth of 15"), "percent kind is required")
    check(passes("增长15", "growth of 15%"), "percent allowed when the source has none")
    check(passes("10-15%", "10 to 15 per cent"), "range")
    check(passes("10至15%", "10-15%"), "range with 至")
    check(passes("10–15 %", "10 à 15 %", tgt="fr"), "range with en dash and à")
    check(not passes("10-15%", "10 to 15"), "range lower bound is a percent")
    check(passes("１５％", "15%"), "full-width percent")


def test_signs_and_decimals() -> None:
    check(passes("增长-2%", "a 2% decrease"), "sign carried by a word")
    check(passes("-1,234.5", f"−1{NBSP}234,5", tgt="fr"), "negative into fr with NBSP")
    check(passes("-1,234.5", "1 234,5", tgt="fr"), "sign dropped")
    check(passes("1,234.5", f"1{NBSP}234,5", tgt="fr"), "NBSP thousands")
    check(passes("1,234.5", f"1{NNBSP}234,5", tgt="fr"), "narrow NBSP thousands")
    check(passes("1,234.5", "1,234.5", tgt="fr"), "same notation in fr")
    check(passes("3.5", "3,5", tgt="fr"), "decimal comma")
    check(passes("1,234", "1,234", tgt="fr"), "ambiguous target")
    check(passes("1,234", "1.234", tgt="en"), "ambiguous english target")
    check(passes("1,234", "1 234", tgt="fr"), "space thousands")
    check(passes("1 234,5", "1,234.5", src="fr", tgt="en"), "fr source into en")
    check(passes("1.234.567", "1,234,567", src="fr", tgt="en"), "fr dotted thousands")
    check(not passes("1,234.5", "1 234,6", tgt="fr"), "different number fails")
    check(not passes("1,234", "1,235"), "different integer fails")
    check(values("3.1.2") == [{Decimal(3)}, {Decimal(1)}, {Decimal(2)}], "section number")
    check(passes("见3.1.2节", "see section 3.1.2"), "section number kept")
    check(passes("项目1,2,3", "items 1, 2, 3"), "comma list")
    check(passes("１２３", "123"), "full-width digits")


def test_ignored_digits() -> None:
    check(values("A4纸和COVID19") == [], "identifiers are ignored")
    check(passes("A4纸", "paper"), "identifier not required")
    check(passes("第3季度", "Q3"), "identifier in the translation still counts")
    check(passes("2024年3月12日召开", "held on 12 March 2024"), "date digits ignored")
    check(passes("2024-03-12", "12 March 2024"), "iso digits ignored")
    check(passes("3月12日", "12 March"), "month-day without year")
    check(values("2024年3月12日") == [], "no number inside a date")
    check(values("RMB751.8") == [{Decimal("751.8")}], "currency code prefix is not an identifier")


def test_findings() -> None:
    found = check_numbers("7", "收入123,456和789", "revenue 123,456", "zh", "en")
    check(len(found) == 1, f"one finding: {found}")
    check(found[0].check == "numbers" and found[0].key == "7", "finding fields")
    check(found[0].message == "number not found in the translation: 789", found[0].message)
    twice = check_numbers("0", "5, 5, 5", "none", "zh", "en")
    check(len(twice) == 1, "one finding per distinct raw token")
    check(check_numbers("0", "没有数字", "no digits", "zh", "en") == [], "no numbers")


def test_currencies() -> None:
    check(find_currencies("142.23億港元") == {"HKD"}, "港元")
    check(find_currencies("US$5 and hk$3") == {"USD", "HKD"}, "US$ and HK$")
    check(find_currencies("人民币 and 美元") == {"RMB", "USD"}, "RMB and USD words")
    check(
        find_currencies("renminbi, Hong Kong dollars, dollars américains") == {"RMB", "HKD", "USD"},
        "words",
    )
    check(find_currencies("nothing") == set(), "none")
    check(not passes("142.23億港元", "14,223 million dollars"), "missing HK$ fails")
    missing = check_numbers("0", "142.23億港元", "14,223 million", "zh", "en")
    check([f.message for f in missing] == ["currency not kept: HKD"], f"currency: {missing}")
    check(passes("5美元", "US$5"), "US$ kept")
    check(passes("100港元", "HK$100"), "HK$ kept")
    check(passes("100港元", "100 Hong Kong dollars"), "HK dollars kept")
    check(passes("100元人民币", "100 yuan"), "yuan for RMB")
    check(passes("100 dollars HK", "HK$100", src="fr", tgt="en"), "fr source currency")


def main() -> int:
    """Run every test_* function in this module."""
    app_config.force_utf8_stdout()
    tests = [
        test_scaled_amounts,
        test_percent,
        test_signs_and_decimals,
        test_ignored_digits,
        test_findings,
        test_currencies,
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
