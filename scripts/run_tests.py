"""Run every test module (tests/test_*.py) and report one aggregate result.

The test modules are plain scripts (no pytest dependency): each one exits 0
when all of its checks pass. This runner executes them one by one with the
same interpreter, prints a line per module and a summary, and exits non-zero
if any module failed.

    python scripts/run_tests.py            # all modules
    python scripts/run_tests.py hooks auth # only modules whose name contains a filter
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"

_COUNT_RE = re.compile(r"(\d+) tests?, (\d+) failure")


def main(argv: list[str]) -> int:
    modules = sorted(TESTS_DIR.glob("test_*.py"))
    if argv:
        modules = [path for path in modules if any(token in path.stem for token in argv)]
    if not modules:
        print("no test module matched")
        return 1

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    failed_modules: list[str] = []
    total_tests = total_failures = 0
    started = time.monotonic()

    for path in modules:
        t0 = time.monotonic()
        completed = subprocess.run(
            [sys.executable, str(path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        output = completed.stdout + completed.stderr
        match = _COUNT_RE.search(output)
        tests, failures = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
        total_tests += tests
        total_failures += failures
        ok = completed.returncode == 0
        print(
            f"[{'OK' if ok else 'FAIL'}] {path.name}: {tests} tests, {failures} failure(s) "
            f"({time.monotonic() - t0:.1f}s)"
        )
        if not ok:
            failed_modules.append(path.name)
            print("\n".join("    " + line for line in output.strip().splitlines()[-25:]))

    print(
        f"\n{len(modules)} module(s), {total_tests} tests, {total_failures} failure(s), "
        f"{len(failed_modules)} failed module(s) in {time.monotonic() - started:.1f}s"
    )
    return 1 if failed_modules else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
