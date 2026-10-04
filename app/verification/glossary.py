"""Glossary check: required target terms must appear in the translation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol, runtime_checkable

from app.verification.findings import Finding


@runtime_checkable
class TermProvider(Protocol):
    """Optional method of context providers, next to context_for()."""

    def required_terms(self, segment: str, src: str, tgt: str) -> list[str]:
        """Return the target terms injected for this segment."""
        ...


def collect_required_terms(
    providers: Iterable[object], segments: Mapping[str, str], src: str, tgt: str
) -> dict[str, list[str]]:
    """Merge the required terms of every provider that exposes required_terms."""
    merged: dict[str, list[str]] = {}
    for provider in providers:
        method = getattr(provider, "required_terms", None)
        if not callable(method):
            continue
        for key, segment in segments.items():
            for term in method(segment, src, tgt):
                bucket = merged.setdefault(key, [])
                if term not in bucket:
                    bucket.append(term)
    return {key: terms for key, terms in merged.items() if terms}


def _normalise(text: str) -> str:
    return " ".join(text.split()).casefold()


def check_glossary(key: str, translation: str, terms: Sequence[str]) -> list[Finding]:
    """One finding per required term absent from the translation."""
    haystack = _normalise(translation)
    return [
        Finding("glossary_terms", key, f"missing glossary term: {term}")
        for term in terms
        if _normalise(term) not in haystack
    ]
