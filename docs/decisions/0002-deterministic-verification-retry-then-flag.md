# 0002. Verify translations with deterministic code checks, retry once, then flag

- Status: Accepted
- Date: 2026-10-04

## Context

Contracts are translated by an LLM that can drop or alter a figure, a date or
a required glossary term, or return a malformed answer. The tool must not hand
such a translation to a reader without saying so. Constraints: a failed batch
must never kill a job, cost must stay bounded, and a reviewer must be able to
see why a segment was flagged.

## Decision

`app/verification/` runs pure, deterministic checks on every batch answer of an
LLM-backed translator: JSON validity, segment count, required glossary terms,
numbers and currencies, dates. On a finding, the batch is re-sent once
(`verification.max_retries: 1`, range 0-3) with the findings appended as a
corrective prompt (`prompts/v1/retry_corrections.md`). Per segment the best
attempt is kept: fewest findings, a present value beating a missing one, ties
to the latest attempt. A segment still failing is flagged: it is written to the
PDF as translated, listed in `review.json`, and never stored in the cache.
If no attempt is parseable JSON, the batch fails like any other batch (job
warning, source text kept).

`verification.enabled` is `true` in `config.yaml` (human decision). The mock
translator is never checked.

## Alternatives considered

- LLM-as-judge: adds a call per batch, is non-deterministic, may share the
  translator's blind spots, and gives no testable, explainable verdict. Code
  checks are unit-testable and each finding states what is missing.
- No retry, flag only: leaves fixable errors to a human.
- Several retries: cost grows per batch for diminishing returns. One
  corrective pass fixes most failures; the rest is better read by a person.
- Caching flagged segments: would serve an unresolved error silently on the
  next run.

## Consequences

- Cost is at most one extra call per failing batch (the whole batch is
  re-sent).
- Checks lean towards few false positives, since each one costs a retry and a
  review; some real errors pass.
- Known limits: numbers spelled out in words, Chinese-numeral amounts outside
  dates, and the semantic meaning of a sentence are not checked. Cache entries
  written before verification existed are served unchecked.
- The check set is closed (`CHECK_NAMES` in `app/verification/findings.py`);
  a new check means a new function plus a test, not a prompt change.
