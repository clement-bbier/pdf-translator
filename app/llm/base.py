"""Provider-neutral request/response types and the LLMProvider protocol.

Message format: ``{"role": "system"|"user"|"assistant", "content": str | list[part]}``
where a part is ``{"type": "text", "text": str}`` or
``{"type": "image", "media_type": "image/png", "data": <base64 str>}``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class LLMRequest:
    """One completion request. ``prompt_version`` is metadata, never sent to an API."""

    messages: list[dict[str, Any]]
    model: str
    temperature: float | None = None  # None = not sent
    max_tokens: int = 4096
    response_format: dict[str, Any] | None = None
    prompt_version: str = field(default="")


@dataclass(slots=True)
class LLMResponse:
    """One completion result."""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str | None = None
    cache_hit: bool = False


class LLMProvider(Protocol):
    """Anything able to answer an LLMRequest."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Return the model answer; raises TranslationError on failure."""
        ...


def request_fingerprint(request: LLMRequest) -> str:
    """Stable sha256 hex digest of everything that determines the answer."""
    payload = {
        "model": request.model,
        "prompt_version": request.prompt_version,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "response_format": request.response_format,
        "messages": request.messages,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
