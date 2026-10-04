# PLAN

## Part A - Agentic development system

### A.0 Formats verified against the official Claude Code documentation

Checked on 2026-10-04 against Claude Code 2.1.288 and code.claude.com/docs
(sub-agents, hooks, skills, settings/permissions, worktrees, headless).

| Topic | Verified fact | Consequence here |
| --- | --- | --- |
| Subagents | `.claude/agents/*.md`, YAML frontmatter: `name`, `description` (drives delegation), `tools` (comma list), `model` (`opus`/`sonnet`/`haiku`/full id/`inherit`), `isolation: worktree`, `skills` (preloaded), `hooks` (agent-scoped), `color`, `maxTurns` | One file per agent, explicit tool lists, implementer uses `isolation: worktree` |
| Subagent hooks | Settings hooks fire inside subagents; tool events carry `agent_id`/`agent_type`. Frontmatter `hooks` of project agents run only after the workspace trust dialog is accepted, never in `claude -p` | Role guard wired in settings.json, role read from `agent_type` |
| Worktree isolation | Worktree under `.claude/worktrees/<name>/` on branch `worktree-<name>`; base is the remote default branch unless `worktree.baseRef: "head"`; edits/commands targeting the main checkout are blocked; kept on disk when it has commits | `baseRef: head`; implementers write their handoff inside their worktree |
| Hook events | `SessionStart` (matchers startup/resume/clear/compact), `PreToolUse`, `PostToolUse`, `SubagentStart`, `SubagentStop` (input: `agent_id`, `agent_type`, `agent_transcript_path`, `stop_hook_active`, `last_assistant_message`, `cwd`) | Five events wired |
| Exit codes | 0 proceed (stdout JSON parsed); 2 = PreToolUse blocks the call, PostToolUse shows stderr to Claude, SubagentStop prevents the subagent from stopping; other = non-blocking error | Guards and validators exit 2 with the reason on stderr |
| Hook config | `hooks.<Event>[] = {matcher, hooks: [{type: "command", command, timeout}]}`; `$CLAUDE_PROJECT_DIR` stays at the project root inside worktrees, `cwd` follows the agent; settings changes picked up by a file watcher | Commands use `$CLAUDE_PROJECT_DIR`; hooks read `cwd` |
| Skills | `.claude/skills/<name>/SKILL.md`, frontmatter `description`, `argument-hint`, `disable-model-invocation`; `$ARGUMENTS`; `/name` works in `-p` mode; `.claude/commands/` still works but skills are recommended | /feature, /eval, /release-check are skills with `disable-model-invocation: true` |
| Permissions | `permissions.allow/deny`, `Tool(specifier)`, `Bash(cmd *)` prefix wildcard, `Read(**/.env)` gitignore-style paths; deny cannot carve exceptions | Deny `.env` reads + destructive commands; `.env.*` except `.env.example` handled by a hook |
| Loading | New `.claude/agents` directory needs a session restart; headless `claude -p` loads project agents, skills and hooks | The demo runs in a fresh headless session |

### A.1 Structure created

- [x] `CLAUDE.md`: overview, architecture map, commands, conventions, definition of done, critical rules, agents
- [x] `.claude/settings.json`: minimal allow list, deny `.env` reads and destructive commands, `worktree.baseRef: head`, hook wiring
- [x] `.claude/agents/`: planner, implementer, reviewer, evaluator, security-auditor, docs-writer
- [x] `.claude/skills/`: eval-protocol, decision-record, security-checklist (procedures); feature, eval, release-check (entry points)
- [x] `.claude/hooks/`: guard_bash, guard_write, guard_read, guard_role, secret_scan, lint_python, record_subagent_start, validate_handoff, session_context, run_hook (launcher), handoff (schema model)
- [x] `.claude/schemas/handoff.schema.json` exported from the Pydantic model
- [x] `evaluation/`: layout suite + committed baseline + comparison (gives /eval and the evaluator real work before the translation suite exists)
- [x] `scripts/validate_agentic_config.py`, `scripts/run_tests.py`, `scripts/install_git_hooks.py`, `Makefile`
- [x] `tests/test_hooks.py` (24 tests, every hook), `tests/test_agentic_config.py` (validator mutation tests)
- [x] CI: lint, validate, secret scan, tests, eval
- [x] `docs/agentic-development.md`
- [x] Demonstration: `/feature` adds `--version` to the CLI, end to end, in a headless session (merge `4f72661`; lessons in docs/agentic-development.md)
