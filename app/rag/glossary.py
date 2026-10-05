"""Exact-match finance glossary and its context provider.

The glossary is a CSV (source, target_fr, target_en, domain). Matching is a
deterministic longest-first scan: no embeddings, no fuzziness. Only counts are
ever logged, never document text.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

logger = logging.getLogger(__name__)

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

HEADER = "Required terminology (use these target terms exactly):"


class GlossaryRow(BaseModel):
    """One validated glossary CSV row."""

    model_config = ConfigDict(extra="forbid")

    source: _Text
    target_fr: _Text
    target_en: _Text
    domain: _Text


@dataclass(frozen=True)
class GlossaryMatch:
    """A glossary term found in a text (start inclusive, end exclusive)."""

    source: str
    target: str
    start: int
    end: int


class Glossary:
    """Source term -> target terms, searched longest match first."""

    def __init__(self, rows: list[GlossaryRow]) -> None:
        self._rows = {row.source: row for row in rows}
        # Longest terms first so that a longer term wins over a contained one.
        self._terms = sorted(self._rows, key=lambda term: (-len(term), term))

    @classmethod
    def load(cls, path: str | Path) -> Glossary:
        """Read and validate a glossary CSV (utf-8, BOM accepted)."""
        rows: list[GlossaryRow] = []
        seen: set[str] = set()
        with open(path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for record in reader:
                line = reader.line_num
                try:
                    row = GlossaryRow(**{k: v for k, v in record.items() if k is not None})
                except Exception as error:  # noqa: BLE001 - re-raised with the line only
                    raise ValueError(f"glossary line {line}: invalid row") from error
                if row.source in seen:
                    raise ValueError(f"glossary line {line}: duplicate source {row.source!r}")
                seen.add(row.source)
                rows.append(row)
        logger.info("glossary loaded: %d terms", len(rows))
        return cls(rows)

    def __len__(self) -> int:
        return len(self._rows)

    def find(self, text: str, tgt: str) -> list[GlossaryMatch]:
        """Non-overlapping matches in document order, longest term first."""
        if tgt not in ("fr", "en"):
            return []
        attribute = "target_fr" if tgt == "fr" else "target_en"
        matches: list[GlossaryMatch] = []
        position = 0
        while position < len(text):
            for term in self._terms:
                if text.startswith(term, position):
                    end = position + len(term)
                    target = getattr(self._rows[term], attribute)
                    matches.append(GlossaryMatch(term, target, position, end))
                    position = end
                    break
            else:
                position += 1
        return matches


class GlossaryContextProvider:
    """Renders the glossary terms found in a batch as a prompt block."""

    name = "glossary"

    def __init__(self, glossary: Glossary, src_languages: tuple[str, ...] = ("zh",)) -> None:
        self.glossary = glossary
        self.src_languages = src_languages

    def render(self, segments: list[str], src: str, tgt: str) -> tuple[str, int]:
        """Return (block text, number of distinct terms injected)."""
        if src not in self.src_languages:
            return "", 0
        lines: dict[str, None] = {}
        for segment in segments:
            for match in self.glossary.find(segment, tgt):
                lines.setdefault(f"- {match.source} => {match.target}", None)
        if not lines:
            return "", 0
        return HEADER + "\n" + "\n".join(lines), len(lines)

    def context_for(self, segments: list[str], src: str, tgt: str) -> str:
        """Context block only (the ContextProvider protocol)."""
        return self.render(segments, src, tgt)[0]

    def required_terms(self, segments: list[str], src: str, tgt: str) -> dict[str, list[str]]:
        """Map str(segment index) to the target terms expected in its translation."""
        if src not in self.src_languages:
            return {}
        result: dict[str, list[str]] = {}
        for index, segment in enumerate(segments):
            terms = [match.target for match in self.glossary.find(segment, tgt)]
            if terms:
                result[str(index)] = terms
        return result
