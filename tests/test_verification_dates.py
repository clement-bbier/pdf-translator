"""Unit tests for app/verification/dates.py.

No network access. Run: python tests/test_verification_dates.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config as app_config  # noqa: E402
from app.verification.dates import check_dates, find_dates  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def ymd(text: str) -> list[tuple[int, int, int | None]]:
    return [(d.year, d.month, d.day) for d in find_dates(text)]


def passes(source: str, translation: str, src: str = "zh", tgt: str = "en") -> bool:
    return check_dates("0", source, translation, src, tgt) == []


def test_find_dates() -> None:
    check(ymd("2024年3月12日") == [(2024, 3, 12)], "cjk date")
    check(ymd("２０２４年３月１２日") == [(2024, 3, 12)], "full-width digits")
    check(ymd("2024 年 3 月 12 日") == [(2024, 3, 12)], "spaces")
    check(ymd("2024年6月") == [(2024, 6, None)], "year-month")
    check(ymd("2024년 3월 12일") == [(2024, 3, 12)], "korean")
    check(ymd("二零二五年十二月三十一日") == [(2025, 12, 31)], "chinese numerals")
    check(ymd("二〇二四年三月廿五日") == [(2024, 3, 25)], "chinese numerals with 廿")
    check(ymd("二零二五年十月十日") == [(2025, 10, 10)], "chinese numerals 十")
    check(ymd("二零二五年二月二十日") == [(2025, 2, 20)], "chinese numerals X十")
    check(ymd("2024-03-12 and 2024/03/13") == [(2024, 3, 12), (2024, 3, 13)], "iso")
    check(ymd("2024-02-30") == [], "invalid calendar date rejected")
    check(ymd("2024年13月1日") == [], "invalid month rejected")

    ambiguous = find_dates("12/03/2024")
    check(len(ambiguous) == 1 and ambiguous[0].alternatives == ((2024, 12, 3),), "DD/MM ambiguity")
    check(ymd("31.12.2024") == [(2024, 12, 31)], "unambiguous DD.MM.YYYY")
    check(find_dates("31.12.2024")[0].alternatives == (), "no alternative when invalid")

    check(ymd("12 March 2024") == [(2024, 3, 12)], "english day first")
    check(ymd("12th March 2024") == [(2024, 3, 12)], "english ordinal")
    check(ymd("March 12, 2024") == [(2024, 3, 12)], "english month first")
    check(ymd("Mar. 12, 2024") == [(2024, 3, 12)], "english abbreviation")
    check(ymd("March 2024") == [(2024, 3, None)], "english year-month")
    check(ymd("12 mars 2024") == [(2024, 3, 12)], "french")
    check(ymd("1er mars 2024") == [(2024, 3, 1)], "french 1er")
    check(ymd("mars 2024") == [(2024, 3, None)], "french year-month")
    check(ymd("5 FÉVRIER 2024") == [(2024, 2, 5)], "french accents and case")
    check(ymd("5 fevrier 2024") == [(2024, 2, 5)], "french without accents")
    check(ymd("5 août 2024") == [(2024, 8, 5)], "french août")
    check(ymd("3 decembre 2024") == [(2024, 12, 3)], "french decembre")
    check(ymd("see 5 days 2024") == [], "non month word ignored")


def test_check_dates_pass() -> None:
    check(passes("2024年3月12日", "12 March 2024"), "cjk vs english")
    check(passes("2024年3月12日", "12 mars 2024", tgt="fr"), "cjk vs french")
    check(passes("2024年3月12日", "March 12, 2024"), "cjk vs month first")
    check(passes("2024-03-12", "12 March 2024"), "iso vs english")
    check(passes("12/03/2024", "12 March 2024"), "DD/MM vs english")
    check(passes("12/03/2024", "December 3, 2024"), "ambiguous second reading")
    check(passes("二零二五年十二月三十一日", "31 December 2025"), "numerals vs english")
    check(passes("二零二五年十二月三十一日", "31 décembre 2025", tgt="fr"), "numerals vs french")
    check(passes("2024年6月", "June 2024"), "year-month")
    check(passes("2024年6月", "15 June 2024"), "year-month matched by a full date")
    check(passes("没有日期", "no date here"), "no dates")
    check(passes("12 March 2024", "2024年3月12日", src="en", tgt="zh"), "CJK allowed for zh target")


def test_check_dates_fail() -> None:
    found = check_dates("3", "2024年3月12日", "13 March 2024", "zh", "en")
    check(len(found) == 1 and found[0].check == "dates" and found[0].key == "3", "wrong day")
    check("2024年3月12日" in found[0].message, "message carries the source date")
    check(not passes("2024年3月12日", "March 2024"), "day dropped")
    check(not passes("2024年3月12日", "no date at all"), "date missing")
    check(not passes("2024年6月", "July 2024"), "wrong month")
    check(not passes("2024年6月", "June 2023"), "wrong year")

    left = check_dates("0", "2024年3月12日", "The 2024年3月12日 meeting, 12 March 2024", "zh", "en")
    check(
        [f.message for f in left] == ["CJK date left untranslated: 2024年3月12日"],
        f"CJK residue: {left}",
    )
    numerals = check_dates("0", "x", "二零二五年十二月三十一日", "zh", "fr")
    check(len(numerals) == 1 and "CJK date left untranslated" in numerals[0].message, "numerals")
    korean = check_dates("0", "x", "2024년 3월 12일", "ko", "en")
    check(len(korean) == 1, "korean residue")
    check(check_dates("0", "x", "2024年3月12日", "zh", "zh") == [], "no residue check for zh")


def main() -> int:
    """Run every test_* function in this module."""
    app_config.force_utf8_stdout()
    tests = [test_find_dates, test_check_dates_pass, test_check_dates_fail]
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
