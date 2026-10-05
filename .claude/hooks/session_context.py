"""SessionStart hook: give Claude a short picture of where the work stands.

Emits (as additionalContext): current branch and dirty-file count, open
workflow runs (latest handoff per task in .claude/handoffs/), the plan's
progress (checkboxes in PLAN.md), and the last evaluation result from
evaluation/reports/latest.json. A few lines only; never blocks.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import ALLOW, project_dir, read_input  # noqa: E402


def _git(args: list[str], root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip()


def branch_line(root: Path) -> str:
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], root) or "unknown"
    dirty = len([line for line in _git(["status", "--porcelain"], root).splitlines() if line])
    return f"Branch: {branch} ({dirty} uncommitted file(s))"


def plan_line(root: Path) -> str:
    plan = root / "PLAN.md"
    if not plan.is_file():
        return "Plan: no PLAN.md"
    text = plan.read_text(encoding="utf-8")
    done = len(re.findall(r"^\s*[-*] \[x\]", text, re.MULTILINE | re.IGNORECASE))
    todo = len(re.findall(r"^\s*[-*] \[ \]", text, re.MULTILINE))
    return f"Plan: PLAN.md, {done}/{done + todo} checklist items done"


def handoff_line(root: Path) -> str:
    directory = root / ".claude" / "handoffs"
    files = (
        sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if directory.is_dir()
        else []
    )
    if not files:
        return "Workflow: no handoffs recorded"
    latest = files[-1]
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
        status = f"{data.get('agent')} -> {data.get('status')}, next: {data.get('next_agent')}"
    except (OSError, ValueError):
        status = "unreadable"
    return f"Workflow: {len(files)} handoff(s); latest {latest.name} ({status})"


def eval_line(root: Path) -> str:
    report = root / "evaluation" / "reports" / "latest.json"
    if not report.is_file():
        return "Evaluation: no report yet (run `make eval`)"
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "Evaluation: latest.json unreadable"
    verdict = data.get("verdict", "unknown")
    headline = data.get("headline", "")
    when = data.get("generated_at", "?")
    return f"Evaluation: {verdict} at {when}. {headline}".strip()


def main() -> int:
    event = read_input()
    root = project_dir(event)
    lines = [branch_line(root), plan_line(root), handoff_line(root), eval_line(root)]
    context = "Project status (from .claude/hooks/session_context.py):\n- " + "\n- ".join(lines)
    print(
        json.dumps(
            {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
        )
    )
    return ALLOW


if __name__ == "__main__":
    raise SystemExit(main())
