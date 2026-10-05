"""PostToolUse hook (Edit|Write|MultiEdit): format and lint the Python file just written.

Runs ``ruff format`` then ``ruff check --fix`` on the edited file only. Issues
ruff cannot fix are sent back to Claude (exit 2 + stderr) so the agent fixes
them in its next step. The edit itself has already happened; nothing is undone.
Run through run_hook.py so ruff comes from the project's venv.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import ALLOW, BLOCK, read_input  # noqa: E402


def _ruff(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ruff", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _config_root(path: Path) -> Path:
    """Closest parent holding pyproject.toml, so the worktree's own config applies."""
    for parent in path.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return path.parent


def main() -> int:
    event = read_input()
    tool_input = event.get("tool_input") or {}
    path_str = str(tool_input.get("file_path") or "")
    if not path_str.endswith(".py"):
        return ALLOW
    path = Path(path_str)
    if not path.is_absolute():
        path = Path(event.get("cwd") or ".") / path
    if not path.is_file():
        return ALLOW

    root = _config_root(path)
    try:
        probe = _ruff("--version", cwd=root)
    except (OSError, subprocess.SubprocessError):
        probe = None
    if probe is None or probe.returncode != 0:
        print(
            "lint_python: ruff not installed (pip install -r requirements-dev.txt)", file=sys.stderr
        )
        return ALLOW

    _ruff("format", str(path), cwd=root)
    checked = _ruff("check", "--fix", "--output-format", "concise", str(path), cwd=root)
    if checked.returncode != 0:
        print(
            f"ruff found issues it could not fix in {path.name}; fix them now:\n"
            + (checked.stdout or checked.stderr).strip(),
            file=sys.stderr,
        )
        return BLOCK
    return ALLOW


if __name__ == "__main__":
    raise SystemExit(main())
