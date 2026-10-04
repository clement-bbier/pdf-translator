"""Tests for the translation evaluation dataset and glossary loaders.

Invalid inputs use temp files only. Run: python tests/test_translation_dataset.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.translation_dataset import load_glossary, load_segments  # noqa: E402

FAILURES: list[str] = []

ROW = {
    "id": "hk-001",
    "source": "收入",
    "reference": "Revenue",
    "domain": "finance",
    "issuer": "Issuer (0001)",
    "doc_title": "Title",
    "date": "2026-03-18",
    "source_url_zh": "https://example.org/a_c.pdf",
    "source_url_en": "https://example.org/a.pdf",
    "page_zh": 1,
    "page_en": 1,
}


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def _raises_value_error(path: Path, expected: int | None = 1) -> bool:
    try:
        load_segments(path, expected=expected)
    except ValueError:
        return True
    return False


def _write(tmp: Path, lines: list[str]) -> Path:
    path = tmp / "data.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_real_dataset() -> None:
    segments = load_segments()
    check(len(segments) == 30, f"expected 30 rows, got {len(segments)}")
    ids = [s.id for s in segments]
    check(len(set(ids)) == len(ids), "ids are not unique")


def test_real_glossary() -> None:
    entries = load_glossary()
    check(len(entries) > 0, "glossary is empty")
    check(all(e.target("en") == e.target_en for e in entries), "target('en') mismatch")
    check(all(e.target("fr") == e.target_fr for e in entries), "target('fr') mismatch")


def test_valid_temp_file() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = _write(Path(d), [json.dumps(ROW, ensure_ascii=False), ""])
        check(len(load_segments(path, expected=1)) == 1, "valid temp file did not load")


def test_invalid_inputs() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dumps = lambda r: json.dumps(r, ensure_ascii=False)  # noqa: E731
        check(
            _raises_value_error(_write(tmp, [dumps(ROW), dumps(ROW)]), expected=2),
            "duplicate id did not raise",
        )
        missing = {k: v for k, v in ROW.items() if k != "reference"}
        check(_raises_value_error(_write(tmp, [dumps(missing)])), "missing field did not raise")
        check(
            _raises_value_error(_write(tmp, [dumps({**ROW, "extra": 1})])),
            "extra field did not raise",
        )
        check(
            _raises_value_error(_write(tmp, [dumps(ROW)]), expected=30),
            "wrong row count did not raise",
        )
        check(_raises_value_error(_write(tmp, ["{not json"])), "malformed JSON did not raise")


def test_glossary_empty_target() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "g.csv"
        path.write_text("source,target_fr,target_en,domain\n收入,,revenue,accounting\n", "utf-8")
        try:
            load_glossary(path)
        except ValueError:
            return
        FAILURES.append("empty glossary target did not raise")


def main() -> int:
    tests = [
        test_real_dataset,
        test_real_glossary,
        test_valid_temp_file,
        test_invalid_inputs,
        test_glossary_empty_target,
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
