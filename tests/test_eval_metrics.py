"""Tests for evaluation/metrics.py (standard library, pure functions).

Run: python tests/test_eval_metrics.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation import metrics  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


@dataclass
class Entry:
    source: str
    en: str
    fr: str = ""

    def target(self, tgt: str) -> str:
        return self.en if tgt == "en" else self.fr


@dataclass
class Price:
    input: float
    output: float


def test_chrf() -> None:
    check(metrics.chrf("hello world", "hello world") == 100.0, "chrf identical != 100")
    check(metrics.chrf("aaa", "zzz") == 0.0, "chrf disjoint != 0")
    check(metrics.chrf("", "ref") == 0.0, "chrf empty hyp != 0")
    # hyp "abc", ref "abd" (max_n=6, orders 4..6 skipped: no n-gram):
    # n=1: 2 matches / 3 -> P=R=2/3; n=2: {ab,bc} vs {ab,bd}: 1/2;
    # n=3: {abc} vs {abd}: 0. P = R = (2/3 + 1/2 + 0)/3 = 7/18.
    # With P == R, F = P, so chrF = 100 * 7/18 = 38.8889.
    value = metrics.chrf("abc", "abd")
    check(abs(value - 38.8889) < 5e-5, f"chrf hand-computed: {value}")
    # hyp "ab", ref "abc" (P != R, beta = 2 weighs recall): n=1: 2 matches,
    # P=2/2, R=2/3; n=2: {ab} vs {ab,bc}: P=1/1, R=1/2; n>=3: hyp has none.
    # P = 1, R = (2/3 + 1/2)/2 = 7/12; F = 5*P*R / (4*P + R) = (35/12)/(55/12) = 7/11
    # chrF = 63.6364 (a plain F1 would give 73.6842).
    value = metrics.chrf("ab", "abc")
    check(abs(value - 63.6364) < 5e-5, f"chrf beta case: {value}")


def test_similarity() -> None:
    check(metrics.similarity("Hello  World", "hello world") == 1.0, "similarity case/space")
    check(metrics.similarity("aaaa", "zzzz") < 0.2, "similarity disjoint")
    check(metrics.normalise("  A\tB \n C ") == "a b c", "normalise")


def test_glossary() -> None:
    entries = [
        Entry("每股盈利", "earnings per share"),
        Entry("基本每股盈利", "basic earnings per share"),
        Entry("董事会", "board of directors"),
    ]
    # Overlap: only the long term counts; the nested short one is masked.
    check(
        metrics.glossary_hits("基本每股盈利为1元", "Basic earnings per share is 1", entries, "en")
        == (1, 1),
        "overlapping term counted once",
    )
    check(
        metrics.glossary_hits("每股盈利和董事会", "Earnings per share and the Board", entries, "en")
        == (1, 2),
        "one hit of two",
    )
    # A term repeated in the source counts once.
    check(
        metrics.glossary_hits("董事会,董事会", "the board of directors", entries, "en") == (1, 1),
        "distinct term once",
    )
    pairs = [
        ("基本每股盈利", "basic earnings per share"),
        ("董事会", "the committee"),
    ]
    check(metrics.glossary_adherence(pairs, entries, "en") == 0.5, "micro average")
    check(metrics.glossary_adherence([("无", "none")], entries, "en") is None, "None when absent")


def test_fallback_checks() -> None:
    ok = metrics.fallback_checks("5.30港元", "HKD5.30")
    check(ok == {"non_empty": True, "numbers_preserved": True}, f"fallback pass: {ok}")
    bad = metrics.fallback_checks("5.30港元", "HKD5")
    check(bad["numbers_preserved"] is False, "fallback fail")
    check(metrics.fallback_checks("1,234", "1234")["numbers_preserved"], "commas ignored")
    check(metrics.fallback_checks("x", "  ")["non_empty"] is False, "empty output")


def test_stats() -> None:
    check(metrics.mean_std([1, 2, 3]) == (2.0, 1.0), "mean_std")
    check(metrics.mean_std([5]) == (5.0, 0.0), "mean_std single")
    try:
        metrics.mean_std([])
        check(False, "mean_std empty did not raise")
    except ValueError:
        pass
    check(metrics.p95(list(range(1, 21))) == 19, "p95 of 20 values")


def test_cost() -> None:
    check(metrics.cost_usd(1000, 500, Price(1.0, 5.0)) == 0.0035, "cost attrs")
    check(metrics.cost_usd(1000, 500, {"input": 1.0, "output": 5.0}) == 0.0035, "cost dict")
    check(metrics.cost_usd(1000, 500, None) is None, "cost None")


def main() -> int:
    tests = [
        test_chrf,
        test_similarity,
        test_glossary,
        test_fallback_checks,
        test_stats,
        test_cost,
    ]
    for fn in tests:
        before = len(FAILURES)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append(f"{fn.__name__} raised {exc!r}")
        print(f"[{'OK' if len(FAILURES) == before else 'FAIL'}] {fn.__name__}")
    print(f"{len(tests)} tests, {len(FAILURES)} failure(s)")
    for message in FAILURES:
        print(f"  - {message}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
