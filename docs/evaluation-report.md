# Translation evaluation

- Provider: fake
- Model(s): fake-model (temperature 0)
- Prompt version: v1
- Dataset: zh_en_hkex.jsonl (30 segments)
- Runs: 3
- Cache: disabled
- Failed segment calls: 0
- Date (UTC): 2026-10-04T20:24:26Z

**Fake model: these numbers measure the pipeline plumbing, not translation quality.**

## Notes

- llm settings read from config with built-in defaults (app.config.llm_settings not available: llm-foundation-t2 not merged)
- PromptBuilder not available (llm-foundation-t2 not merged): no context providers; rag.glossary / rag.translation_memory are no-ops, the three configurations are identical
- no price for fake-model
- workstream B (verification) not merged: check_pass_rate uses the subset translated, non_empty, numbers_preserved

## glossary_adherence

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| fake-model | 0.000 ± 0.000 | 0.000 ± 0.000 | 0.000 ± 0.000 |

## check_pass_rate

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| fake-model | 0.900 ± 0.000 | 0.900 ± 0.000 | 0.900 ± 0.000 |

## similarity

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| fake-model | 0.146 ± 0.000 | 0.146 ± 0.000 | 0.146 ± 0.000 |

## chrf

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| fake-model | 6.975 ± 0.000 | 6.975 ± 0.000 | 6.975 ± 0.000 |

## cost_usd

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| fake-model | 0.000000 ± 0.000000 | 0.000000 ± 0.000000 | 0.000000 ± 0.000000 |

## input_tokens

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| fake-model | 4956.000 ± 0.000 | 4956.000 ± 0.000 | 4956.000 ± 0.000 |

## output_tokens

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| fake-model | 522.000 ± 0.000 | 522.000 ± 0.000 | 522.000 ± 0.000 |

## latency_ms

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| fake-model | 0.347 ± 0.152 | 0.232 ± 0.042 | 0.255 ± 0.054 |

## latency_p95_ms

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| fake-model | 1.060 ± 0.875 | 0.353 ± 0.045 | 0.494 ± 0.202 |
