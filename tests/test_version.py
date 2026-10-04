"""Tests for the single source of truth of the application version.

No network, no package build. Run: python tests/test_version.py
"""

from __future__ import annotations

import contextlib
import io
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from app import extractor, pipeline, rebuilder, scanned  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def test_version_format() -> None:
    check(isinstance(app.__version__, str), "__version__ is not a str")
    check(
        re.match(r"^\d+\.\d+\.\d+", str(app.__version__)) is not None,
        f"__version__ has an unexpected format: {app.__version__!r}",
    )


def test_pyproject_dynamic_version() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    check("version" not in project, "pyproject.toml still has a literal version")
    check("version" in project.get("dynamic", []), "version is not declared dynamic")
    attr = data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    check(attr == "app.__version__", f"unexpected version attr: {attr!r}")


def test_version_flag_all_modules() -> None:
    for module in (pipeline, extractor, rebuilder, scanned):
        name = module.__name__
        buf = io.StringIO()
        returned = False
        code: object = "unset"
        try:
            with contextlib.redirect_stdout(buf):
                module.main(["--version"])
            returned = True
        except SystemExit as exc:
            code = exc.code
        check(not returned, f"{name}: main returned normally on --version")
        check(code in (0, None), f"{name}: unexpected exit code {code!r}")
        check(
            buf.getvalue().strip() == f"pdf-translator {app.__version__}",
            f"{name}: unexpected output {buf.getvalue()!r}",
        )


def test_version_flag_subprocess() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.pipeline", "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    check(result.returncode == 0, f"subprocess exit code {result.returncode}")
    check(
        result.stdout.strip() == f"pdf-translator {app.__version__}",
        f"subprocess output {result.stdout!r}",
    )


def main() -> int:
    tests = [
        test_version_format,
        test_pyproject_dynamic_version,
        test_version_flag_all_modules,
        test_version_flag_subprocess,
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
