"""Request-level response cache around an LLMProvider (filled in by F5)."""

from __future__ import annotations

from pathlib import Path

from app.llm.base import LLMProvider, LLMRequest, LLMResponse


class CachedProvider:
    """Will cache responses by request fingerprint; pass-through until F4/F5."""

    def __init__(self, inner: LLMProvider, path: str | Path) -> None:
        self._inner = inner
        self._path = Path(path)

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Pass-through until F4/F5."""
        return self._inner.complete(request)
