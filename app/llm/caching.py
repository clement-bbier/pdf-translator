"""Request-level response cache around an LLMProvider."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from app.llm.base import LLMProvider, LLMRequest, LLMResponse, request_fingerprint

logger = logging.getLogger(__name__)


class CachedProvider:
    """Caches responses by request fingerprint, persisted as JSON at ``path``.

    Hits never call the inner provider; failures are never cached.
    """

    def __init__(self, inner: LLMProvider, path: str | Path) -> None:
        self._inner = inner
        self._path = Path(path)
        self._data: dict[str, dict] | None = None
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict]:
        """Load the cache file once; a corrupt or unreadable file starts empty."""
        if self._data is not None:
            return self._data
        data: dict[str, dict] = {}
        if self._path.is_file():
            try:
                with self._path.open("r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if not isinstance(loaded, dict):
                    raise ValueError("not a mapping")
                data = loaded
            except (OSError, ValueError) as error:
                logger.warning(
                    "llm cache: failed to load %s (%s), starting empty",
                    self._path.name,
                    type(error).__name__,
                )
                data = {}
        self._data = data
        return data

    def _save(self, data: dict[str, dict]) -> None:
        """Atomic write: <path>.tmp then os.replace."""
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(tmp_path, self._path)
        except OSError as error:
            logger.warning("llm cache: failed to persist (%s)", type(error).__name__)

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Return the cached response or call the inner provider and store its answer."""
        key = request_fingerprint(request)
        with self._lock:
            entry = self._load().get(key)
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                return LLMResponse(
                    text=entry["text"],
                    model=str(entry.get("model", request.model)),
                    input_tokens=int(entry.get("input_tokens", 0)),
                    output_tokens=int(entry.get("output_tokens", 0)),
                    finish_reason=entry.get("finish_reason"),
                    cache_hit=True,
                )
        response = self._inner.complete(request)
        with self._lock:
            data = self._load()
            data[key] = {
                "text": response.text,
                "model": response.model,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "finish_reason": response.finish_reason,
            }
            self._save(data)
        return response
