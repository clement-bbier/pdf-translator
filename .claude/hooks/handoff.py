"""Handoff schema: the structured report every agent writes before it stops.

Single source of truth. ``.claude/schemas/handoff.schema.json`` is generated
from this model (``python .claude/hooks/handoff.py --export``) and
``scripts/validate_agentic_config.py`` fails when the two drift apart.

File location: ``.claude/handoffs/<task_id>.<agent>.json`` (the agent's own
checkout, i.e. its worktree for the implementer).

Common fields are required for every agent. Three fields are role-specific and
enforced by a validator: ``tasks`` (planner), ``verdict`` (reviewer) and
``metrics`` (evaluator). ``tests`` is required from the agents that run tests.
"""

from __future__ import annotations

import argparse
import json
import sys
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "handoff.schema.json"

TASK_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,63}$"


class Agent(StrEnum):
    planner = "planner"
    implementer = "implementer"
    reviewer = "reviewer"
    evaluator = "evaluator"
    security_auditor = "security-auditor"
    docs_writer = "docs-writer"


AGENT_NAMES = [agent.value for agent in Agent]


class Status(StrEnum):
    done = "done"
    blocked = "blocked"
    needs_review = "needs_review"


class Verdict(StrEnum):
    approve = "approve"
    changes_requested = "changes_requested"


class NextAgent(StrEnum):
    planner = "planner"
    implementer = "implementer"
    reviewer = "reviewer"
    evaluator = "evaluator"
    security_auditor = "security-auditor"
    docs_writer = "docs-writer"
    human = "human"
    none = "none"


class TestRun(BaseModel):
    """The test command the agent ran and its outcome."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, description="Exact command that was run.")
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)


class PlannedTask(BaseModel):
    """One independent task produced by the planner."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=TASK_ID_PATTERN)
    title: str = Field(min_length=3)
    description: str = Field(min_length=10)
    acceptance_criteria: list[str] = Field(min_length=1)
    files_hint: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class Handoff(BaseModel):
    """Structured relay between agents of the /feature workflow."""

    model_config = ConfigDict(extra="forbid", title="Handoff")

    task_id: str = Field(pattern=TASK_ID_PATTERN, description="Task or feature identifier.")
    agent: Agent
    status: Status
    summary: str = Field(min_length=10, max_length=4000)
    files_changed: list[str] = Field(default_factory=list)
    tests: TestRun | None = None
    decisions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    next_agent: NextAgent

    tasks: list[PlannedTask] | None = Field(default=None, description="planner only")
    verdict: Verdict | None = Field(default=None, description="reviewer only")
    metrics: dict[str, float] | None = Field(default=None, description="evaluator only")

    @model_validator(mode="after")
    def _role_fields(self) -> Handoff:
        role = self.agent
        if role is Agent.planner and self.status is Status.done and not self.tasks:
            raise ValueError("planner handoff with status 'done' must list its tasks")
        if self.tasks is not None and role is not Agent.planner:
            raise ValueError("'tasks' is reserved for the planner")
        if role is Agent.reviewer and self.status is not Status.blocked and self.verdict is None:
            raise ValueError("reviewer handoff must carry a verdict")
        if self.verdict is not None and role is not Agent.reviewer:
            raise ValueError("'verdict' is reserved for the reviewer")
        if role in {Agent.implementer, Agent.reviewer} and self.status is not Status.blocked:
            if self.tests is None:
                raise ValueError(f"{role.value} handoff must report the tests it ran")
        if role is Agent.evaluator and self.status is not Status.blocked and not self.metrics:
            raise ValueError("evaluator handoff must report its metrics")
        if self.metrics is not None and role is not Agent.evaluator:
            raise ValueError("'metrics' is reserved for the evaluator")
        if (
            role is Agent.implementer
            and self.tests
            and self.tests.failed
            and self.status is Status.done
        ):
            raise ValueError("implementer cannot report 'done' with failing tests")
        return self


def validate_file(path: Path) -> Handoff:
    """Parse and validate a handoff file; the file name must match its content."""
    data = json.loads(path.read_text(encoding="utf-8"))
    handoff = Handoff.model_validate(data)
    expected = f"{handoff.task_id}.{handoff.agent.value}.json"
    if path.name != expected:
        raise ValueError(f"file is named {path.name!r} but its content says {expected!r}")
    return handoff


def json_schema() -> dict:
    schema = Handoff.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "handoff.schema.json"
    return schema


def format_errors(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "\n".join(
            f"- {'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}"
            for item in error.errors()
        )
    return f"- {error}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Handoff schema tools.")
    parser.add_argument("--export", action="store_true", help="write the JSON Schema file")
    parser.add_argument("files", nargs="*", type=Path, help="handoff files to validate")
    args = parser.parse_args(argv)

    if args.export:
        SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCHEMA_PATH.write_text(json.dumps(json_schema(), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {SCHEMA_PATH}")

    status = 0
    for path in args.files:
        try:
            validate_file(path)
            print(f"OK   {path}")
        except (ValidationError, ValueError, OSError) as error:
            print(f"FAIL {path}\n{format_errors(error)}")
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
