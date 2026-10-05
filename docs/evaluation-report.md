# Translation evaluation

- Provider: anthropic
- Model(s): claude-haiku-4-5-20251001 (temperature 0.0), claude-sonnet-5-5 (temperature not sent)
- Prompt version: v1
- Dataset: zh_en_hkex.jsonl (30 segments)
- Runs: 3
- Cache: disabled
- Failed segment calls: 0
- Date (UTC): 2026-10-05T00:41:14Z

## glossary_adherence

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| claude-haiku-4-5-20251001 | 0.653 ± 0.012 | 1.000 ± 0.000 | 1.000 ± 0.000 |
| claude-sonnet-5-5 | 0.674 ± 0.012 | 1.000 ± 0.000 | 1.000 ± 0.000 |

## check_pass_rate

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| claude-haiku-4-5-20251001 | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 |
| claude-sonnet-5-5 | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 |

## similarity

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| claude-haiku-4-5-20251001 | 0.684 ± 0.001 | 0.684 ± 0.001 | 0.679 ± 0.003 |
| claude-sonnet-5-5 | 0.726 ± 0.005 | 0.726 ± 0.002 | 0.716 ± 0.007 |

## chrf

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| claude-haiku-4-5-20251001 | 65.562 ± 0.024 | 66.770 ± 0.033 | 67.212 ± 0.031 |
| claude-sonnet-5-5 | 71.677 ± 0.097 | 71.994 ± 0.365 | 72.370 ± 0.314 |

## cost_usd

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| claude-haiku-4-5-20251001 | 0.019935 ± 0.000005 | 0.020472 ± 0.000003 | 0.028020 ± 0.000005 |
| claude-sonnet-5-5 | 0.047307 ± 0.000097 | 0.049953 ± 0.000581 | 0.056667 ± 0.000306 |

## input_tokens

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| claude-haiku-4-5-20251001 | 10730.000 ± 0.000 | 11549.000 ± 0.000 | 17920.000 ± 0.000 |
| claude-sonnet-5-5 | 12915.000 ± 0.000 | 13975.000 ± 0.000 | 17690.000 ± 0.000 |

## output_tokens

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| claude-haiku-4-5-20251001 | 1841.000 ± 1.000 | 1784.667 ± 0.577 | 2020.000 ± 1.000 |
| claude-sonnet-5-5 | 2147.667 ± 9.713 | 2200.333 ± 58.055 | 2128.667 ± 30.616 |

## latency_ms

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| claude-haiku-4-5-20251001 | 1559.985 ± 58.068 | 1560.129 ± 97.016 | 1743.508 ± 48.178 |
| claude-sonnet-5-5 | 2071.581 ± 29.945 | 2041.099 ± 37.195 | 2003.043 ± 27.450 |

## latency_p95_ms

| Model | no_rag | glossary | glossary_tm |
| --- | ---: | ---: | ---: |
| claude-haiku-4-5-20251001 | 2930.025 ± 386.415 | 2905.134 ± 282.141 | 3314.567 ± 85.764 |
| claude-sonnet-5-5 | 2542.802 ± 103.503 | 2569.744 ± 131.116 | 2273.023 ± 63.215 |
