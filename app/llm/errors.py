"""Errors shared by every LLM provider and translator."""

from __future__ import annotations


class TranslationError(Exception):
    """Raised when a batch cannot be translated after retries or on a hard error."""
