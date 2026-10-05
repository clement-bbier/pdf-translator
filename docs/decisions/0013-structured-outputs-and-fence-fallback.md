# 0013. Constrain the answer with structured outputs, and strip a Markdown fence as a fallback

- Status: Accepted
- Date: 2026-10-04

## Context

The prompt (`prompts/v1/translate_blocks.system.md`) asks for a bare JSON
object and says not to use markdown formatting. Two real-model behaviours
showed that the instruction alone is not enough:

1. The first real end-to-end run in the container, with Claude Haiku 4.5,
   answered with the JSON wrapped in a fenced block (a triple backtick
   followed by `json`). `json.loads` rejected it, `json_valid` failed twice and
   every batch stayed untranslated, although the translations were correct.
2. The first real evaluation run, with Claude Sonnet 5.5, failed 8 segment
   calls out of 540: on some segments the model wrote a first JSON object, a
   line such as "Wait - correcting for fidelity", then a second object. The
   answer was not one JSON object, so `json_valid` failed on both attempts.

No test caught either: the fake provider returns bare JSON.

## Decision

Two layers.

- **Structured outputs where the provider supports them.** For every block
  batch, `LLMTranslator` attaches a JSON schema (`batch_response_format`):
  an object with exactly the batch keys `"0"..."n"`, each a string, no other
  key. `AnthropicProvider` sends it as `output_config.format`, so Claude is
  constrained to return one object matching the schema. Measured after the
  change: 6/6 valid single objects on the segment that failed for Sonnet 5.5,
  and no fence from Haiku 4.5.
- **Tolerant parsing everywhere.** `strip_code_fence` in `app/llm/answer.py`
  removes one Markdown fence that wraps the whole answer and nothing else. It
  is applied in `LLMTranslator` and in `app/verification/structure.py`, so the
  verified and unverified paths agree. It covers the gateway provider, which
  does not forward the schema (structured-output support varies across
  OpenAI-compatible gateways, and its request body is kept unchanged).

The fake provider gained a `fenced_json` fault flag; tests cover the fence,
the schema, and the `output_config` mapping.

## Alternatives considered

- Insist harder in the prompt: the instruction already exists and was
  ignored; compliance is not guaranteed, now or after a model upgrade.
- Extract the first `{` to the last `}`, or keep the last object: accepts
  malformed answers and hides real failures from `json_valid`.
- Forward the schema to the gateway as an OpenAI `response_format`: not every
  gateway or model behind it supports it; left out until a gateway is tested.

## Consequences

- The schema is part of the request fingerprint, so cache keys changed once.
- `json_valid` still guards the gateway and any provider that ignores the
  schema; nothing else is loosened.
- Cached answers and trace lines keep what the model actually returned.
- General lesson: a fake model cannot cover a real model's output habits.
  Each habit seen in a real run is added to the fake as a fault flag and
  handled in code.
