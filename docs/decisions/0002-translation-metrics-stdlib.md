# 0002. Translation metrics use the standard library only

- Status: Accepted
- Date: 2026-10-04

## Context

The translation suite needs a similarity score, a chrF score and a glossary
adherence score over zh->en output. Common packages (sacrebleu, numpy) would
add pinned dependencies to `requirements*.txt` for a few dozen lines of
arithmetic.

## Decision

- Similarity is `difflib.SequenceMatcher(...).ratio()` between output and
  reference.
- chrF is implemented in `evaluation/metrics.py`: character n-grams with
  n = 1..6, beta = 2, averaged over n. Character n-grams need no tokenizer, so
  they work unchanged on Chinese, Japanese and Korean text.
- Glossary adherence is substring matching: a glossary term counts as
  respected when its required target form appears in the output.
- No sacrebleu, no numpy: one pinned dependency fewer, nothing to audit, and
  the whole metric code can be read in one sitting.

## Alternatives considered

- sacrebleu: reference implementation, but a new dependency (and its
  transitive ones) for one metric.
- numpy-based vectorised code: the dataset is 30 segments, speed is irrelevant.

## Limits

- Both scores measure surface overlap only. They do not judge adequacy or
  fluency; a correct paraphrase scores low.
- One reference per segment, so legitimate variants are penalised.
- The chrF here is not bit-identical to sacrebleu (smoothing and whitespace
  handling differ). Compare values from this harness with each other, never
  with published chrF figures.
- `difflib` is sensitive to word order and to the length of the inputs.
- Substring matching for the glossary ignores inflection and word boundaries:
  it can over-count (a term inside a longer word) and under-count (a plural).

## Consequences

- Metric changes are code changes in `evaluation/metrics.py`, covered by
  `tests/`, and shift the numbers: a human must rerun `make eval-baseline`.
