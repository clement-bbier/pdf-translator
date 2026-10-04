"""Tracing wrapper around an LLMProvider: one JSONL line per call.

The trace file is the ONLY place where call content (the model output) is
written. It is local-only and gitignored (``traces/``). Input messages are not
stored (``input_id`` identifies them) and image data never reaches the trace.
Nothing from a request or response goes to the application logger.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import ModelPrice
from app.llm.base import LLMProvider, LLMRequest, LLMResponse, request_fingerprint

logger = logging.getLogger(__name__)

_WRITE_LOCK = threading.Lock()  # web jobs run in background threads


class TracingProvider:
    """Append one JSON line per call to ``path``, then return or re-raise."""

    def __init__(self, inner: LLMProvider, path: str | Path, prices: dict[str, ModelPrice]) -> None:
        self._inner = inner
        self._path = Path(path)
        self._prices = prices

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Delegate to the inner provider and trace the outcome."""
        started = time.perf_counter()
        try:
            response = self._inner.complete(request)
        except Exception as error:
            duration_ms = int((time.perf_counter() - started) * 1000)
            self._write(self._record(request, None, duration_ms, error))
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        self._write(self._record(request, response, duration_ms, None))
        return response

    def _cost(self, response: LLMResponse | None) -> float | None:
        if response is None:
            return None
        if response.cache_hit:
            return 0.0
        price = self._prices.get(response.model)
        if price is None:
            return None
        cost = (
            response.input_tokens * price.input + response.output_tokens * price.output
        ) / 1_000_000
        return round(cost, 6)

    def _record(
        self,
        request: LLMRequest,
        response: LLMResponse | None,
        duration_ms: int,
        error: BaseException | None,
    ) -> dict[str, Any]:
        ok = response is not None
        return {
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "input_id": request_fingerprint(request),
            "prompt_version": request.prompt_version,
            "model": response.model if response is not None else request.model,
            "params": {"temperature": request.temperature, "max_tokens": request.max_tokens},
            "output": response.text if response is not None else None,
            "duration_ms": duration_ms,
            "input_tokens": response.input_tokens if response is not None else 0,
            "output_tokens": response.output_tokens if response is not None else 0,
            "estimated_cost_usd": self._cost(response),
            "cache_hit": response.cache_hit if response is not None else False,
            "status": "ok" if ok else "error",
            "error_type": type(error).__name__ if error is not None else None,
        }

    def _write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        try:
            with _WRITE_LOCK:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError as error:
            logger.warning("llm trace write failed: %s", type(error).__name__)
