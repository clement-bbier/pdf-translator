# 0012. Claude Haiku 4.5 (dated id, temperature 0) is the default model, Sonnet 5.5 is for comparison

- Status: Accepted
- Date: 2026-10-04

## Context

The translation step needs a model whose behaviour can be pinned and whose
runs can be compared. Two facts constrain the choice. First, aliases such as
`claude-haiku-4-5` can move to a new snapshot, so a result produced today may
not be reproducible later; a dated id does not move. Second, the Sonnet and
Opus 5.x models reject sampling parameters: a request carrying `temperature`
is refused, so there is no way to ask for the most conservative sampling.
Haiku 4.5 has a dated id (`claude-haiku-4-5-20251001`) and accepts a
temperature. Documents are translated in many batches, so price and latency
per call also matter.

## Decision

- `llm.model: claude-haiku-4-5-20251001` and `llm.temperature: 0` in
  `config.yaml`. The dated id and the temperature are part of the request
  fingerprint, so they are also part of the cache key.
- `claude-sonnet-5-5` is listed in `llm.compare_models` and measured by the
  evaluation next to the default. Its temperature is `null` (not sent). The
  code omits `temperature` for ids starting with `claude-sonnet-5` or
  `claude-opus-5`.
- Prices are configured per model (`llm.prices`, USD per million tokens:
  Haiku 1.00/5.00, Sonnet 5.5 2.00/10.00) to estimate cost in traces and
  reports.

## Alternatives considered

- An alias such as `claude-haiku-4-5`: not pinned, results drift silently.
- Sonnet 5.5 as the default: no dated id and no temperature control, and
  higher cost per call; kept as the comparison point instead.
- A model chosen per job only (the gateway model list): remains available for
  the gateway provider, but gives no pinned reference for the evaluation.

## Consequences

- A model upgrade is a deliberate edit of `config.yaml`, and it changes cache
  keys, so old cached translations are not served for the new model.
- Temperature 0 reduces variance but does not give identical outputs
  (decision 0003), hence the request cache and repeated evaluation runs.
- Whether Haiku or Sonnet translates better on this dataset is an empirical
  question answered by `make eval-real`, not by this note.
- Dated ids are retired by the provider at some point; the default will have
  to be updated then.
