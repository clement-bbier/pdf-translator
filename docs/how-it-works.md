# How it works: the journey of one segment

This page follows one real sentence through the pipeline, from the PDF to the
translated page. The data below comes from an actual run with
`claude-haiku-4-5-20251001` at temperature 0 (prompt version `v1`, glossary on,
translation memory off, verification on).

The sentence comes from a public notice of HSBC Holdings dated 23 July 2026,
published on HKEXnews (hkexnews.hk). It is quoted here as a test input only.

> 根據《香港聯合交易所有限公司證券上市規則》第13.43條的規定，滙豐控股有限公司宣布將於2026年8月4日召開董事會下設委員會會議（「董事會議」），審議截至2026年6月30日止六個月期間之中期業績（「業績」）公告及會否派發2026年普通股第二次股息（「股息」）。

## 1. Extraction into blocks

`app/extractor.py` (`extract`, `_extract_page`) opens the PDF with PyMuPDF and
returns, per page, text blocks: the text, its bounding box, font size, bold and
italic flags, and a dominant colour. Columns are split (`_split_columns`), and
a page with no extractable text is marked as scanned. This step is
deterministic and involves no model. Our sentence becomes one block with an id.

## 2. Deduplication and the block cache

`app/pipeline.py` (`_translate_file`) collects the distinct texts of the file
and asks `TranslationCache` (`app/cache.py`) which are already known. The key
is `sha256(model|prompt_version|src|tgt|text)`, stored in `tmp/cache.json`, so
changing the model or the prompt version never serves an old translation.
Repeated texts (page numbers, headers) are translated once. Our sentence is
new, so it goes on.

## 3. Batching

`_translate_missing` splits the unseen texts into batches of `batch_size`
(20) and calls `translator.translate_blocks(batch, src, tgt)` once per batch.
In `LLMTranslator.translate_blocks` (`app/llm/translator.py`) each batch
becomes a JSON object whose keys are the positions in the batch. Here the batch
holds one segment, key `"0"`.

## 4. RAG context

Before the prompt is built, `RagContext.render` (`app/rag/context.py`) asks
the context providers for help. The glossary provider (`app/rag/glossary.py`)
searches the batch text for glossary terms, longest match first, exact string
comparison. Four terms matched in this sentence (six occurrences):

```text
Required terminology (use these target terms exactly):
- 上市規則 => Listing Rules
- 董事會 => Board
- 中期業績 => interim results
- 股息 => dividend
```

The translation memory was off in this run, so `context_sources` for the
request is `{"glossary": 4}`: the trace records which providers contributed and
how many items, never the content. The required target terms are also kept
aside (each once per segment) for the verification step.

## 5. The exact message sent

`PromptBuilder` (`app/llm/prompts.py`) fills the files in `prompts/v1/`. The
system message (`translate_blocks.system.md`) asks for a faithful legal
translation that keeps numbering, amounts, dates and proper nouns, and for a
JSON object with the same keys as the input, with "no markdown formatting". The
user message (`translate_blocks.user.md`) is the language pair, the context and
the payload:

```text
Source language: zh
Target language: en

Required terminology (use these target terms exactly):
- 上市規則 => Listing Rules
- ... (the four lines above)

{"0": "根據《香港聯合交易所有限公司證券上市規則》第13.43條的規定，..."}
```

## 6. The provider stack

`app/llm/factory.py` builds `TracingProvider(CachedProvider(base))` where
`base` is `AnthropicProvider`, `GatewayProvider` or `FakeProvider`.

- **Cache** (`app/llm/caching.py`): the key is `request_fingerprint`
  (`app/llm/base.py`), a sha256 of model, prompt version, temperature,
  `max_tokens`, response format and messages. On a hit the model is not called.
  This request's fingerprint starts with `9dca64c4`. Failures are never cached,
  and a response the translator rejects is invalidated.
- **Trace** (`app/llm/tracing.py`): one JSON line appended to
  `traces/llm-calls.jsonl` with `timestamp`, `input_id` (the fingerprint),
  `prompt_version`, `model`, `params`, `context_sources`, `output`,
  `duration_ms`, `input_tokens`, `output_tokens`, `estimated_cost_usd`,
  `cache_hit`, `status`, `error_type`. The model output is the only content in
  the line; the input is represented by `input_id`. Nothing goes to the
  application logger.

The request also carries a JSON schema of the batch (`batch_response_format`
in `app/llm/translator.py`: exactly the key `"0"`, a string, nothing else).
`AnthropicProvider` sends it as `output_config.format` (structured outputs), so
the model is constrained to return one object of that shape.

This call used 450 input tokens (the schema adds to the prompt) and 117 output
tokens. At the configured prices (USD 1.00 and 5.00 per million tokens) that is
about USD 0.0010.

## 7. The raw answer, and why it is constrained

With the schema, the model answered exactly one object:

```text
{"0": "Pursuant to Rule 13.43 of the Listing Rules of The Stock Exchange of Hong Kong Limited, HSBC Holdings plc announces that ..."}
```

Before the schema, the same request came back wrapped in a Markdown fence
(a line with three backticks and `json`, the object, three backticks), despite
the prompt's instruction. The fake provider never does this, so every test was
green; the first real end-to-end run made every batch fail `json_valid`. The
first real evaluation then showed a second habit: Claude Sonnet 5.5 sometimes
wrote an object, a correction note, and a second object. Two layers handle
this: structured outputs for the Anthropic provider, and `strip_code_fence`
(`app/llm/answer.py`), which removes a single fence wrapping the whole answer
for providers that do not get the schema (the gateway). See
[decision 0013](decisions/0013-structured-outputs-and-fence-fallback.md).

## 8. Verification

`verify_with_retry` (`app/verification/policy.py`) runs `run_checks`
(`app/verification/checks.py`) on the parsed answer. Five checks ran:

| Check | Result for this segment |
| --- | --- |
| `json_valid` | the answer is a JSON object |
| `segment_count` | key `"0"` is present |
| `glossary_terms` | `Listing Rules`, `Board`, `interim results`, `dividend` all appear |
| `numbers` | `13.43` is in the translation |
| `dates` | 4 August 2026 and 30 June 2026 are found (the dates check understands that `2026年8月4日` is "4 August 2026") |

No findings, so there was no retry and the segment is not flagged.

**What happens on failure.** The findings are turned into a corrections message
(`prompts/v1/retry_corrections.md`, `app/verification/retry_prompt.py`) and the
batch is sent once more (`verification.max_retries`, default 1). For each
segment the attempt with the fewest findings is kept. A segment that still has
findings is flagged: it is kept in the PDF as translated, written to
`review.json` with its source, translation and failed checks, and is not put in
the cache, so the next run checks it again. If no attempt is valid JSON at all,
the batch fails, a warning is added to the job, and the source text stays on
the page.

## 9. Result

The translation stored in the cache and used for the rebuild:

> Pursuant to Rule 13.43 of the Listing Rules of The Stock Exchange of Hong
> Kong Limited, HSBC Holdings plc announces that it will convene a meeting of a
> committee established under the Board on 4 August 2026 (the "Board meeting")
> to consider the announcement of interim results for the six-month period
> ended 30 June 2026 (the "results") and whether to declare a second dividend
> on ordinary shares for 2026 (the "dividend").

## 10. Rebuild into the page

`app/rebuilder.py` (`rebuild`) works per page in two passes. Pass 1 adds a
redaction for the bounding box of every translated block and applies them
(text removed, images and line art kept). Pass 2 (`_write_block`) picks a font
from the script of the text (`app/fonts.py`), then `_fit_textbox` tries the
original font size and shrinks it step by step down to `font_min_scale` (0.6)
of the original. If the text still does not fit it is truncated with `...`.
English is usually longer than the same Chinese text, so shrinking is common. The
pages are saved, zipped with `review.json` when something was flagged, and
offered for download; the job directory is purged afterwards.
