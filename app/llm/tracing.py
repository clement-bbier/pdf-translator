"""Tracing wrapper around an LLMProvider (filled in by F4)."""

from __future__ import annotations

from pathlib import Path

from app.config import ModelPrice
from app.llm.base import LLMProvider, LLMRequest, LLMResponse


class TracingProvider:
    """Will append one JSONL line per call; pass-through until F4/F5."""

    def __init__(self, inner: LLMProvider, path: str | Path, prices: dict[str, ModelPrice]) -> None:
        self._inner = inner
        self._path = Path(path)
        self._prices = prices

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Pass-through until F4/F5."""
        return self._inner.complete(request)
