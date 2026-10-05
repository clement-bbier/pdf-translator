"""PreToolUse hook: keep each workflow role inside its remit.

Wired in settings.json for Bash/PowerShell/Write/Edit. Tool calls made inside
a subagent carry ``agent_type`` in the hook input; the role is read from
there (an explicit ``python guard_role.py <role>`` argument overrides it, for
tests). Calls from the main session or from roles without restrictions
(implementer, built-in agents) pass through.

Why settings and not agent frontmatter: frontmatter hooks of project agents
only run once the workspace trust dialog has been accepted, and a headless
``claude -p`` run never accepts it. Settings hooks always run.

- Write/Edit: read-only roles (planner, reviewer, evaluator, security-auditor)
  may only write their handoff under ``.claude/handoffs/``; docs-writer may
  also write Markdown and files under ``docs/``.
- Bash/PowerShell: each read-only role gets an allowlist of command prefixes
  (run the tests, the evaluation, the scanners, read git state). Pipes into
  read-only filters (head, tail, grep...) are allowed; output redirection to a
  file is not.

The global guards (guard_bash.py, guard_write.py) still apply on top.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import ALLOW, block, read_input, relative_to_project  # noqa: E402

_GIT_READ = (
    "git diff",
    "git log",
    "git show",
    "git status",
    "git rev-parse",
    "git merge-base",
    "git branch",
    "git ls-files",
    "git grep",
    "git worktree list",
    # Every role validates its own handoff before stopping.
    "python .claude/hooks/handoff.py",
)
_TESTS = ("make test", "make lint", "make check", "python scripts/run_tests.py", "python tests/")

BASH_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "planner": _GIT_READ,
    "reviewer": _GIT_READ
    + _TESTS
    + (
        "make validate",
        "make security",
        "python -m ruff check",
        "ruff check",
        # Exercise the CLI the change touches (writes only under output/ and tmp/).
        "python -m app.",
    ),
    "evaluator": _GIT_READ + ("make eval", "python -m evaluation"),
    "security-auditor": _GIT_READ
    + (
        "make security",
        "make audit",
        "python .claude/hooks/secret_scan.py",
        "python -m pip_audit",
        "pip-audit",
    ),
}

# Commands that only read their input; allowed after a pipe or on their own.
_FILTERS = {"head", "tail", "grep", "wc", "sort", "uniq", "cat", "ls", "echo", "pwd", "cd", "true"}

_REDIRECT_RE = re.compile(r"(?<![0-9&])>{1,2}(?!&)|\btee\b")
_QUOTED_RE = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")


def _split_commands(command: str) -> list[str]:
    """Split on &&, ||, ;, | and newlines, ignoring separators inside quotes."""
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            current.append(char)
            if char == "\\" and quote == '"' and index + 1 < len(command):
                current.append(command[index + 1])
                index += 1
            elif char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
            current.append(char)
        elif command.startswith(("&&", "||"), index):
            parts.append("".join(current))
            current = []
            index += 1
        elif char in ";|\n":
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _normalize(part: str) -> str:
    """Rewrite interpreter paths (.venv/Scripts/python.exe ...) to plain ``python``."""
    try:
        tokens = shlex.split(part, posix=True)
    except ValueError:
        tokens = part.split()
    # Leading environment assignments (PYTHONIOENCODING=utf-8 python ...) do not
    # change which command runs.
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return ""
    head = Path(tokens[0].replace("\\", "/")).name.lower()
    if head.startswith("python"):
        tokens[0] = "python"
    elif head in {"make.exe", "git.exe"}:
        tokens[0] = head.removesuffix(".exe")
    return " ".join(tokens)


def check_bash(role: str, command: str) -> str | None:
    allowed = BASH_ALLOWLIST.get(role)
    if allowed is None:
        return None
    cleaned = re.sub(r"\s(?:2>&1|2>/dev/null|>/dev/null|2>nul)", " ", command)
    # A '>' inside a quoted argument (a grep pattern, a message) is not a redirect.
    if _REDIRECT_RE.search(_QUOTED_RE.sub("''", cleaned)):
        return f"{role} is read-only: output redirection to a file is not allowed"
    for part in _split_commands(cleaned):
        normalized = _normalize(part)
        if not normalized:
            continue
        if normalized.split()[0] in _FILTERS:
            continue
        if not any(
            normalized == prefix
            or normalized.startswith(prefix + " ")
            or (prefix.endswith(("/", ".")) and normalized.startswith(prefix))
            for prefix in allowed
        ):
            return (
                f"{role} may only run: {', '.join(allowed)} (plus read-only filters); "
                "read files with the Read/Grep/Glob tools. "
                f"Refused: {normalized[:120]!r}"
            )
    return None


def check_write(role: str, path_str: str, event: dict) -> str | None:
    relative = relative_to_project(path_str, event)
    if relative.startswith(".claude/handoffs/"):
        return None
    if role == "docs-writer":
        if relative.endswith(".md") or relative.startswith("docs/"):
            return None
        return "docs-writer may only write Markdown, files under docs/, and its handoff"
    return f"{role} is read-only: it may only write its handoff under .claude/handoffs/"


RESTRICTED_ROLES = set(BASH_ALLOWLIST) | {"docs-writer"}


def main(argv: list[str]) -> int:
    event = read_input()
    role = argv[0] if argv else str(event.get("agent_type") or "")
    if role not in RESTRICTED_ROLES:
        return ALLOW
    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") or {}

    if tool in {"Bash", "PowerShell"}:
        reason = check_bash(role, str(tool_input.get("command") or ""))
    elif tool in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        reason = check_write(role, path, event) if path else None
    else:
        reason = None

    if reason:
        return block(f"Blocked by .claude/hooks/guard_role.py: {reason}")
    return ALLOW


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
