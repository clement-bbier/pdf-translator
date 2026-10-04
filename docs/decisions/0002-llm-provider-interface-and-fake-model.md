# 0002. LLM access goes through a provider interface, tested with a deterministic fake model

- Status: Accepted
- Date: 2026-10-04

## Context

Translation used to call an OpenAI-compatible gateway directly from
`app/translator.py`. We now also need the Anthropic Messages API, pinned model
ids, a request cache and call tracing. Tests and CI must not call a real model
or the network, and the evaluation harness needs reproducible output.

## Decision

- `app/llm/base.py` defines `LLMRequest`, `LLMResponse` and the `LLMProvider`
  protocol (`complete(request) -> response`). `LLMTranslator` implements the
  `Translator` interface on top of any provider; prompts come from
  `PromptBuilder` (`prompts/<version>/`).
- Providers: `AnthropicProvider`, `GatewayProvider` (the former gateway code),
  and `FakeProvider`. The factory wraps the base provider with `CachedProvider`
  then `TracingProvider`.
- `FakeProvider` answers deterministically from the prompt (prefixes each
  segment with `[tgt]`, renders dates, can simulate bad output). Tests, CI and
  `make eval` use it or `MockTranslator`; no test calls a real model.
- New pinned dependencies: `anthropic==1.11.0` and `httpx2==2.13.1`.
  `anthropic` 1.11.0 declares `httpx2>=2.0.0,<3` (an httpx fork maintained
  by Pydantic), so it is pinned explicitly. The existing `httpx==0.28.1` stays
  for the gateway provider and `httpx.MockTransport` tests.
- We use the SDK for its typed errors (`RateLimitError`, `InternalServerError`,
  `APIConnectionError`, `APIStatusError`), which `AnthropicProvider` maps to
  retryable or fatal errors. SDK retries are off (`max_retries=0`): the only
  retry policy is `app/llm/retry.py` (3 attempts, exponential backoff), shared
  with the gateway provider.
- This SDK release has no `temperature` keyword on `messages.create()`.
  Temperature is sent through `extra_body`, and only for models that accept
  sampling parameters. Model ids starting with `claude-sonnet-5` or
  `claude-opus-5` reject it, so it is omitted for them
  (`temperature: null` in config also means "not sent").

## Alternatives considered

- Raw `httpx` calls to the Messages API: no new dependency, but we would
  re-implement error typing and request/response models.
- Keep one translator per backend: duplicates prompts, retry and tracing.
- Recorded real responses in tests: tie the suite to one model and to stored
  document text.

## Consequences

- Adding a backend means one class implementing `complete`.
- Two HTTP client libraries are installed. Transitive dependencies of the SDK
  (`httpcore2`, `anyio`, `jiter`, ...) are not pinned; there is no lock file.
- The fake model validates plumbing, not translation quality. Quality is only
  measured with a real model, outside CI.
- The sampling-model list is a name-prefix check and must be updated when a
  new model family rejects temperature.
