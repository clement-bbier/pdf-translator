"""Tests for scripts/validate_agentic_config.py and the evaluation comparison.

The validator runs on a copy of the configuration, mutated to break one rule
at a time, and must report each breakage. Run: python tests/test_agentic_config.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.run import compare, verdict_of  # noqa: E402

FAILURES: list[str] = []

COPIED = [".claude/agents", ".claude/skills", ".claude/hooks", ".claude/schemas"]
COPIED_FILES = [
    ".claude/settings.json",
    ".gitignore",
    "CLAUDE.md",
    "tests/test_hooks.py",
    "scripts/validate_agentic_config.py",
]


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def _copy_config(tmp: Path) -> Path:
    for rel in COPIED:
        shutil.copytree(ROOT / rel, tmp / rel, ignore=shutil.ignore_patterns("__pycache__"))
    for rel in COPIED_FILES:
        (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, tmp / rel)
    return tmp


def _validate(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "validate_agentic_config.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_repository_config_is_valid() -> None:
    """The committed configuration passes."""
    result = _validate(ROOT)
    check(result.returncode == 0, f"validator fails on the repo:\n{result.stdout}")


def test_validator_catches_breakages() -> None:
    """Each mutation is reported with a message naming the problem."""
    mutations: list[tuple[str, Callable[[Path], None], str]] = [
        (
            "implementer without worktree",
            lambda r: _replace(r / ".claude/agents/implementer.md", "isolation: worktree\n", ""),
            "isolation: worktree",
        ),
        (
            "reviewer allowed to edit",
            lambda r: _replace(
                r / ".claude/agents/reviewer.md", "tools: Read, Grep", "tools: Read, Edit, Grep"
            ),
            "read-only role must not have",
        ),
        (
            "missing agent",
            lambda r: (r / ".claude/agents/evaluator.md").unlink(),
            "missing definitions",
        ),
        (
            "unknown model",
            lambda r: _replace(r / ".claude/agents/planner.md", "model: opus", "model: gpt-x"),
            "is not an alias",
        ),
        (
            "schema drift",
            lambda r: _replace(
                r / ".claude/schemas/handoff.schema.json", '"needs_review"', '"wip"'
            ),
            "out of date",
        ),
        (
            "SubagentStop does not cover an agent",
            lambda r: _replace(r / ".claude/settings.json", '|docs-writer"', '"'),
            "does not cover docs-writer",
        ),
        (
            "decorative hook",
            lambda r: (r / ".claude/hooks/unused_hook.py").write_text("x = 1\n", encoding="utf-8"),
            "not wired to any event",
        ),
        (
            "handoffs versioned",
            lambda r: _replace(r / ".gitignore", ".claude/handoffs/\n", ""),
            ".claude/handoffs/",
        ),
        (
            "workflow skill auto-invocable",
            lambda r: _replace(
                r / ".claude/skills/feature/SKILL.md", "disable-model-invocation: true\n", ""
            ),
            "disable-model-invocation",
        ),
        (
            "worktrees not based on HEAD",
            lambda r: _replace(
                r / ".claude/settings.json", '"baseRef": "head"', '"baseRef": "fresh"'
            ),
            "baseRef",
        ),
    ]
    for label, mutate, expected in mutations:
        with tempfile.TemporaryDirectory() as tmp:
            root = _copy_config(Path(tmp))
            mutate(root)
            result = _validate(root)
            check(result.returncode == 1, f"{label}: validator should fail")
            check(expected in result.stdout, f"{label}: expected {expected!r} in:\n{result.stdout}")


def test_evaluation_compare_classifies_metrics() -> None:
    """Direction-aware comparison: regression, improvement, unchanged, new."""
    baseline = {"layout": {"written_ratio": 0.9, "failed_blocks": 2.0, "shrunk_ratio": 0.5}}
    current = {
        "layout": {
            "written_ratio": 0.8,  # higher is better -> regression
            "failed_blocks": 1.0,  # lower is better -> improvement
            "shrunk_ratio": 0.5,  # unchanged
            "missing_glyphs": 0.0,  # not in baseline -> new
        }
    }
    rows = {row["metric"]: row["status"] for row in compare(current, baseline)}
    check(rows["written_ratio"] == "regression", f"written_ratio: {rows}")
    check(rows["failed_blocks"] == "improvement", f"failed_blocks: {rows}")
    check(rows["shrunk_ratio"] == "unchanged", f"shrunk_ratio: {rows}")
    check(rows["missing_glyphs"] == "new", f"missing_glyphs: {rows}")
    check(verdict_of(compare(current, baseline)) == "regression", "any regression wins")
    better = {"layout": {"written_ratio": 0.95, "failed_blocks": 2.0, "shrunk_ratio": 0.5}}
    check(verdict_of(compare(better, baseline)) == "improvement", "improvement verdict")


def main() -> int:
    tests = [
        test_repository_config_is_valid,
        test_validator_catches_breakages,
        test_evaluation_compare_classifies_metrics,
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
