"""Combine the enabled RAG providers into one prompt context block.

RagContext holds no state between calls, so one instance can serve concurrent
web jobs. Sources record provider names and counts only, never content.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.llm.errors import TranslationError
from app.rag.glossary import Glossary, GlossaryContextProvider
from app.rag.memory import (
    Embedder,
    GatewayEmbedder,
    HashingEmbedder,
    TranslationMemory,
    TranslationMemoryContextProvider,
)
from app.rag.settings import rag_settings


@dataclass(frozen=True, slots=True)
class RenderedContext:
    """Result of one render: prompt text, per-provider counts, required terms."""

    text: str = ""
    sources: dict[str, int] = field(default_factory=dict)
    required_terms: dict[str, list[str]] = field(default_factory=dict)


class RagContext:
    """Composite of context providers exposing ``render`` and ``context_for``."""

    def __init__(self, providers: Sequence[Any]) -> None:
        self._providers = tuple(providers)

    def render(self, segments: list[str], src: str, tgt: str) -> RenderedContext:
        """Render every provider; non-empty texts are joined with a blank line."""
        texts: list[str] = []
        sources: dict[str, int] = {}
        required_terms: dict[str, list[str]] = {}
        for provider in self._providers:
            text, count = provider.render(segments, src, tgt)
            if text:
                texts.append(text)
            if count > 0:
                sources[provider.name] = count
            if hasattr(provider, "required_terms"):
                required_terms.update(provider.required_terms(segments, src, tgt))
        return RenderedContext("\n\n".join(texts), sources, required_terms)

    def context_for(self, segments: list[str], src: str, tgt: str) -> str:
        """ContextProvider protocol: the rendered text only."""
        return self.render(segments, src, tgt).text


def build_rag_context(
    config: dict[str, Any], *, cache_dir: Path | None = None
) -> RagContext | None:
    """Build the RagContext described by config['rag']; None when nothing is enabled."""
    settings = rag_settings(config)
    providers: list[Any] = []
    if settings.glossary:
        providers.append(GlossaryContextProvider(Glossary.load(settings.glossary_file)))
    if settings.translation_memory:
        embedder: Embedder
        if settings.embedder == "gateway":
            base_url = os.environ.get("GATEWAY_BASE_URL")
            if not base_url:
                raise TranslationError("GATEWAY_BASE_URL is not set")
            embedder = GatewayEmbedder(base_url, settings.embedding_model)
        else:
            embedder = HashingEmbedder()
        memory = TranslationMemory(settings.tm_file, embedder, cache_dir=cache_dir)
        providers.append(
            TranslationMemoryContextProvider(memory, settings.tm_top_k, settings.tm_min_score)
        )
    return RagContext(providers) if providers else None
