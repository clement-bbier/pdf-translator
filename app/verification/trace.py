"""Content-free verification trace: one JSON line per verified batch.

Lines carry check names, flags and counters only (no source text, no
translation, no finding message). ``input_ids`` join a line to the per-call
lines of the LLM tracing provider, which hold the outputs.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

from app.verification.policy import BatchVerification

logger = logging.getLogger(__name__)


class VerificationTrace:
    """Append-only JSONL writer, safe across the web app's job threads."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def record(self, batch: BatchVerification, *, model: str, prompt_version: str) -> None:
        """Append one line for a batch. An I/O error never fails the translation."""
        line = {
            "timestamp": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "kind": "verification",
            "model": model,
            "prompt_version": prompt_version,
            "input_ids": list(batch.input_ids),
            "attempts": batch.attempts,
            "segments": [
                {
                    "key": outcome.key,
                    "passed": list(outcome.passed),
                    "failed": list(outcome.failed),
                    "retried": outcome.retried,
                    "flagged": outcome.flagged,
                }
                for outcome in batch.segments
            ],
        }
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as error:
            logger.warning("verification trace write failed: %s", type(error).__name__)
