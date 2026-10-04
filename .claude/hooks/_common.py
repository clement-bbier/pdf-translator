"""Shared helpers for the Claude Code hooks in this directory.

Stdlib only: the guard hooks run with whatever ``python`` is on PATH, before
any virtual environment is guaranteed to exist.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Hook exit codes (Claude Code contract): 0 = proceed, 2 = block and feed
# stderr back to Claude. Any other code is a non-blocking error.
ALLOW = 0
BLOCK = 2

HOOKS_DIR = Path(__file__).resolve().parent


def read_input() -> dict[str, Any]:
    """Parse the JSON event Claude Code sends on stdin; empty dict when absent."""
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def project_dir(event: dict[str, Any] | None = None) -> Path:
    """Return the project root: $CLAUDE_PROJECT_DIR, else the repo holding this file."""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    return HOOKS_DIR.parent.parent


def block(reason: str) -> int:
    """Print the reason for Claude and return the blocking exit code."""
    print(reason, file=sys.stderr)
    return BLOCK


def normalize_path(raw: str) -> str:
    """Return a forward-slash, lower-cased path for pattern matching on any OS."""
    return raw.replace("\\", "/").lower()


def relative_to_project(path_str: str, event: dict[str, Any]) -> str:
    """Express ``path_str`` relative to the event's cwd/project when possible.

    Agents in a worktree write under ``.claude/worktrees/<name>/``; that prefix
    is stripped so the same rules apply to the main checkout and to worktrees.
    """
    norm = normalize_path(path_str)
    roots = [event.get("cwd") or "", str(project_dir(event))]
    for root in roots:
        if not root:
            continue
        root_norm = normalize_path(root).rstrip("/") + "/"
        if norm.startswith(root_norm):
            norm = norm[len(root_norm) :]
            break
    marker = ".claude/worktrees/"
    if norm.startswith(marker):
        norm = norm[len(marker) :].split("/", 1)[-1]
    return norm.removeprefix("./")
