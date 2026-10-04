"""Install a git pre-commit hook that runs the same secret scan as Claude Code.

The PreToolUse hook only sees commits made by an agent. This git hook covers
commits made by hand, with the same rules and the same local forbidden-terms
list. Idempotent; refuses to overwrite a pre-commit hook it did not write.

    python scripts/install_git_hooks.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MARKER = "# installed by scripts/install_git_hooks.py"

HOOK = f"""#!/bin/sh
{MARKER}
root="$(git rev-parse --show-toplevel)"
py="$root/.venv/Scripts/python.exe"
[ -x "$py" ] || py="$root/.venv/bin/python"
[ -x "$py" ] || py=python
exec "$py" "$root/.claude/hooks/secret_scan.py" --staged --root "$root"
"""


def main() -> int:
    hooks_dir = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-path", "hooks"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    target = Path(hooks_dir) / "pre-commit"
    if target.exists() and MARKER not in target.read_text(encoding="utf-8", errors="replace"):
        print(
            f"{target} exists and was not written by this script; not overwriting", file=sys.stderr
        )
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(HOOK, encoding="utf-8", newline="\n")
    target.chmod(0o755)
    print(f"installed {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
