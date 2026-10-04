# 0004. LLM traces are local-only; the trace file is the only place content goes

- Status: Accepted
- Date: 2026-10-04

## Context

Debugging and cost tracking need a record of every LLM call: which prompt
version and model, tokens, duration, estimated cost, and the answer. The
project rule is that application logs carry ids and counters only, never
document text, because documents may be confidential contracts.

## Decision

- `TracingProvider` appends one JSON line per call to `traces/llm-calls.jsonl`
  (`tracing.path`): timestamp, `input_id` (request fingerprint), prompt
  version, model, params, duration, token counts, `estimated_cost_usd`,
  `cache_hit`, status, error type, and `output` (the model answer, i.e.
  translated text).
- Input messages and image data are not written. `input_id` identifies them.
- The trace file is the only place where call content is written. The
  application logger (`logging`) stays content-free: it records type names,
  counters and file names of the cache, never text.
- `traces/` is gitignored and must never be shipped (no Docker image, no
  release artifact, no upload). It is not covered by the per-job purge or the
  30-minute TTL.
- Tracing is on by default (`tracing.enabled: true`); it can be turned off.
  Whether the default should change is left to the maintainer.

## Alternatives considered

- Log content to the application logger: violates the logging rule and
  reaches log aggregation.
- Store no output, only ids and counters: safe, but loses the ability to
  inspect bad translations.
- Encrypt or rotate the file: not implemented; files use default permissions
  and have no size cap.

## Consequences

- Translated contract text persists on disk beyond a job, in this file and in
  the response cache (`tmp/llm-cache.json`) and the block cache
  (`tmp/cache.json`). The README states this and how to delete or disable it.
- On a shared machine, restrict the directory or disable tracing and caches.
- Anything that packages the repository must exclude `traces/` and `tmp/`.
