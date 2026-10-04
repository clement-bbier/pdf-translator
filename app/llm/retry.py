"""Shared retry/backoff policy for transient provider errors."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from app.llm.errors import TranslationError

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

T = TypeVar("T")


class RetryableError(Exception):
    """Internal signal: the call failed with a transient error."""


def call_with_retry(fn: Callable[[], T], *, label: str) -> T:
    """Run ``fn``, retrying RetryableError with exponential backoff."""
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except RetryableError as error:
            last_error = error
            if attempt < MAX_RETRIES - 1:
                delay = BACKOFF_BASE_SECONDS * (2**attempt)
                logger.warning(
                    "%s call failed (attempt %d/%d), retrying in %.1fs",
                    label,
                    attempt + 1,
                    MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)

    raise TranslationError(f"{label} call failed after {MAX_RETRIES} attempts") from last_error
