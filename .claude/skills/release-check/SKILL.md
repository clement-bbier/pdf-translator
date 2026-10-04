---
name: release-check
description: Pre-push gate - lint, tests, agentic config validation, evaluation, security audit and documentation check, ending in a GO / NO-GO table. Run as /release-check before any push.
disable-model-invocation: true
---

# /release-check

Run every gate even if an earlier one fails, then report them all. Change
nothing: this is a check, not a fix.

Task id: `release-<YYYYMMDD-HHMM>`.

1. **Lint**: `make lint`.
2. **Tests**: `make test` (includes the hook tests). Note the counts.
3. **Agentic config**: `make validate`.
4. **Evaluation**: spawn the `evaluator` (task id above, no base). Gate fails
   on `regression`.
5. **Security**: spawn the `security-auditor` with scope `release`. Gate fails
   on any `BLOCKER:`; list `MAJOR:` findings.
6. **Docs**: spawn the `docs-writer` in **check mode**: it must not edit any
   file; it reports in its handoff `risks` every README/CLAUDE.md/docs
   statement that no longer matches the code (commands, paths, config keys,
   architecture), and sets `status` to `blocked` if any is found.
7. **Git state**: `git status --porcelain` empty; branch is not behind its
   upstream (`git status -sb`).

Reply with one table: gate, result (PASS / FAIL / NOT RUN), evidence (counts,
verdict, handoff path). Last line: `GO` only if every gate passed, else
`NO-GO` with the blocking items. Never push yourself.
