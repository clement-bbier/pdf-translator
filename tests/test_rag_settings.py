"""Tests for app.rag.settings and the shipped rag block of config.yaml.

No network access. Run: python tests/test_rag_settings.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.rag.settings import REPO_ROOT, RagConfigError, RagSettings, rag_settings  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def _rejects(section: object, key: str, label: str) -> None:
    try:
        rag_settings({"rag": section})
    except RagConfigError as error:
        text = str(error)
        check(key in text, f"{label}: message should name {key!r}: {text}")
        check("invalid 'rag' section" in text, f"{label}: message prefix: {text}")
    else:
        check(False, f"{label}: should be rejected")


def test_defaults_when_missing() -> None:
    for config in ({"provider": "mock"}, {"rag": None}, {}):
        settings = rag_settings(config)
        check(settings == RagSettings(), "missing section gives defaults")
        check(settings.glossary is True, "glossary on by default")
        check(settings.translation_memory is False, "translation memory off by default")
        check(settings.tm_top_k == 3 and settings.tm_min_score == 0.3, "default tm params")
        check(settings.embedder == "hashing", "default embedder")
        check(settings.embedding_model == "text-embedding-3-small", "default embedding model")


def test_shipped_config_equals_defaults() -> None:
    config = load_config(reload=True)
    check(isinstance(config, dict), "load_config returns a plain dict")
    check("rag" in config, "config.yaml has a rag block")
    check(rag_settings(config) == RagSettings(), "shipped rag block equals the defaults")


def test_invalid_values_rejected() -> None:
    _rejects({"tm_top_k": 0}, "tm_top_k", "tm_top_k 0")
    _rejects({"tm_top_k": 21}, "tm_top_k", "tm_top_k 21")
    _rejects({"tm_min_score": 2}, "tm_min_score", "tm_min_score 2")
    _rejects({"embedder": "bert"}, "embedder", "embedder bert")
    _rejects({"unknown_key": 1}, "unknown_key", "unknown key")
    _rejects({"glossary": "yes"}, "glossary", "glossary 'yes' (strict bool)")
    _rejects({"translation_memory": 1}, "translation_memory", "tm int (strict bool)")
    _rejects({"embedding_model": ""}, "embedding_model", "empty embedding_model")
    _rejects({"glossary_path": ""}, "glossary_path", "empty glossary_path")
    _rejects("on", "rag", "non-mapping section")


def test_error_does_not_echo_values() -> None:
    try:
        rag_settings({"rag": {"embedder": "SECRET-VALUE"}})
    except RagConfigError as error:
        check("SECRET-VALUE" not in str(error), "error must not echo the input value")
    else:
        check(False, "invalid embedder should raise")


def test_valid_override() -> None:
    settings = rag_settings(
        {"rag": {"translation_memory": True, "tm_top_k": 5, "embedder": "gateway"}}
    )
    check(settings.translation_memory is True and settings.tm_top_k == 5, "override applied")
    check(settings.embedder == "gateway", "embedder override")
    check(settings.glossary is True, "other keys keep defaults")


def test_paths_resolve_inside_repo() -> None:
    settings = RagSettings()
    check(
        settings.glossary_file == REPO_ROOT / "data" / "glossary" / "finance_zh.csv",
        "glossary path",
    )
    check(settings.tm_file == REPO_ROOT / "data" / "tm" / "seed_zh_en.jsonl", "tm path")
    check(REPO_ROOT == ROOT, "REPO_ROOT is the repo root")
    absolute = ROOT / "somewhere" / "g.csv"
    check(RagSettings(glossary_path=str(absolute)).glossary_file == absolute, "absolute kept")


def main() -> int:
    tests = [
        test_defaults_when_missing,
        test_shipped_config_equals_defaults,
        test_invalid_values_rejected,
        test_error_does_not_echo_values,
        test_valid_override,
        test_paths_resolve_inside_repo,
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
