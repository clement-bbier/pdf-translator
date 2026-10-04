"""SubagentStop hook: a workflow agent may only stop after writing a valid handoff.

For the six workflow agents (planner, implementer, reviewer, evaluator,
security-auditor, docs-writer) the hook looks for
``.claude/handoffs/<task_id>.<agent>.json`` written since the agent started,
in the agent's own checkout (its worktree for the implementer) and in the main
checkout. The newest one is validated against the Pydantic model in
handoff.py. Missing or invalid: exit 2, and the error goes back to the agent,
which cannot stop until it fixes the file.

Safety valve: after MAX_ATTEMPTS refusals the agent is let go with a warning
to the user, so a confused agent cannot loop forever; the orchestrator then
treats the missing handoff as ``blocked``.

Run through run_hook.py (needs pydantic from the venv).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import ALLOW, BLOCK, project_dir, read_input  # noqa: E402
from handoff import AGENT_NAMES, format_errors, validate_file  # noqa: E402
from record_subagent_start import run_record_path  # noqa: E402

MAX_ATTEMPTS = 4

# Without a start record, accept handoffs written within this window.
FALLBACK_WINDOW_SECONDS = 6 * 3600

HANDOFFS = Path(".claude") / "handoffs"


def _load_record(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def candidate_dirs(event: dict) -> list[Path]:
    dirs: list[Path] = []
    for root in (event.get("cwd"), str(project_dir(event))):
        if root and (Path(root) / HANDOFFS) not in dirs:
            dirs.append(Path(root) / HANDOFFS)
    # Worktree agents write inside their worktree; cwd may already point there,
    # but scan the default worktree location too in case it does not.
    worktrees = project_dir(event) / ".claude" / "worktrees"
    if worktrees.is_dir():
        dirs.extend(path / HANDOFFS for path in worktrees.iterdir() if (path / HANDOFFS).is_dir())
    return dirs


def find_handoffs(dirs: list[Path], agent: str, since: float) -> list[Path]:
    """Fresh handoffs of the first directory (in priority order) that has any.

    The agent's own checkout comes first, so parallel implementers in sibling
    worktrees never pick up each other's file.
    """
    for directory in dirs:
        if not directory.is_dir():
            continue
        found = [
            path
            for path in directory.glob(f"*.{agent}.json")
            if path.stat().st_mtime >= since - 1.0
        ]
        if found:
            return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)
    return []


def main() -> int:
    event = read_input()
    agent = str(event.get("agent_type") or "")
    if agent not in AGENT_NAMES:
        return ALLOW

    root = project_dir(event)
    record_path = run_record_path(root, str(event.get("agent_id") or "unknown"))
    record = _load_record(record_path)
    since = float(record.get("started_at") or time.time() - FALLBACK_WINDOW_SECONDS)

    handoffs = find_handoffs(candidate_dirs(event), agent, since)
    if handoffs:
        try:
            validate_file(handoffs[0])
            return ALLOW
        except Exception as error:  # noqa: BLE001 - every failure is reported to the agent
            problem = (
                f"Your handoff {handoffs[0].name} is invalid:\n{format_errors(error)}\n"
                "Fix the file (schema: .claude/schemas/handoff.schema.json), then stop again."
            )
    else:
        problem = (
            f"No handoff found for agent '{agent}'. Before stopping, write "
            f".claude/handoffs/<task_id>.{agent}.json in your working checkout, valid "
            "against .claude/schemas/handoff.schema.json."
        )

    attempts = int(record.get("attempts") or 0) + 1
    if record:
        record["attempts"] = attempts
        record_path.write_text(json.dumps(record), encoding="utf-8")
    if attempts > MAX_ATTEMPTS:
        print(
            json.dumps(
                {
                    "systemMessage": (
                        f"{agent} stopped without a valid handoff after {MAX_ATTEMPTS} "
                        "attempts; treat its task as blocked."
                    )
                }
            )
        )
        return ALLOW

    print(f"Blocked by .claude/hooks/validate_handoff.py: {problem}", file=sys.stderr)
    return BLOCK


if __name__ == "__main__":
    raise SystemExit(main())
