# 0002. The glossary uses exact longest-match search

- Status: Accepted
- Date: 2026-10-04

## Context

Financial documents need fixed renderings for recurring terms (for example
`毛利` -> `gross profit`). The glossary (`data/glossary/finance_zh.csv`, 59
terms) is injected into the translation prompt. Terminology must be
deterministic and auditable: for a given text, a reviewer must be able to say
which terms were injected and why, and tests must be able to assert it.

## Decision

`app/rag/glossary.py` finds terms by exact substring match on the source
text. At each position the scan tries terms from longest to shortest
(ties broken alphabetically) and takes the first that matches, then continues
after the matched span. Matches are therefore non-overlapping and a longer
term wins over a term it contains. No normalisation, stemming or scoring is
applied. Only `fr` and `en` targets are served, and only for source language
`zh` (`GlossaryContextProvider.src_languages`). Duplicate sources in the CSV
are rejected at load time.

## Alternatives considered

- Fuzzy matching (edit distance): injects terms that are not in the text,
  and the result depends on a threshold that is hard to audit.
- Embedding retrieval of terms: probabilistic, needs a model or a network
  call, and cannot guarantee that a present term is found.
- Shortest match first or all overlapping matches: a short term inside a
  longer one would inject two conflicting renderings.

## Consequences

- Output is reproducible and testable without a model.
- Variants that are not listed (other inflections, typos, a different
  spelling of the same term) are not matched; they must be added as rows.
- Traditional vs Simplified Chinese: the glossary sources are Traditional
  Chinese as used in Hong Kong filings. Simplified text does not match
  (for example `經營盈利` is found, its Simplified form is not). There is no
  automatic conversion; supporting Simplified means adding Simplified rows
  or a separate glossary file via `rag.glossary_path`. This limitation is
  accepted for now.
- Source languages other than `zh` get no glossary context.
