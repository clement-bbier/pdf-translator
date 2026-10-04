---
name: eval
description: Run the evaluation suites and compare them with the committed baseline, through the evaluator agent. Run as /eval.
disable-model-invocation: true
---

# /eval

1. Task id: `eval-<YYYYMMDD-HHMM>` (current UTC time).
2. Spawn the `evaluator` agent with that task id and no base commit.
3. Read `.claude/handoffs/<task-id>-eval.evaluator.json` and
   `evaluation/reports/latest.md`.
4. Reply with the comparison table, the verdict, and:
   - on `regression`: each regressed metric with baseline -> current, and the
     last commits that touched the code it measures (`git log -5 --oneline`);
   - on `improvement` or `new`: remind the human that only they can accept
     the new numbers with `make eval-baseline`.
