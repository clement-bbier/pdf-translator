"""PreToolUse hook (Read|Grep): never let an agent read a .env file.

Complements the permission deny rules in settings.json, which cannot express
"every .env* file except .env.example". Stdlib only.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import ALLOW, block, normalize_path, read_input  # noqa: E402


def check_path(path_str: str) -> str | None:
    name = Path(normalize_path(path_str)).name
    if name.startswith(".env") and name != ".env.example":
        return f"{name} holds secrets and must not be read; .env.example lists the variables"
    return None


def main() -> int:
    event = read_input()
    tool_input = event.get("tool_input") or {}
    candidates = [tool_input.get("file_path"), tool_input.get("path"), tool_input.get("glob")]
    for candidate in candidates:
        if candidate:
            reason = check_path(str(candidate))
            if reason:
                return block(f"Blocked by .claude/hooks/guard_read.py: {reason}")
    return ALLOW


if __name__ == "__main__":
    raise SystemExit(main())
