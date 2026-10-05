"""Provider for an OpenAI-compatible LLM gateway (chat/completions)."""

from __future__ import annotations

from typing import Any

import httpx

from app import auth
from app.llm.base import LLMRequest, LLMResponse
from app.llm.errors import TranslationError
from app.llm.retry import RETRYABLE_STATUS, RetryableError, call_with_retry


def _convert_content(content: Any) -> Any:
    """Convert neutral image parts to OpenAI image_url parts; pass the rest through."""
    if not isinstance(content, list):
        return content
    converted: list[Any] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image":
            url = f"data:{part['media_type']};base64,{part['data']}"
            converted.append({"type": "image_url", "image_url": {"url": url}})
        else:
            converted.append(part)
    return converted


class GatewayProvider:
    """LLMProvider backed by an OpenAI-compatible gateway."""

    def __init__(self, base_url: str, *, timeout: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Send one chat/completions request under the shared retry policy."""
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {**message, "content": _convert_content(message["content"])}
                for message in request.messages
            ],
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        # The JSON schema is not forwarded: structured-output support varies
        # across OpenAI-compatible gateways, and the body stays as before; the
        # translator strips a code fence and verification checks the JSON.
        if (
            request.response_format is not None
            and request.response_format.get("type") != "json_schema"
        ):
            body["response_format"] = request.response_format

        response = call_with_retry(lambda: self._post(body), label="gateway")
        return self._parse(response, request.model)

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        """POST once, mapping transient failures to RetryableError."""
        headers = {"Authorization": f"Bearer {auth.get_token()}"}

        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(
                    f"{self._base_url}/chat/completions", json=body, headers=headers
                )
        except httpx.HTTPError as error:
            raise TranslationError(f"gateway request failed: {error}") from error

        if response.status_code in RETRYABLE_STATUS:
            raise RetryableError(f"gateway returned {response.status_code}")
        if response.status_code >= 400:
            raise TranslationError(
                f"gateway returned {response.status_code}: {response.text[:200]}"
            )
        return response

    @staticmethod
    def _parse(response: httpx.Response, model: str) -> LLMResponse:
        try:
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
            usage = data.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or 0)
        except (KeyError, IndexError, ValueError, TypeError, AttributeError) as error:
            raise TranslationError(f"malformed gateway response: {error}") from error

        if not isinstance(content, str):
            raise TranslationError("gateway returned a non-text response")
        return LLMResponse(
            text=content,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=finish_reason,
        )
