"""Run a hook script with the project's virtual-environment interpreter.

Usage (from settings.json): python run_hook.py <hook_name>

Hooks that need third-party packages (pydantic for the handoff schema, ruff
for linting) must run inside the project's venv, but Claude Code starts hooks
with whatever ``python`` is on PATH. This launcher finds the venv of the main
checkout (also from inside a git worktree, which has no venv of its own),
forwards stdin, and propagates the exit code and output unchanged.
Stdlib only.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = HOOKS_DIR.parent.parent


def venv_python(root: Path = PROJECT_DIR) -> str:
    """Return the venv interpreter under ``root`` if present, else the current one."""
    for candidate in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not (HOOKS_DIR / f"{argv[0]}.py").is_file():
        print(f"run_hook.py: unknown hook {argv!r}", file=sys.stderr)
        return 1
    script = HOOKS_DIR / f"{argv[0]}.py"
    completed = subprocess.run(
        [venv_python(), str(script)],
        input=sys.stdin.buffer.read(),
        capture_output=True,
    )
    sys.stdout.buffer.write(completed.stdout)
    sys.stderr.buffer.write(completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
