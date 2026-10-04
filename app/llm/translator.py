"""Translator built on any LLMProvider."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.llm.base import LLMProvider, LLMRequest
from app.llm.errors import TranslationError
from app.llm.prompts import PromptBuilder

if TYPE_CHECKING:
    from app.rag.context import RagContext


class LLMTranslator:
    """Implements the Translator protocol on top of an LLMProvider."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int = 4096,
        prompt_builder: PromptBuilder | None = None,
        context_provider: RagContext | None = None,
    ) -> None:
        self._provider = provider
        self._context_provider = context_provider
        self.model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._prompts = prompt_builder or PromptBuilder("v1")
        self.prompt_version = self._prompts.version

    def _request(self, messages: list[dict]) -> LLMRequest:
        return LLMRequest(
            messages=messages,
            model=self.model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            prompt_version=self.prompt_version,
        )

    def _invalidate(self, request: LLMRequest) -> None:
        """Ask a caching provider to forget a response we rejected."""
        invalidate = getattr(self._provider, "invalidate", None)
        if invalidate is not None:
            invalidate(request)

    def translate_blocks(self, blocks: list[str], src: str, tgt: str) -> list[str]:
        """Translate a batch of blocks in one call; raises TranslationError on failure."""
        if not blocks:
            return []

        payload_map = {str(index): text for index, text in enumerate(blocks)}
        context_text = ""
        context_sources: dict[str, int] = {}
        if self._context_provider is not None:
            rendered = self._context_provider.render(blocks, src, tgt)
            context_text = rendered.text
            context_sources = dict(rendered.sources)
        request = self._request(
            self._prompts.blocks_messages(payload_map, src, tgt, extra_context=context_text)
        )
        request.context_sources = context_sources
        response = self._provider.complete(request)

        try:
            parsed = json.loads(response.text)
        except ValueError as error:
            self._invalidate(request)
            raise TranslationError(f"malformed model response: {error}") from error
        if not isinstance(parsed, dict):
            self._invalidate(request)
            raise TranslationError("model response JSON is not an object")

        missing = [key for key in payload_map if key not in parsed]
        if missing:
            self._invalidate(request)
            raise TranslationError(
                f"model response missing {len(missing)} of {len(payload_map)} keys"
            )
        return [str(parsed[str(index)]) for index in range(len(blocks))]

    def translate_page_image(self, image_bytes: bytes, src: str, tgt: str) -> str:
        """Translate a scanned page sent as a PNG image; returns plain text."""
        if not image_bytes:
            raise TranslationError("page image is empty")
        response = self._provider.complete(
            self._request(self._prompts.page_image_messages(image_bytes, src, tgt))
        )
        return response.text
