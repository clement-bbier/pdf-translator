# 0013. Strip a Markdown fence around the model's JSON answer instead of trusting the prompt

- Status: Accepted
- Date: 2026-10-04

## Context

The prompt (`prompts/v1/translate_blocks.system.md`) asks for a bare JSON
object and says not to use markdown formatting. The first real end-to-end run
in the container, with Claude Haiku 4.5, showed the model answering with the
JSON wrapped in a fenced block (the opening line is a triple backtick followed
by `json`). `json.loads` rejected it, the `json_valid` check failed, and batches
failed even though the translations were correct. No test had caught it: the
fake provider returns bare JSON, so the whole suite was green.

## Decision

`strip_code_fence` in `app/llm/answer.py` removes one Markdown fence that
wraps the whole answer, with or without a language tag, and returns the inner
text. Anything else is returned unchanged, so a fence in the middle of text,
prose around the JSON, or a truncated answer still reaches the JSON parser
and fails `json_valid` as before. It is applied in `LLMTranslator` and in
`app/verification/structure.py`, so the verified and unverified paths agree.
Tests cover fenced and bare answers for both.

## Alternatives considered

- Insist harder in the prompt: the instruction already exists and was
  ignored; a model's compliance is not guaranteed, now or after an upgrade.
- Provider structured outputs or response-format options: they exist for some
  providers, but the translator is one code path shared by the Anthropic,
  gateway (OpenAI-compatible) and fake providers, and the options differ per
  provider. Tolerant parsing works for all three without per-provider code.
- Extract the first `{` to the last `}`: accepts more malformed answers
  (prose, several objects) and hides real failures from `json_valid`.

## Consequences

- Fenced and bare answers are both accepted; nothing else is loosened.
- Cached answers are stored raw (with the fence) and stripped at parse time, so
  the cache and the trace show what the model actually returned.
- The lesson is general: behaviour of a real model cannot be fully covered by
  a fake. The fake does not emit fences; if other wrapping shows up in real
  runs it should be added to the fake and handled here the same way.
