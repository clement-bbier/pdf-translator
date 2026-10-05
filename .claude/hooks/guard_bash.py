"""PreToolUse hook (Bash|PowerShell): block destructive commands and .env reads.

Blocked:
- recursive forced deletion (``rm -rf`` and its spellings, ``Remove-Item -Recurse -Force``);
- force pushes (``--force``, ``-f``, ``--force-with-lease``, ``+refspec``);
- ``git reset --hard`` while on main/master (or targeting them);
- ``git clean -f`` (deletes untracked work irrecoverably);
- any command reading a ``.env`` file other than ``.env.example``;
- shell writes into the protected evaluation references (see guard_write.py),
  and ``make eval-baseline``, unless AGENTIC_ALLOW_REFERENCE_EDIT=1 was set by
  the human who launched Claude Code.

This is a guard rail against mistakes, not a sandbox: a determined command can
always be obfuscated. The permission deny rules in settings.json are a second
layer.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import ALLOW, block, read_input  # noqa: E402

PROTECTED_BRANCHES = {"main", "master"}

# A .env file other than the committed template: .env, .env.local, .env.prod...
_ENV_FILE_RE = re.compile(r"(?<![\w.-])\.env(?:\.(?!example\b)[\w.-]+)?(?![\w.-])")

_SEPARATORS_RE = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")

_REFERENCE_PATH_RE = re.compile(r"evaluation[/\\](?:dataset[/\\]|baseline[\w.-]*\.json)")
_WRITE_OPS_RE = re.compile(
    r"(?:>|\btee\b|\bsed\s+-i|\bcp\b|\bmv\b|\brm\b|\btruncate\b|Set-Content|Out-File"
    r"|Add-Content|Remove-Item|Move-Item|Copy-Item|\bwrite_text\b|\bopen\()"
)


def _split_commands(command: str) -> list[list[str]]:
    """Split a command line into simple commands, each tokenized."""
    result: list[list[str]] = []
    for part in _SEPARATORS_RE.split(command):
        part = part.strip()
        if not part:
            continue
        try:
            tokens = shlex.split(part, posix=True)
        except ValueError:
            tokens = part.split()
        # Drop leading env assignments (FOO=bar cmd) and wrappers.
        while tokens and (re.match(r"^\w+=", tokens[0]) or tokens[0] in {"sudo", "command", "env"}):
            tokens = tokens[1:]
        if tokens:
            result.append(tokens)
    return result


def _short_flags(tokens: list[str]) -> set[str]:
    """Collect single-letter flags, expanding bundles like -rf."""
    flags: set[str] = set()
    for token in tokens:
        if token.startswith("--"):
            flags.add(token)
        elif token.startswith("-") and len(token) > 1:
            flags.update(f"-{char}" for char in token[1:])
    return flags


def _is_rm_rf(tokens: list[str]) -> bool:
    if Path(tokens[0]).name not in {"rm", "rm.exe"}:
        return False
    flags = _short_flags(tokens[1:])
    recursive = bool({"-r", "-R", "--recursive"} & flags)
    forced = bool({"-f", "--force"} & flags)
    return recursive and forced


def _is_remove_item_recurse_force(tokens: list[str]) -> bool:
    if tokens[0].lower() not in {"remove-item", "ri", "del", "rmdir", "rd"}:
        return False
    lowered = {token.lower() for token in tokens[1:]}
    return bool({"-recurse", "-r"} & lowered) and "-force" in lowered


def _git_args(tokens: list[str]) -> list[str] | None:
    """Return the git sub-arguments with global options (-C dir, -c k=v) skipped."""
    if Path(tokens[0]).name not in {"git", "git.exe"}:
        return None
    args = tokens[1:]
    while args and args[0].startswith("-"):
        args = args[2:] if args[0] in {"-C", "-c"} else args[1:]
    return args


def _current_branch(cwd: str | None) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip()


def _check_git(args: list[str], cwd: str | None) -> str | None:
    if not args:
        return None
    sub, rest = args[0], args[1:]

    if sub == "push":
        flags = _short_flags(rest)
        forced = {"--force", "-f", "--force-with-lease", "--force-if-includes"} & flags
        plus_refspec = any(token.startswith("+") for token in rest)
        if forced or plus_refspec:
            return "force push is forbidden: push a new branch or ask the human"

    if sub == "reset" and "--hard" in rest:
        targets = {token.split("/")[-1] for token in rest if not token.startswith("-")}
        if targets & PROTECTED_BRANCHES or _current_branch(cwd) in PROTECTED_BRANCHES:
            return "git reset --hard on main/master is forbidden"

    if sub == "clean" and any(flag in _short_flags(rest) for flag in ("-f", "--force")):
        return "git clean -f deletes untracked work irrecoverably; remove files explicitly"

    return None


def _reference_override() -> bool:
    return os.environ.get("AGENTIC_ALLOW_REFERENCE_EDIT") == "1"


_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\2(?=\s|$)", re.DOTALL)
_COMMIT_MESSAGE_RE = re.compile(r"(\s-m\s*|\s--message[=\s])(\"(?:[^\"\\]|\\.)*\"|'[^']*')")


def _without_commit_messages(command: str) -> str:
    """Drop commit message text, which may legitimately mention .env or rm -rf.

    Only messages of ``git commit`` are dropped: a heredoc feeding any other
    command (``python - <<EOF``) is code and stays subject to every check.
    """
    if not re.search(r"\bgit\s+(?:-C\s+\S+\s+)?commit\b", command):
        return command

    def _strip_heredoc(match: re.Match[str]) -> str:
        line_start = command.rfind("\n", 0, match.start()) + 1
        prefix = command[line_start : match.start()]
        return "<<MSG" if re.search(r"\bgit\s+(?:-C\s+\S+\s+)?commit\b", prefix) else match.group(0)

    command = _HEREDOC_RE.sub(_strip_heredoc, command)
    return _COMMIT_MESSAGE_RE.sub(r"\1''", command)


def check_command(command: str, cwd: str | None = None) -> str | None:
    """Return a reason to block ``command``, or None when it may run."""
    command = _without_commit_messages(command)
    env_match = _ENV_FILE_RE.search(command)
    if env_match:
        return (
            f"command touches {env_match.group(0)!r}: .env files hold secrets and must "
            "never be read by an agent (use .env.example for the variable names)"
        )

    if not _reference_override():
        if _REFERENCE_PATH_RE.search(command) and _WRITE_OPS_RE.search(command):
            return (
                "command would write to the evaluation references (evaluation/dataset, "
                "evaluation/baseline*.json); only a human may change them"
            )
        if re.search(r"\bmake\s+(?:\S+\s+)*eval-baseline\b", command):
            return "updating the evaluation baseline is a human decision (make eval-baseline)"

    for tokens in _split_commands(command):
        if _is_rm_rf(tokens) or _is_remove_item_recurse_force(tokens):
            return "recursive forced deletion is forbidden: delete specific files instead"
        git_args = _git_args(tokens)
        if git_args is not None:
            reason = _check_git(git_args, cwd)
            if reason:
                return reason
    return None


def main() -> int:
    event = read_input()
    tool_input = event.get("tool_input") or {}
    command = str(tool_input.get("command") or "")
    if not command:
        return ALLOW
    reason = check_command(command, event.get("cwd"))
    if reason:
        return block(f"Blocked by .claude/hooks/guard_bash.py: {reason}")
    return ALLOW


if __name__ == "__main__":
    raise SystemExit(main())
