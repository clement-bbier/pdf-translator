---
name: eval-protocol
description: Step-by-step protocol to run the evaluation, check the references are untouched, compare with the baseline and classify every metric as regression, improvement or unchanged. Use whenever evaluation results are produced or interpreted (evaluator agent, /eval, /feature, /release-check).
---

# Evaluation protocol

The evaluation lives in `evaluation/`. `make eval` runs every suite, compares
each metric with `evaluation/baseline.json` and writes
`evaluation/reports/latest.json` and `latest.md`. Exit code 1 means at least
one metric regressed.

## 1. Check the references are untouched

The dataset (`evaluation/dataset/`) and the baseline
(`evaluation/baseline*.json`) are the yardstick. If they changed, every
comparison is meaningless.

```
git status --porcelain -- evaluation/dataset evaluation/baseline.json
git diff --stat <base> -- evaluation/dataset evaluation/baseline.json
```

Both must print nothing (skip the second when there is no base commit). If
either prints anything: verdict `regression`, risk
`references modified`, stop here.

## 2. Run

```
make eval
```

Do not rerun to get a better number. Deterministic suites (layout, anything
on the fake model) give identical results on every run; a difference between
two runs is itself a bug to report. Suites on a real model are run 3 times by
the harness itself, which reports mean and standard deviation; compare means
and treat a move smaller than the reported tolerance as noise.

## 3. Read and classify

Open `evaluation/reports/latest.json`. For each row of `comparison`:

| status | meaning | action |
| --- | --- | --- |
| `regression` | moved the wrong way beyond tolerance | report: suite.metric, baseline -> current |
| `improvement` | moved the right way | report; only a human may accept it with `make eval-baseline` |
| `unchanged` | within tolerance | nothing |
| `new` | metric absent from the baseline | report; a human adds it with `make eval-baseline` |

The overall verdict is the file's `verdict` field: any regression makes the
whole run a regression. No regression is tolerated in /feature.

## 4. Report

Copy every metric into the handoff `metrics` as `"<suite>.<metric>": value`.
Quote the `headline`. Never edit the baseline, the dataset or the harness to
change a verdict.
