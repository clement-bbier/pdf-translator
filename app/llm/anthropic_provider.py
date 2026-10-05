"""Provider for the Anthropic Messages API.

The API key comes from the ANTHROPIC_API_KEY environment variable (loaded from
.env by app.config); it is never logged. The SDK's own retries are disabled so
that app.llm.retry is the only retry policy.
"""

from __future__ import annotations

import os
from typing import Any

import anthropic

from app.llm.base import LLMRequest, LLMResponse
from app.llm.errors import TranslationError
from app.llm.retry import RETRYABLE_STATUS, RetryableError, call_with_retry

# Model families that reject sampling parameters such as temperature.
NO_SAMPLING_MODEL_PREFIXES = ("claude-sonnet-5", "claude-opus-5")

_OVERLOADED_STATUS = 529


def model_accepts_sampling(model: str) -> bool:
    """False for model ids that do not accept a temperature parameter."""
    return not model.startswith(NO_SAMPLING_MODEL_PREFIXES)


def _convert_part(part: Any) -> Any:
    if isinstance(part, dict) and part.get("type") == "image":
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": part["media_type"],
                "data": part["data"],
            },
        }
    return part


def _system_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))


class AnthropicProvider:
    """LLMProvider backed by the Anthropic Messages API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 60.0,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        if client is None:
            key = api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise TranslationError("ANTHROPIC_API_KEY is not set")
            client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=0)
        self._client = client

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Send one Messages request under the shared retry policy."""
        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message["role"] == "system":
                system_parts.append(_system_text(message["content"]))
                continue
            content = message["content"]
            if isinstance(content, list):
                content = [_convert_part(part) for part in content]
            messages.append({"role": message["role"], "content": content})

        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
        }
        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)
        if request.response_format and request.response_format.get("type") == "json_schema":
            # Structured outputs: the answer is guaranteed to be one object
            # matching the schema (no fence, no second "corrected" object).
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": request.response_format["schema"]}
            }
        if request.temperature is not None and model_accepts_sampling(request.model):
            # The pinned SDK no longer exposes sampling parameters on
            # messages.create(), so temperature travels in the raw request body.
            kwargs["extra_body"] = {"temperature": request.temperature}

        response = call_with_retry(lambda: self._create(kwargs), label="anthropic")
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return LLMResponse(
            text=text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            finish_reason=response.stop_reason,
        )

    def _create(self, kwargs: dict[str, Any]) -> Any:
        """One API call, mapping SDK errors to RetryableError / TranslationError."""
        try:
            return self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as error:
            raise RetryableError("anthropic rate limited") from error
        except anthropic.InternalServerError as error:
            raise RetryableError("anthropic server error") from error
        except anthropic.APIConnectionError as error:
            raise RetryableError("anthropic connection error") from error
        except anthropic.APIStatusError as error:
            status = error.status_code
            if status in RETRYABLE_STATUS or status == _OVERLOADED_STATUS:
                raise RetryableError(f"anthropic returned {status}") from error
            raise TranslationError(f"anthropic returned {status}") from error
        except anthropic.APIError as error:
            raise TranslationError(f"anthropic request failed: {type(error).__name__}") from error
