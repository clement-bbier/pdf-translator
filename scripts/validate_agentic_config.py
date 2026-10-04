"""Validate the agentic development configuration under .claude/.

Checks that the agents, skills, hooks, handoff schema, settings and
.gitignore are consistent with each other, so a broken or decorative piece is
caught in CI rather than at the first /feature run.

    python scripts/validate_agentic_config.py   # exit 1 on any error
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_DIR = ROOT / ".claude"
sys.path.insert(0, str(CLAUDE_DIR / "hooks"))

from handoff import AGENT_NAMES, SCHEMA_PATH, json_schema  # noqa: E402

KNOWN_TOOLS = {
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "Grep",
    "Glob",
    "Bash",
    "PowerShell",
    "WebFetch",
    "WebSearch",
    "NotebookEdit",
    "Agent",
    "Skill",
    "TodoWrite",
}
MODEL_ALIASES = {"opus", "sonnet", "haiku", "fable", "inherit"}
READ_ONLY_ROLES = {"planner", "reviewer", "evaluator", "security-auditor"}
WRITE_TOOLS = {"Edit", "MultiEdit", "NotebookEdit"}
WORKFLOW_SKILLS = {"feature", "eval", "release-check"}
PROCEDURE_SKILLS = {"eval-protocol", "decision-record", "security-checklist"}
REQUIRED_EVENTS = {"SessionStart", "PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop"}
GITIGNORE_ENTRIES = {
    ".claude/handoffs/",
    ".claude/settings.local.json",
    "*.local.txt",
    ".claude/worktrees/",
}
# Hook modules that are libraries or launchers, not hooks themselves.
HOOK_LIBRARIES = {"_common", "handoff", "run_hook"}

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_SCRIPT_RE = re.compile(r"\.claude/hooks/(\w+)\.py(?:\"?\s+(\w+))?")


def frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("missing YAML frontmatter")
    data = yaml.safe_load(match.group(1)) or {}
    if not isinstance(data, dict):
        raise ValueError("frontmatter is not a mapping")
    return data, text[match.end() :]


def _tool_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _hook_scripts(commands: list[str]) -> set[str]:
    """Hook module names referenced by commands (run_hook.py <name> counts as <name>)."""
    names: set[str] = set()
    for command in commands:
        for script, arg in _SCRIPT_RE.findall(command):
            names.add(arg if script == "run_hook" and arg else script)
    return names


def _commands(hooks_config: dict) -> list[str]:
    return [
        hook.get("command", "")
        for groups in hooks_config.values()
        for group in groups
        for hook in group.get("hooks", [])
    ]


def check_agents(errors: list[str]) -> tuple[set[str], set[str]]:
    """Validate agent frontmatter; return (skills referenced, hook scripts referenced)."""
    agents_dir = CLAUDE_DIR / "agents"
    found: set[str] = set()
    skills: set[str] = set()
    scripts: set[str] = set()

    for path in sorted(agents_dir.glob("*.md")):
        where = f"agents/{path.name}"
        try:
            meta, body = frontmatter(path)
        except (ValueError, yaml.YAMLError) as error:
            errors.append(f"{where}: {error}")
            continue

        name = meta.get("name")
        found.add(str(name))
        if name != path.stem:
            errors.append(f"{where}: name {name!r} must match the file name")
        if name not in AGENT_NAMES:
            errors.append(f"{where}: {name!r} is not an agent of the handoff schema")
        description = str(meta.get("description") or "")
        if len(description) < 60:
            errors.append(f"{where}: description too short for reliable delegation")

        tools = _tool_list(meta.get("tools"))
        if not tools:
            errors.append(f"{where}: tools must be listed explicitly (least privilege)")
        for tool in tools:
            if tool not in KNOWN_TOOLS:
                errors.append(f"{where}: unknown tool {tool!r}")

        model = str(meta.get("model") or "")
        if model not in MODEL_ALIASES and not model.startswith("claude-"):
            errors.append(f"{where}: model {model!r} is not an alias or a claude-* id")

        hooks = meta.get("hooks") or {}
        commands = _commands(hooks) if isinstance(hooks, dict) else []
        scripts |= _hook_scripts(commands)
        guard_args = [
            arg
            for command in commands
            for arg in re.findall(r"guard_role\.py\"?\s+([\w-]+)", command)
        ]

        if name in READ_ONLY_ROLES:
            if WRITE_TOOLS & set(tools):
                errors.append(f"{where}: read-only role must not have {WRITE_TOOLS & set(tools)}")
            if name not in guard_args:
                errors.append(f"{where}: read-only role needs the guard_role.py {name} hook")
        if name == "docs-writer" and "docs-writer" not in guard_args:
            errors.append(f"{where}: docs-writer needs the guard_role.py docs-writer hook")
        if name == "implementer" and meta.get("isolation") != "worktree":
            errors.append(f"{where}: implementer must run with isolation: worktree")
        if meta.get("isolation") not in (None, "worktree"):
            errors.append(f"{where}: unsupported isolation {meta.get('isolation')!r}")
        if f"{name}.json" not in body and ".json" not in body:
            errors.append(f"{where}: body must tell the agent which handoff file to write")

        for skill in meta.get("skills") or []:
            skills.add(str(skill))

    missing = set(AGENT_NAMES) - found
    if missing:
        errors.append(f"agents: missing definitions for {sorted(missing)}")
    return skills, scripts


def check_skills(errors: list[str], referenced: set[str]) -> None:
    skills_dir = CLAUDE_DIR / "skills"
    present: set[str] = set()
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        skill = path.parent.name
        present.add(skill)
        where = f"skills/{skill}/SKILL.md"
        try:
            meta, body = frontmatter(path)
        except (ValueError, yaml.YAMLError) as error:
            errors.append(f"{where}: {error}")
            continue
        if meta.get("name", skill) != skill:
            errors.append(f"{where}: name must match its directory")
        if len(str(meta.get("description") or "")) < 40:
            errors.append(f"{where}: description too short")
        if skill in WORKFLOW_SKILLS and meta.get("disable-model-invocation") is not True:
            errors.append(f"{where}: workflow entry points must set disable-model-invocation: true")
        if not body.strip():
            errors.append(f"{where}: empty body")

    for skill in sorted((WORKFLOW_SKILLS | PROCEDURE_SKILLS | referenced) - present):
        errors.append(f"skills: {skill!r} is required or referenced but missing")
    for skill in sorted(PROCEDURE_SKILLS - referenced):
        errors.append(f"skills: procedure {skill!r} is not preloaded by any agent")


def check_schema(errors: list[str]) -> None:
    if not SCHEMA_PATH.is_file():
        errors.append(
            "schemas/handoff.schema.json is missing (python .claude/hooks/handoff.py --export)"
        )
        return
    committed = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if committed != json_schema():
        errors.append("schemas/handoff.schema.json is out of date with handoff.py (re-export it)")


def check_settings(errors: list[str], agent_scripts: set[str]) -> None:
    path = CLAUDE_DIR / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        errors.append(f"settings.json: {error}")
        return

    hooks = settings.get("hooks") or {}
    for event in sorted(REQUIRED_EVENTS - set(hooks)):
        errors.append(f"settings.json: no hook for {event}")

    for event in ("SubagentStart", "SubagentStop"):
        matchers = " ".join(group.get("matcher", "") for group in hooks.get(event, []))
        for agent in AGENT_NAMES:
            if not re.search(rf"(?:^|\|){re.escape(agent)}(?:$|\||\s)", matchers):
                errors.append(f"settings.json: {event} matcher does not cover {agent}")

    settings_scripts = _hook_scripts(_commands(hooks))
    hooks_dir = CLAUDE_DIR / "hooks"
    for script in sorted(settings_scripts | agent_scripts):
        if not (hooks_dir / f"{script}.py").is_file():
            errors.append(f"hook script .claude/hooks/{script}.py is referenced but missing")
    on_disk = {path.stem for path in hooks_dir.glob("*.py")} - HOOK_LIBRARIES
    for script in sorted(on_disk - settings_scripts - agent_scripts - {"secret_scan"}):
        errors.append(f"hook script {script}.py is not wired to any event (decorative)")
    if "secret_scan" not in settings_scripts:
        errors.append("settings.json: secret_scan.py must run before commits (PreToolUse Bash)")

    deny = " ".join(settings.get("permissions", {}).get("deny", []))
    if "Read(**/.env)" not in deny:
        errors.append("settings.json: permissions.deny must refuse reading .env")
    if "rm -rf" not in deny or "push --force" not in deny:
        errors.append("settings.json: permissions.deny must refuse rm -rf and force pushes")
    if (settings.get("worktree") or {}).get("baseRef") != "head":
        errors.append(
            "settings.json: worktree.baseRef must be 'head' (implementers build on the feature branch)"
        )


def check_hook_tests(errors: list[str]) -> None:
    tests = ROOT / "tests" / "test_hooks.py"
    text = tests.read_text(encoding="utf-8") if tests.is_file() else ""
    for path in sorted((CLAUDE_DIR / "hooks").glob("*.py")):
        if path.stem in HOOK_LIBRARIES - {"run_hook", "handoff"}:
            continue
        if f"{path.stem}.py" not in text and f'"{path.stem}"' not in text:
            errors.append(f"tests/test_hooks.py does not exercise {path.name}")


def check_gitignore(errors: list[str]) -> None:
    lines = {
        line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    for entry in sorted(GITIGNORE_ENTRIES - lines):
        errors.append(f".gitignore: missing {entry!r}")
    for tracked in (".claude/agents", ".claude/skills", ".claude/hooks", ".claude/schemas"):
        if any(line.rstrip("/") in {tracked, ".claude", ".claude/*"} for line in lines):
            errors.append(f".gitignore: {tracked} must stay versioned")


def check_claude_md(errors: list[str]) -> None:
    path = ROOT / "CLAUDE.md"
    if not path.is_file():
        errors.append("CLAUDE.md is missing")
        return
    text = path.read_text(encoding="utf-8")
    for agent in AGENT_NAMES:
        if f"`{agent}`" not in text:
            errors.append(f"CLAUDE.md does not list the {agent} agent")
    for command in ("/feature", "/eval", "/release-check"):
        if command not in text:
            errors.append(f"CLAUDE.md does not mention {command}")


def main() -> int:
    errors: list[str] = []
    skills, agent_scripts = check_agents(errors)
    check_skills(errors, skills)
    check_schema(errors)
    check_settings(errors, agent_scripts)
    check_hook_tests(errors)
    check_gitignore(errors)
    check_claude_md(errors)

    if errors:
        print(f"agentic config: {len(errors)} error(s)")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("agentic config: OK (agents, skills, schema, settings, hooks, gitignore, CLAUDE.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
