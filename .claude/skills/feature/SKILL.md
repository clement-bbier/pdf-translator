---
name: feature
description: Full feature workflow - planner, human approval of the plan, parallel implementers in git worktrees, reviewer (max 2 rounds), evaluator (no regression), security-auditor, docs-writer, then a summary and a no-fast-forward merge. Run as /feature <description>.
argument-hint: <feature description>
disable-model-invocation: true
---

# /feature workflow

Request: **$ARGUMENTS**

You are the orchestrator. You delegate every step to the workflow agents in
`.claude/agents/`, read their handoffs, and move the work between them. You
do not write feature code yourself. Each agent ends by writing a handoff in
`.claude/handoffs/`; a SubagentStop hook refuses to let it stop until that
file is valid, so a handoff always exists when an agent returns.

Validate any handoff you read with
`python .claude/hooks/handoff.py <file>` (use the venv interpreter, see
CLAUDE.md) before acting on it.

## 0. Preflight

1. `git status --porcelain` must be empty. If not, stop and ask the human to
   commit or stash.
2. Derive the feature id: a kebab-case slug of the request, at most 30
   characters (e.g. `cli-version`).
3. Record the base: `BASE=$(git rev-parse HEAD)` and the starting branch
   `git rev-parse --abbrev-ref HEAD`. Write both in your notes; you need them
   at every step.
4. Create the feature branch: `git switch -c feature/<feature-id>`.

## 1. Plan, then STOP for the human

Spawn the `planner` agent with the feature id and the request. Read
`.claude/handoffs/<feature-id>.planner.json`.

- `blocked`: show the open questions to the human and stop.
- `done`: show the plan as a table (task id, title, depends_on, acceptance
  criteria) followed by its risks.

**Human checkpoint.** End your turn with: "Reply `approve` to start
implementation, or describe the changes you want." Do not spawn any
implementer before the human approves. If the human asks for changes, run the
planner again with their feedback and stop again.

## 2. Implement in parallel

Group the tasks in waves: a task joins a wave once everything in its
`depends_on` is integrated. For each wave:

1. Spawn one `implementer` per task **in a single message** (parallel calls),
   each with: the feature id, the task (id, description, acceptance
   criteria, files_hint). Each runs in its own worktree branched from the
   current HEAD of `feature/<feature-id>`.
2. For each result: read `<worktree>/.claude/handoffs/<task_id>.implementer.json`
   (the worktree path is in the agent's final message) and copy it to the main
   checkout's `.claude/handoffs/`. If any is `blocked`, stop and report.
3. Integrate in plan order, in the main checkout:
   `git cherry-pick <branch-base>..<worktree-branch>`. Resolve a conflict only
   if it is mechanical (both sides kept as-is); otherwise abort the
   cherry-pick, stop and report.
4. Run `make test`. If it fails after integration, send the failure to a new
   implementer as a fix task and integrate its commit.
5. Remove each integrated worktree: `git worktree remove <path>` then
   `git branch -d <worktree-branch>`.

## 3. Review (at most 2 rounds)

Spawn the `reviewer` with: feature id, `BASE`, the tasks and their acceptance
criteria, round number. Read `.claude/handoffs/<feature-id>-review-r<N>.reviewer.json`.

- `approve`: go to step 4.
- `changes_requested` in round 1: spawn implementer(s) with the `REQUIRED:`
  items as a fix task (`<feature-id>-fix-r1`), integrate as in step 2, then
  review round 2.
- `changes_requested` in round 2: stop. Report the remaining findings to the
  human; do not merge.

## 4. Evaluate (no regression tolerated)

Spawn the `evaluator` with task id `<feature-id>` and `BASE`. Read
`.claude/handoffs/<feature-id>-eval.evaluator.json`.

- `done` (pass or improvement): continue.
- `blocked` (regression): give the regressed metrics to one implementer as a
  fix task, integrate, re-run the evaluator once. Still regressing: stop and
  report. Never edit the baseline or dataset to make it pass.

## 5. Security audit

Spawn the `security-auditor` with task id `<feature-id>` and scope `BASE`.
Any `BLOCKER:` finding: one implementer fix task, integrate, re-audit once;
still blocked: stop and report.

## 6. Documentation

Spawn the `docs-writer` with the feature id, `BASE`, the plan and the list of
handoff files. Then commit its changes on the feature branch:
`git add <files> && git commit -m "docs(<feature-id>): <summary>"`.

## 7. Summary and merge

1. `make check` must pass on the feature branch.
2. Validate every handoff of the run:
   `python .claude/hooks/handoff.py .claude/handoffs/<feature-id>*.json`.
3. Write `.claude/handoffs/<feature-id>.summary.md`: request, tasks and
   commits, review rounds and verdict, evaluation verdict and key metrics,
   security findings, docs changed, open risks.
4. Merge into the starting branch, keeping the trace in history:
   `git switch <starting-branch>` then
   `git merge --no-ff feature/<feature-id> -m "feat(<feature-id>): <request>"`
   with a body listing: plan (task ids), review verdict and round, evaluation
   verdict, security result. Delete the feature branch (`git branch -d`).
   Never push.
5. Final message to the human: the summary (tasks, commits, verdicts, metrics,
   open risks) in under 25 lines.
