"""PreToolUse hook (Edit|Write|MultiEdit|NotebookEdit): protect secrets and eval references.

Blocked:
- any write to a ``.env*`` file, except the committed template ``.env.example``;
- any modification of an existing evaluation reference: files under
  ``evaluation/dataset/`` and ``evaluation/baseline*.json``. Creating a
  reference that does not exist yet is allowed (that is how a dataset is first
  added); changing one is not, so no agent can raise a score by editing the
  expected answers. A human can lift the rule for a session by launching
  Claude Code with AGENTIC_ALLOW_REFERENCE_EDIT=1.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import ALLOW, block, normalize_path, read_input, relative_to_project  # noqa: E402

_REFERENCE_RE = re.compile(r"^evaluation/(?:dataset/.+|baseline[\w.-]*\.json)$")


def check_path(path_str: str, event: dict) -> str | None:
    """Return a reason to block a write to ``path_str``, or None."""
    name = Path(normalize_path(path_str)).name
    if name.startswith(".env") and name != ".env.example":
        return f"{name}: .env files hold secrets; edit .env.example instead"

    relative = relative_to_project(path_str, event)
    if _REFERENCE_RE.match(relative):
        if os.environ.get("AGENTIC_ALLOW_REFERENCE_EDIT") == "1":
            return None
        target = Path(path_str)
        if not target.is_absolute():
            target = Path(event.get("cwd") or ".") / target
        if target.exists():
            return (
                f"{relative} is an evaluation reference and already exists: only a human "
                "may change it (launch Claude Code with AGENTIC_ALLOW_REFERENCE_EDIT=1)"
            )
    return None


def main() -> int:
    event = read_input()
    tool_input = event.get("tool_input") or {}
    path_str = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if not path_str:
        return ALLOW
    reason = check_path(path_str, event)
    if reason:
        return block(f"Blocked by .claude/hooks/guard_write.py: {reason}")
    return ALLOW


if __name__ == "__main__":
    raise SystemExit(main())
