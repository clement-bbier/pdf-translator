# 0003. Translation memory uses a local hashing embedder by default

- Status: Accepted
- Date: 2026-10-04

## Context

The translation memory (`data/tm/seed_zh_en.jsonl`, `app/rag/memory.py`)
retrieves validated source/translation pairs similar to the text being
translated and injects them as examples. Retrieval needs vectors. The project
has few dependencies, tests must not use a network or a real model, and
results must be deterministic.

## Decision

The default embedder (`rag.embedder: hashing`) is `HashingEmbedder`, written
with the standard library only: character n-grams of length 1 to 3 are hashed
into a fixed-size vector, which is L2-normalised, so the dot product is the
cosine similarity. Vectors of the TM are cached under `tmp/rag/`
(gitignored), with the embedder id in the file name.

A `GatewayEmbedder` is available as an opt-in swap
(`rag.embedder: gateway`, model from `rag.embedding_model`, default
`text-embedding-3-small`). It calls the gateway `/embeddings` endpoint and
needs `GATEWAY_BASE_URL`.

`rag.tm_min_score` defaults to 0.3. Measured leave-one-out on the seed
(each entry queried against the others, top-1 score): min 0.182, median
0.461, max 0.686. 0.3 is a starting value, not a tuned one; revisit it when
the TM grows or the embedder changes. Translation memory is off by default
(`rag.translation_memory: false`).

## Alternatives considered

- Local neural model (sentence-transformers or similar): large dependency,
  model download, non-trivial CI cost, slower tests.
- Gateway embeddings only: needs a network and credentials for every test
  and local run, and is not deterministic across model updates.
- No translation memory: loses consistency with previously validated
  renderings.

## Consequences

- No new dependency, deterministic and offline tests.
- Character n-grams capture surface overlap only. They do not capture
  paraphrase or semantic similarity without shared characters, and do not
  work across scripts (a Chinese query does not match English text). Entries
  are matched per (source, target) language pair; the seed is zh -> en.
- Privacy: with `rag.embedder: gateway`, each segment text is sent to the
  gateway `/embeddings` endpoint whatever translation provider is selected
  (including the offline `fake` and the `anthropic` providers). This is a second outbound
  recipient of document text, found by the security audit. It is opt-in and
  off in the default configuration; enable it only where that is acceptable.
- The score distribution above is small-sample; the threshold may drop
  useful low-scoring matches or keep weak ones.
