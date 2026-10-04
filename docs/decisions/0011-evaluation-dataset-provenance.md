# 0011. Evaluation dataset provenance

- Status: Accepted
- Date: 2026-10-04

## Context

The translation suite needs a reference set of zh->en pairs. Test data must be
synthetic or public with its source cited; no real contracts.

## Decision

- The dataset is 30 short extracts from public bilingual announcements
  published on HKEXnews by 4 listed issuers.
- Every document is attributed in `evaluation/dataset/SOURCES.md`: issuer,
  title, date, both document URLs and the number of segments taken.
- Copyright stays with the issuers. Reuse rests on the extracts being short,
  attributed and used for evaluation only. No licence is stated for them;
  this is a caveat, not a grant. Before any wider reuse or redistribution,
  read the terms of use of the HKEXnews / HKEX website.
- `evaluation/dataset/` is protected by the hooks: existing files never
  change; only a human edits the yardstick.
- The finance glossary `data/glossary/finance_zh.csv` is authored generic
  financial vocabulary, not extracted from any document. It was prepared
  outside the repository together with the dataset and is shared
  byte-for-byte with workstream A (retrieval).

## File-name mapping (human decision)

`SOURCES.md` is copied verbatim and shared byte-for-byte with workstream A, so
its file names are kept even though they differ from the repository layout:

| Name in `SOURCES.md` | File in the repository |
| --- | --- |
| `eval_segments.jsonl` | `evaluation/dataset/zh_en_hkex.jsonl` |
| `tm_segments.jsonl` | `data/tm/seed_zh_en.jsonl` (added by workstream A) |

## Consequences

- No document is shared between the evaluation set and the translation
  memory, so the translation-memory configuration cannot leak references.
- Thirty segments give a coarse yardstick: small differences are noise, which
  is why runs are repeated (see 0003).
- Adding a document means updating `SOURCES.md` in both workstreams.
