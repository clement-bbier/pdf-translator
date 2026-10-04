# 0003. Temperature 0 is not treated as determinism: request cache and multi-run evaluation

- Status: Accepted
- Date: 2026-10-04

## Context

`config.yaml` pins `llm.temperature: 0` and a dated model id
(`claude-haiku-4-5-20251001`). Even so, hosted models can return different
text for the same request (batching, hardware, backend changes), and some
models (Sonnet/Opus 5.x) accept no temperature at all. Re-running a job or an
evaluation must not silently change the output, and a measurement from one
run must not be taken as the model's behaviour.

## Decision

- Pin the dated model id, temperature 0 and `prompt_version`; every one of
  them is part of the request.
- `CachedProvider` caches responses by `request_fingerprint` (sha256 of model,
  prompt version, temperature, max_tokens, response format and messages).
  Replaying an identical request returns the stored answer instead of
  sampling again, and costs nothing. Failures are never cached, and a
  response the translator rejects (invalid JSON, missing keys) is invalidated.
- `TranslationCache` (per block) keys include model and prompt version, so
  changing either never serves an old translation.
- Evaluation of a real model must compare several runs, and `compare_models`
  in config lists the models to compare against (evaluation only). A single
  run proves nothing about stability. Multi-run reporting is not implemented
  in `evaluation/` yet; today `compare_models` is only validated config.

## Alternatives considered

- Rely on temperature 0 alone: not guaranteed by the API.
- Seed parameter: not offered by the Messages API.
- No cache, always re-call: costs money and makes reruns drift.

## Consequences

- Reruns are reproducible while the cache file exists; deleting
  `tmp/llm-cache.json` makes the next run sample again.
- To measure variance, disable the cache (`cache.enabled: false`) or use a
  different path, otherwise every run returns the first answer.
- The cache file holds model output, see 0004.
