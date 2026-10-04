"""Translator interface and its Mock / LLM-backed implementations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.errors import TranslationError
from app.llm.fake_provider import FakeProvider
from app.llm.gateway_provider import GatewayProvider
from app.llm.translator import LLMTranslator

__all__ = [
    "GatewayTranslator",
    "JobStats",
    "MockTranslator",
    "TranslationError",
    "Translator",
    "get_translator",
]


class Translator(Protocol):
    """Translates a batch of text blocks from a source to a target language."""

    def translate_blocks(self, blocks: list[str], src: str, tgt: str) -> list[str]:
        """Return one translation per input block, same order, same length."""
        ...

    def translate_page_image(self, image_bytes: bytes, src: str, tgt: str) -> str:
        """Return the translation of a whole page sent as a PNG image, as plain text.

        Used for scanned pages, where no text can be extracted and the
        block-by-block path does not apply.
        """
        ...


class MockTranslator:
    """Deterministic, visually identifiable translator for testing without network.

    Output is "[tgt] " + the original text, then padded with repeated words so the
    result is roughly 1.4x the original length — this exercises the rebuilder's
    layout handling (font shrink / truncation) under realistic growth.
    """

    GROWTH_FACTOR = 1.4

    def translate_blocks(self, blocks: list[str], src: str, tgt: str) -> list[str]:
        """Return a deterministic, length-inflated translation for each block."""
        return [self._translate_one(text, tgt) for text in blocks]

    def translate_page_image(self, image_bytes: bytes, src: str, tgt: str) -> str:
        """Return a deterministic multi-paragraph stand-in for a translated scan.

        Keyed on the image size so the same page always yields the same text.
        Multi-paragraph on purpose: it exercises the scanned-page rebuilder's
        paragraph splitting and wrapping without needing a real model.
        """
        if not image_bytes:
            raise TranslationError("page image is empty")

        paragraphs = [
            f"[{tgt}] Scanned page translated from {src} ({len(image_bytes)} bytes of image data).",
            f"[{tgt}] This is the second paragraph of the mock page "
            "translation, long enough to wrap across several lines when it is "
            "written back into a rebuilt page.",
            f"[{tgt}] Third and final paragraph of the mock translation.",
        ]
        return "\n\n".join(paragraphs)

    def _translate_one(self, text: str, tgt: str) -> str:
        prefix = f"[{tgt}] "
        base = prefix + text
        target_length = int(len(text) * self.GROWTH_FACTOR) + len(prefix)

        words = text.split() or [text]
        grown = base
        index = 0
        while len(grown) < target_length:
            grown += " " + words[index % len(words)]
            index += 1
        return grown


class GatewayTranslator(LLMTranslator):
    """Compatibility alias: an LLMTranslator over an OpenAI-compatible LLM gateway."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 60.0,
        temperature: float | None = None,
    ) -> None:
        super().__init__(
            GatewayProvider(base_url, timeout=timeout),
            model=model,
            temperature=temperature,
        )


def get_translator(config: dict[str, Any]) -> Translator:
    """Build the Translator described by config.yaml's `provider` key."""
    provider = config.get("provider", "mock")
    if provider == "mock":
        return MockTranslator()
    if provider == "fake":
        return LLMTranslator(FakeProvider(), model=FakeProvider.MODEL_ID)
    if provider == "anthropic":
        llm = config.get("llm", {})
        return LLMTranslator(
            AnthropicProvider(),
            model=llm.get("model", "claude-haiku-4-5-20251001"),
            temperature=llm.get("temperature", 0),
        )
    if provider == "gateway":
        base_url = os.environ.get("GATEWAY_BASE_URL")
        if not base_url:
            raise TranslationError("GATEWAY_BASE_URL is not set")
        return GatewayTranslator(base_url=base_url, model=config.get("model", ""))
    raise ValueError(f"unknown translator provider: {provider!r}")


@dataclass(slots=True)
class JobStats:
    """Counters for one translation job. Never holds document content."""

    total_blocks: int = 0
    unique_blocks: int = 0
    cache_hits: int = 0
    api_calls: int = 0
    estimated_tokens: int = 0

    def record_blocks(self, texts: list[str]) -> None:
        """Register the total and unique block counts for a batch of texts."""
        self.total_blocks += len(texts)
        self.unique_blocks += len(set(texts))

    def record_cache_hits(self, count: int) -> None:
        """Register cache hits, avoiding a real translation call for them."""
        self.cache_hits += count

    def record_api_call(self, texts: list[str]) -> None:
        """Register one API call and estimate its token cost (len // 4)."""
        self.api_calls += 1
        self.estimated_tokens += sum(len(text) // 4 for text in texts)

    def summary(self) -> str:
        """One-line human-readable summary."""
        dedup_pct = 100 * (1 - self.unique_blocks / self.total_blocks) if self.total_blocks else 0
        return (
            f"blocks={self.total_blocks} unique={self.unique_blocks} "
            f"(dedup {dedup_pct:.0f}%) cache_hits={self.cache_hits} "
            f"api_calls={self.api_calls} estimated_tokens={self.estimated_tokens}"
        )
