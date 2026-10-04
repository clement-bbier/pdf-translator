"""SubagentStart hook: remember when each workflow agent started.

validate_handoff.py (SubagentStop) uses this timestamp to tell a handoff the
agent just wrote from a stale one left by an earlier run. Records live in
``.claude/handoffs/.runs/<agent_id>.json`` of the main checkout (runtime state,
gitignored). Never blocks.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import ALLOW, project_dir, read_input  # noqa: E402

RUNS_DIR = Path(".claude") / "handoffs" / ".runs"


def run_record_path(root: Path, agent_id: str) -> Path:
    safe = "".join(char for char in agent_id if char.isalnum() or char in "-_") or "unknown"
    return root / RUNS_DIR / f"{safe}.json"


def main() -> int:
    event = read_input()
    agent_id = str(event.get("agent_id") or "")
    if not agent_id:
        return ALLOW
    path = run_record_path(project_dir(event), agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "agent_id": agent_id,
        "agent_type": event.get("agent_type"),
        "started_at": time.time(),
        "attempts": 0,
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    return ALLOW


if __name__ == "__main__":
    raise SystemExit(main())
