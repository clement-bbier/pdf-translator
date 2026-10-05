# 0010. Three runs, mean and standard deviation, per-model tolerance

- Status: Accepted
- Date: 2026-10-04

## Context

A real model does not return the same text twice: temperature 0 is not
bit-for-bit deterministic, and the Sonnet 5.5 comparison model accepts no
temperature at all. A single run cannot tell a regression from noise. The
offline fake model, on the other hand, is fully deterministic.

## Decision

- Each model x configuration is run 3 times (`RUNS` in
  `evaluation/translation_suite.py`). The report carries the mean and the
  sample standard deviation of every metric.
- The translation cache is disabled during evaluation: a cache hit would
  replay the first run and hide the variance. Providers are built directly,
  without tracing, so no document text is written to disk.
- Tolerance: 0 for the fake model; 2 standard deviations on the means for real
  models (the larger of the current and baseline std). Latency and std metrics
  are informational (status `info`): they never count as a regression.
- Three configurations are compared: `no_rag`, `glossary`, `glossary_tm`.
- The baseline is merged per suite. Real-model numbers are stored under the
  suite key `translation@anthropic`, fake-model numbers under `translation`.
  Metrics absent from the baseline show as `new` until a human runs
  `make eval-baseline`.

## Warning: the fake model flatters RAG

The fake model applies glossary lines found in its prompt literally, so
retrieval-augmented configurations look better than they would with a real
model, by construction. **Fake-model results measure the pipeline plumbing,
not translation quality.** The published report says so in bold.

## Integration status

Until workstreams A (retrieval) and B (verification) are merged, the three
configurations are identical. `translation_suite.py` binds to them through
adapters marked `ADAPTER(...)`: `app.rag.GlossaryContextProvider`,
`app.rag.TranslationMemoryContextProvider` and `app.verification.run_checks`.
When an interface is missing the adapter falls back and the report lists the
fallback under Notes. At merge, each adapter is rewired to the real module.

## Consequences

- A real-model evaluation costs 3 runs x 3 configurations x models x 30
  segments of API calls.
- With few runs, the standard deviation is itself a rough estimate; the 2-std
  band is a pragmatic noise filter, not a statistical guarantee.
