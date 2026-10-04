"""Tests for the Claude Code hooks in .claude/hooks/.

Each hook is run as a subprocess, exactly as Claude Code runs it: a JSON event
on stdin, then the exit code is checked (0 = allow, 2 = block). No network.
Run: python tests/test_hooks.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / ".claude" / "hooks"

FAILURES: list[str] = []

ALLOW, BLOCK = 0, 2


def check(condition: bool, message: str) -> None:
    """Record a failure if the condition does not hold."""
    if not condition:
        FAILURES.append(message)


def run_hook(
    script: str,
    event: dict,
    *,
    args: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a hook script with ``event`` as stdin JSON."""
    # The human override must only be active when a test sets it explicitly.
    full_env = {k: v for k, v in os.environ.items() if k != "AGENTIC_ALLOW_REFERENCE_EDIT"}
    full_env.update({"PYTHONIOENCODING": "utf-8", **(env or {})})
    return subprocess.run(
        [sys.executable, str(HOOKS / script), *args],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=full_env,
        cwd=cwd or ROOT,
        timeout=60,
    )


def bash_event(command: str, cwd: Path = ROOT) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd),
    }


def write_event(path: str, tool: str = "Write") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"file_path": path, "content": "x"},
        "cwd": str(ROOT),
    }


def git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def make_repo(tmp: Path, branch: str = "main") -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    git(["init", "-q", "-b", branch], repo)
    git(["config", "user.email", "test@example.invalid"], repo)
    git(["config", "user.name", "test"], repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    git(["add", "README.md"], repo)
    git(["commit", "-q", "-m", "init"], repo)
    return repo


# --------------------------------------------------------------------------- #
# guard_bash.py
# --------------------------------------------------------------------------- #


def test_guard_bash_blocks_destructive_commands() -> None:
    """rm -rf in all spellings, force pushes and git clean -f are blocked."""
    for command in (
        "rm -rf build",
        "rm -fr /",
        "rm -r -f output",
        "rm --recursive --force tmp",
        "make test && rm -rf .git",
        "sudo rm -Rf /var",
        "git push --force origin main",
        "git push -f",
        "git push origin +feature",
        "git push --force-with-lease",
        "git -C repo push --force",
        "git clean -fdx",
        "Remove-Item -Recurse -Force build",
    ):
        result = run_hook("guard_bash.py", bash_event(command))
        check(result.returncode == BLOCK, f"guard_bash.py should block {command!r}")
        check("guard_bash.py" in result.stderr, f"no reason given for {command!r}")


def test_guard_bash_allows_safe_commands() -> None:
    """Ordinary commands, including ones that look alike, pass."""
    for command in (
        "rm build/output.pdf",
        "rm -r tmp/job",
        "git push origin feature/x",
        "git status",
        "make test",
        "ls -la .venv",
        "cat .env.example",
        "python -c 'from dotenv import load_dotenv'",
        "git commit -m 'remove -rf mention'",
    ):
        result = run_hook("guard_bash.py", bash_event(command))
        check(
            result.returncode == ALLOW, f"guard_bash.py should allow {command!r}: {result.stderr}"
        )


def test_guard_bash_blocks_env_reads() -> None:
    """.env files other than the template cannot be read from the shell."""
    for command in (
        "cat .env",
        "type .env",
        "source .env && env",
        "grep KEY ./.env.local",
        "cp .env /tmp/x",
        "Get-Content .env.production",
        "python -c \"print(open('.env').read())\"",
    ):
        result = run_hook("guard_bash.py", bash_event(command))
        check(result.returncode == BLOCK, f"guard_bash.py should block env read {command!r}")


def test_guard_bash_ignores_commit_message_text() -> None:
    """Commit messages may mention .env or rm -rf; code in other heredocs may not."""
    allowed = (
        "git commit -m 'block .env access and rm -rf'",
        "git commit -q -F - <<'EOF'\nfeat: guard .env reads\n\nNo rm -rf here.\nEOF",
        'git add a.py && git commit -m "never read .env.local"',
    )
    for command in allowed:
        result = run_hook("guard_bash.py", bash_event(command))
        check(result.returncode == ALLOW, f"commit message should not trigger: {command!r}")
    blocked = (
        "python - <<'EOF'\nprint(open('.env').read())\nEOF",
        "git commit -m 'msg' && cat .env",
        "git commit -m ok && rm -rf build",
    )
    for command in blocked:
        result = run_hook("guard_bash.py", bash_event(command))
        check(result.returncode == BLOCK, f"should still block: {command!r}")


def test_guard_bash_reset_hard_only_blocked_on_main() -> None:
    """git reset --hard is blocked on main (or targeting main), allowed elsewhere."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(Path(tmp))
        on_main = run_hook("guard_bash.py", bash_event("git reset --hard HEAD~1", repo))
        check(on_main.returncode == BLOCK, "reset --hard on main should be blocked")

        git(["switch", "-q", "-c", "feature"], repo)
        on_feature = run_hook("guard_bash.py", bash_event("git reset --hard HEAD", repo))
        check(on_feature.returncode == ALLOW, "reset --hard on a feature branch should pass")
        to_main = run_hook("guard_bash.py", bash_event("git reset --hard origin/main", repo))
        check(to_main.returncode == BLOCK, "reset --hard origin/main should be blocked")


def test_guard_bash_protects_eval_references() -> None:
    """Shell writes to the eval references and make eval-baseline need the human override."""
    for command in (
        "echo x > evaluation/dataset/zh_en.jsonl",
        "sed -i s/a/b/ evaluation/baseline.json",
        "make eval-baseline",
    ):
        blocked = run_hook("guard_bash.py", bash_event(command))
        check(blocked.returncode == BLOCK, f"should block {command!r}")
        allowed = run_hook(
            "guard_bash.py", bash_event(command), env={"AGENTIC_ALLOW_REFERENCE_EDIT": "1"}
        )
        check(allowed.returncode == ALLOW, f"override should allow {command!r}")
    read_only = run_hook("guard_bash.py", bash_event("cat evaluation/baseline.json"))
    check(read_only.returncode == ALLOW, "reading the baseline must stay allowed")


# --------------------------------------------------------------------------- #
# guard_write.py / guard_read.py
# --------------------------------------------------------------------------- #


def test_guard_write_env_files() -> None:
    """.env* writes are blocked except for .env.example."""
    for path in (".env", "config/.env.local", str(ROOT / ".env.production")):
        result = run_hook("guard_write.py", write_event(path))
        check(result.returncode == BLOCK, f"guard_write.py should block {path}")
    for path in (".env.example", "app/config.py", "docs/env.md"):
        result = run_hook("guard_write.py", write_event(path))
        check(result.returncode == ALLOW, f"guard_write.py should allow {path}")


def test_guard_write_eval_references() -> None:
    """Existing references are immutable; new ones may be created; override works."""
    existing = str(ROOT / "evaluation" / "baseline.json")
    check(Path(existing).is_file(), "evaluation/baseline.json should exist for this test")
    edited = run_hook("guard_write.py", write_event(existing, tool="Edit"))
    check(edited.returncode == BLOCK, "editing the existing baseline should be blocked")

    new_ref = str(ROOT / "evaluation" / "dataset" / "does-not-exist-yet.jsonl")
    created = run_hook("guard_write.py", write_event(new_ref))
    check(created.returncode == ALLOW, "creating a new reference file should be allowed")

    override = run_hook(
        "guard_write.py",
        write_event(existing, tool="Edit"),
        env={"AGENTIC_ALLOW_REFERENCE_EDIT": "1"},
    )
    check(override.returncode == ALLOW, "human override should allow editing the baseline")

    worktree_path = str(ROOT / ".claude" / "worktrees" / "wt-1" / "evaluation" / "baseline.json")
    with tempfile.TemporaryDirectory() as tmp:
        # Same rule inside a worktree: create a fake worktree copy that exists.
        fake = Path(tmp) / ".claude" / "worktrees" / "wt-1" / "evaluation"
        fake.mkdir(parents=True)
        (fake / "baseline.json").write_text("{}", encoding="utf-8")
        event = write_event(str(fake / "baseline.json"), tool="Edit")
        event["cwd"] = tmp
        result = run_hook("guard_write.py", event, env={"CLAUDE_PROJECT_DIR": tmp})
        check(
            result.returncode == BLOCK,
            f"worktree reference edit should be blocked ({worktree_path})",
        )


def test_guard_read_env_files() -> None:
    """The Read tool cannot open .env files; the template stays readable."""
    blocked = run_hook(
        "guard_read.py", {"tool_name": "Read", "tool_input": {"file_path": str(ROOT / ".env")}}
    )
    check(blocked.returncode == BLOCK, "guard_read.py should block .env")
    grep = run_hook("guard_read.py", {"tool_name": "Grep", "tool_input": {"path": ".env.local"}})
    check(grep.returncode == BLOCK, "guard_read.py should block grep in .env.local")
    allowed = run_hook(
        "guard_read.py", {"tool_name": "Read", "tool_input": {"file_path": ".env.example"}}
    )
    check(allowed.returncode == ALLOW, "guard_read.py should allow .env.example")


# --------------------------------------------------------------------------- #
# secret_scan.py
# --------------------------------------------------------------------------- #


def _staged_repo(tmp: Path, content: str, terms: str | None = None) -> Path:
    repo = make_repo(tmp)
    (repo / "module.py").write_text(content, encoding="utf-8")
    git(["add", "module.py"], repo)
    if terms is not None:
        (repo / ".claude").mkdir()
        (repo / ".claude" / "forbidden-terms.local.txt").write_text(terms, encoding="utf-8")
    return repo


def test_secret_scan_blocks_commit_with_secret() -> None:
    """A git commit adding a credential is blocked; the reason names file and line."""
    fake_key = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"
    with tempfile.TemporaryDirectory() as tmp:
        repo = _staged_repo(Path(tmp), f'API = "{fake_key}"\n')
        result = run_hook(
            "secret_scan.py",
            bash_event("git commit -m 'add module'", repo),
            env={"CLAUDE_PROJECT_DIR": str(repo)},
        )
        check(result.returncode == BLOCK, "commit with an API key should be blocked")
        check("module.py:1" in result.stderr, f"finding should cite module.py:1: {result.stderr}")
        check(fake_key not in result.stderr, "the secret itself must not be echoed")


def test_secret_scan_blocks_forbidden_terms_without_echoing_them() -> None:
    """Terms from the local list block the commit and are reported by number only."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _staged_repo(
            Path(tmp), "# built for Contoso Holdings\n", terms="# local list\ncontoso holdings\n"
        )
        result = run_hook(
            "secret_scan.py",
            bash_event("git commit -m wip", repo),
            env={"CLAUDE_PROJECT_DIR": str(repo)},
        )
        check(result.returncode == BLOCK, "forbidden term should block the commit")
        check(
            "forbidden term #1" in result.stderr,
            f"term should be reported by number: {result.stderr}",
        )
        check("contoso" not in result.stderr.lower(), "the forbidden term must not be echoed")


def test_secret_scan_allows_clean_commit_and_other_commands() -> None:
    """Clean commits and non-commit commands pass."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _staged_repo(Path(tmp), "VALUE = 42\n", terms="contoso holdings\n")
        env = {"CLAUDE_PROJECT_DIR": str(repo)}
        clean = run_hook("secret_scan.py", bash_event("git commit -m ok", repo), env=env)
        check(clean.returncode == ALLOW, f"clean commit should pass: {clean.stderr}")
        other = run_hook("secret_scan.py", bash_event("git status", repo), env=env)
        check(other.returncode == ALLOW, "non-commit command should pass")


def test_secret_scan_add_then_commit_in_one_command() -> None:
    """`git add x && git commit` is judged on the working tree, not the stale index."""
    fake_key = "sk-ant-" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6"
    with tempfile.TemporaryDirectory() as tmp:
        repo = _staged_repo(Path(tmp), "VALUE = 1\n")
        env = {"CLAUDE_PROJECT_DIR": str(repo)}
        (repo / "new_module.py").write_text(f'KEY = "{fake_key}"\n', encoding="utf-8")
        result = run_hook(
            "secret_scan.py", bash_event("git add new_module.py && git commit -m x", repo), env=env
        )
        check(result.returncode == BLOCK, "secret in a file staged by the same command must block")

        # The reverse: the index holds a secret already fixed in the working tree.
        (repo / "new_module.py").unlink()
        (repo / "module.py").write_text(f'KEY = "{fake_key}"\n', encoding="utf-8")
        git(["add", "module.py"], repo)
        (repo / "module.py").write_text("KEY = None\n", encoding="utf-8")
        fixed = run_hook(
            "secret_scan.py", bash_event("git add module.py && git commit -m fix", repo), env=env
        )
        check(fixed.returncode == ALLOW, f"fixed working tree should pass: {fixed.stderr}")


def test_secret_scan_cli_modes() -> None:
    """--staged and --all exit 1 on a finding, 0 when clean; the pragma skips a line."""
    # Built at run time so this file itself contains no credential literal.
    assignment = 'password = "' + "hunter2" * 3 + '"'
    with tempfile.TemporaryDirectory() as tmp:
        repo = _staged_repo(Path(tmp), assignment + "\n")
        staged = subprocess.run(
            [sys.executable, str(HOOKS / "secret_scan.py"), "--staged", "--root", str(repo)],
            capture_output=True,
            text=True,
        )
        check(staged.returncode == 1, "--staged should report the staged password")

        (repo / "module.py").write_text(assignment + "  # secret-scan: allow\n", encoding="utf-8")
        git(["add", "module.py"], repo)
        git(["commit", "-q", "-m", "fixture"], repo)
        full = subprocess.run(
            [sys.executable, str(HOOKS / "secret_scan.py"), "--all", "--root", str(repo)],
            capture_output=True,
            text=True,
        )
        check(full.returncode == 0, f"--all should honour the pragma: {full.stderr}")


# --------------------------------------------------------------------------- #
# lint_python.py (through run_hook.py, as configured)
# --------------------------------------------------------------------------- #


def test_lint_python_formats_and_reports() -> None:
    """Badly formatted code is reformatted; an unfixable error is sent back (exit 2)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
        fixable = root / "fixable.py"
        fixable.write_text("x=[1,2 ,3]\n", encoding="utf-8")
        event = {"tool_name": "Write", "tool_input": {"file_path": str(fixable)}, "cwd": tmp}
        result = subprocess.run(
            [sys.executable, str(HOOKS / "run_hook.py"), "lint_python"],
            input=json.dumps(event),
            capture_output=True,
            text=True,
        )
        check(result.returncode == ALLOW, f"fixable file should pass: {result.stderr}")
        check(fixable.read_text(encoding="utf-8") == "x = [1, 2, 3]\n", "file should be formatted")

        broken = root / "broken.py"
        broken.write_text("def f():\n    return undefined_name\n", encoding="utf-8")
        event["tool_input"]["file_path"] = str(broken)
        result = subprocess.run(
            [sys.executable, str(HOOKS / "run_hook.py"), "lint_python"],
            input=json.dumps(event),
            capture_output=True,
            text=True,
        )
        check(result.returncode == BLOCK, "undefined name should be reported (exit 2)")
        check("F821" in result.stderr, f"ruff code should be in the message: {result.stderr}")

        other = subprocess.run(
            [sys.executable, str(HOOKS / "run_hook.py"), "lint_python"],
            input=json.dumps({"tool_input": {"file_path": str(root / "notes.md")}}),
            capture_output=True,
            text=True,
        )
        check(other.returncode == ALLOW, "non-Python files are ignored")


# --------------------------------------------------------------------------- #
# record_subagent_start.py + validate_handoff.py
# --------------------------------------------------------------------------- #


def _valid_handoff(task_id: str = "demo-t1", agent: str = "implementer") -> dict:
    return {
        "task_id": task_id,
        "agent": agent,
        "status": "done",
        "summary": "Implemented the demo task with tests.",
        "files_changed": ["app/x.py"],
        "tests": {"command": "make test", "passed": 10, "failed": 0},
        "decisions": [],
        "risks": [],
        "next_agent": "reviewer",
    }


def _stop(
    project: Path, agent: str = "implementer", agent_id: str = "agent-1"
) -> subprocess.CompletedProcess[str]:
    event = {
        "hook_event_name": "SubagentStop",
        "agent_type": agent,
        "agent_id": agent_id,
        "cwd": str(project),
        "stop_hook_active": False,
    }
    return subprocess.run(
        [sys.executable, str(HOOKS / "run_hook.py"), "validate_handoff"],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(project)},
    )


def _start(project: Path, agent: str = "implementer", agent_id: str = "agent-1") -> None:
    run_hook(
        "record_subagent_start.py",
        {"hook_event_name": "SubagentStart", "agent_type": agent, "agent_id": agent_id},
        env={"CLAUDE_PROJECT_DIR": str(project)},
    )


def test_validate_handoff_requires_a_fresh_valid_file() -> None:
    """Missing -> blocked; invalid -> blocked with the error; valid -> allowed."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        handoffs = project / ".claude" / "handoffs"
        handoffs.mkdir(parents=True)

        # A stale handoff from an earlier run must not count.
        stale = handoffs / "old-t1.implementer.json"
        stale.write_text(json.dumps(_valid_handoff("old-t1")), encoding="utf-8")
        past = time.time() - 3600
        os.utime(stale, (past, past))
        _start(project)
        check((project / ".claude/handoffs/.runs/agent-1.json").is_file(), "start not recorded")

        missing = _stop(project)
        check(missing.returncode == BLOCK, "stop without a fresh handoff should be blocked")
        check("No handoff found" in missing.stderr, f"unexpected message: {missing.stderr}")

        bad = _valid_handoff()
        bad["status"] = "finished"
        del bad["tests"]
        (handoffs / "demo-t1.implementer.json").write_text(json.dumps(bad), encoding="utf-8")
        invalid = _stop(project)
        check(invalid.returncode == BLOCK, "invalid handoff should be blocked")
        check("status" in invalid.stderr, f"error should name the bad field: {invalid.stderr}")

        (handoffs / "demo-t1.implementer.json").write_text(
            json.dumps(_valid_handoff()), encoding="utf-8"
        )
        valid = _stop(project)
        check(valid.returncode == ALLOW, f"valid handoff should allow the stop: {valid.stderr}")


def test_validate_handoff_role_rules_and_naming() -> None:
    """Role-specific fields are enforced and the file name must match the content."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        handoffs = project / ".claude" / "handoffs"
        handoffs.mkdir(parents=True)
        _start(project, "reviewer", "rev-1")

        review = _valid_handoff("demo-review-r1", "reviewer")
        review["next_agent"] = "evaluator"
        (handoffs / "demo-review-r1.reviewer.json").write_text(json.dumps(review), encoding="utf-8")
        no_verdict = _stop(project, "reviewer", "rev-1")
        check(no_verdict.returncode == BLOCK, "reviewer without a verdict should be blocked")

        review["verdict"] = "approve"
        (handoffs / "demo-review-r1.reviewer.json").write_text(json.dumps(review), encoding="utf-8")
        check(
            _stop(project, "reviewer", "rev-1").returncode == ALLOW, "reviewer with verdict passes"
        )

        _start(project, "reviewer", "rev-2")
        (handoffs / "wrong-name.reviewer.json").write_text(json.dumps(review), encoding="utf-8")
        misnamed = _stop(project, "reviewer", "rev-2")
        check(misnamed.returncode == BLOCK, "file name not matching task_id should be blocked")


def test_validate_handoff_ignores_other_agents_and_gives_up_eventually() -> None:
    """Built-in agents are not checked; after MAX_ATTEMPTS the agent is released."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        explore = _stop(project, "Explore", "x-1")
        check(explore.returncode == ALLOW, "non-workflow agents are not validated")

        _start(project, "docs-writer", "doc-1")
        codes = [_stop(project, "docs-writer", "doc-1").returncode for _ in range(5)]
        check(codes[:4] == [BLOCK] * 4, f"first attempts should block: {codes}")
        check(codes[4] == ALLOW, "the agent is released after MAX_ATTEMPTS")


def test_validate_handoff_prefers_the_agents_own_worktree() -> None:
    """Parallel implementers: the hook validates the file in the stopping agent's cwd."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        own = project / ".claude" / "worktrees" / "wt-a"
        other = project / ".claude" / "worktrees" / "wt-b"
        for tree in (own, other):
            (tree / ".claude" / "handoffs").mkdir(parents=True)
        _start(project, "implementer", "impl-a")
        bad = _valid_handoff("demo-t2")
        bad["tests"]["failed"] = 3  # 'done' with failing tests is invalid
        (other / ".claude/handoffs/demo-t2.implementer.json").write_text(
            json.dumps(bad), encoding="utf-8"
        )
        (own / ".claude/handoffs/demo-t1.implementer.json").write_text(
            json.dumps(_valid_handoff()), encoding="utf-8"
        )
        event = {
            "hook_event_name": "SubagentStop",
            "agent_type": "implementer",
            "agent_id": "impl-a",
            "cwd": str(own),
        }
        result = subprocess.run(
            [sys.executable, str(HOOKS / "run_hook.py"), "validate_handoff"],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(project)},
        )
        check(result.returncode == ALLOW, f"own valid handoff should win: {result.stderr}")


# --------------------------------------------------------------------------- #
# guard_role.py
# --------------------------------------------------------------------------- #


def _role(role: str, tool: str, tool_input: dict) -> int:
    """Run guard_role.py as wired in settings.json: the role comes from agent_type."""
    event = {"tool_name": tool, "tool_input": tool_input, "cwd": str(ROOT)}
    if role:
        event["agent_type"] = role
    return run_hook("guard_role.py", event).returncode


def test_guard_role_leaves_unrestricted_callers_alone() -> None:
    """Main session, implementer and built-in agents are not restricted by this hook."""
    code = {"file_path": str(ROOT / "app/main.py")}
    check(_role("", "Edit", code) == ALLOW, "main session may edit code")
    check(_role("implementer", "Edit", code) == ALLOW, "implementer may edit code")
    check(_role("general-purpose", "Bash", {"command": "make format"}) == ALLOW, "built-in agent")
    check(
        _role("implementer", "Bash", {"command": "git commit -m x"}) == ALLOW, "implementer commits"
    )


def test_guard_role_read_only_roles() -> None:
    """Read-only roles write only handoffs and run only their allowlisted commands."""
    handoff = str(ROOT / ".claude" / "handoffs" / "x.reviewer.json")
    check(_role("reviewer", "Write", {"file_path": handoff}) == ALLOW, "handoff write allowed")
    check(
        _role("reviewer", "Edit", {"file_path": str(ROOT / "app/main.py")}) == BLOCK,
        "reviewer must not edit code",
    )
    check(
        _role("planner", "Write", {"file_path": "PLAN.md"}) == BLOCK,
        "planner must not write PLAN.md",
    )

    for command in (
        "make test",
        "make lint 2>&1 | tail -20",
        "git diff abc...HEAD",
        ".venv/Scripts/python.exe scripts/run_tests.py hooks",
        "cd /tmp/wt && make test",
    ):
        check(
            _role("reviewer", "Bash", {"command": command}) == ALLOW,
            f"reviewer may run {command!r}",
        )
    for command in (
        "git commit -m x",
        "make format",
        'python -c \'open("a","w")\'',
        "make test > out.txt",
        "git push",
    ):
        check(
            _role("reviewer", "Bash", {"command": command}) == BLOCK,
            f"reviewer may not run {command!r}",
        )

    check(_role("evaluator", "Bash", {"command": "make eval"}) == ALLOW, "evaluator runs make eval")
    check(
        _role("evaluator", "Bash", {"command": "make eval-baseline"}) == BLOCK,
        "evaluator cannot update the baseline",
    )
    check(_role("security-auditor", "Bash", {"command": "make security"}) == ALLOW, "auditor scans")
    check(_role("planner", "Bash", {"command": "make test"}) == BLOCK, "planner does not run tests")


def test_guard_role_docs_writer() -> None:
    """docs-writer writes Markdown and docs/ only."""
    check(
        _role("docs-writer", "Edit", {"file_path": str(ROOT / "README.md")}) == ALLOW, "README ok"
    )
    check(
        _role("docs-writer", "Write", {"file_path": "docs/decisions/0001-x.md"}) == ALLOW, "docs ok"
    )
    check(_role("docs-writer", "Edit", {"file_path": "app/main.py"}) == BLOCK, "code is off limits")


# --------------------------------------------------------------------------- #
# session_context.py
# --------------------------------------------------------------------------- #


def test_session_context_reports_status() -> None:
    """Emits SessionStart additionalContext with branch, plan, handoffs and eval lines."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = make_repo(Path(tmp))
        (repo / "PLAN.md").write_text("- [x] one\n- [ ] two\n- [ ] three\n", encoding="utf-8")
        reports = repo / "evaluation" / "reports"
        reports.mkdir(parents=True)
        (reports / "latest.json").write_text(
            json.dumps(
                {
                    "verdict": "pass",
                    "headline": "6 metric(s), no regression",
                    "generated_at": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        result = run_hook(
            "session_context.py", {"source": "startup"}, env={"CLAUDE_PROJECT_DIR": str(repo)}
        )
        check(result.returncode == ALLOW, "session_context.py must never block")
        payload = json.loads(result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        check(payload["hookSpecificOutput"]["hookEventName"] == "SessionStart", "wrong event name")
        check("Branch: main" in context, f"branch missing: {context}")
        check("1/3 checklist items" in context, f"plan progress missing: {context}")
        check("pass" in context and "no regression" in context, f"eval missing: {context}")
        check(len(context.splitlines()) <= 6, "context must stay short")


def main() -> int:
    tests = [
        test_guard_bash_blocks_destructive_commands,
        test_guard_bash_allows_safe_commands,
        test_guard_bash_blocks_env_reads,
        test_guard_bash_ignores_commit_message_text,
        test_guard_bash_reset_hard_only_blocked_on_main,
        test_guard_bash_protects_eval_references,
        test_guard_write_env_files,
        test_guard_write_eval_references,
        test_guard_read_env_files,
        test_secret_scan_blocks_commit_with_secret,
        test_secret_scan_blocks_forbidden_terms_without_echoing_them,
        test_secret_scan_allows_clean_commit_and_other_commands,
        test_secret_scan_add_then_commit_in_one_command,
        test_secret_scan_cli_modes,
        test_lint_python_formats_and_reports,
        test_validate_handoff_requires_a_fresh_valid_file,
        test_validate_handoff_role_rules_and_naming,
        test_validate_handoff_ignores_other_agents_and_gives_up_eventually,
        test_validate_handoff_prefers_the_agents_own_worktree,
        test_guard_role_leaves_unrestricted_callers_alone,
        test_guard_role_read_only_roles,
        test_guard_role_docs_writer,
        test_session_context_reports_status,
    ]
    for test in tests:
        failures_before = len(FAILURES)
        try:
            test()
        except Exception as error:  # noqa: BLE001 - report and continue
            FAILURES.append(f"{test.__name__} raised {type(error).__name__}: {error}")
        status = "OK" if len(FAILURES) == failures_before else "FAIL"
        print(f"[{status}] {test.__name__}")

    print(f"\n{len(tests)} tests, {len(FAILURES)} failure(s)")
    for failure in FAILURES:
        print(f"  - {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
