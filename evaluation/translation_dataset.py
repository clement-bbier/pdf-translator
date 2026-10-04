"""Loaders for the zh->en translation evaluation dataset and the finance glossary.

Read-only: nothing here writes to ``evaluation/dataset/`` or ``data/``.
"""

from __future__ import annotations

import csv
import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = ROOT / "evaluation" / "dataset" / "zh_en_hkex.jsonl"
DEFAULT_GLOSSARY_PATH = ROOT / "data" / "glossary" / "finance_zh.csv"

NonEmptyStr = Annotated[str, Field(min_length=1)]
HttpsUrl = Annotated[str, Field(pattern=r"^https://")]
Page = Annotated[int, Field(ge=1)]


class EvalSegment(BaseModel):
    """One aligned zh/en segment with its provenance."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(pattern=r"^hk-[0-9]{3}$")]
    source: NonEmptyStr
    reference: NonEmptyStr
    domain: NonEmptyStr
    issuer: NonEmptyStr
    doc_title: NonEmptyStr
    date: datetime.date
    source_url_zh: HttpsUrl
    source_url_en: HttpsUrl
    page_zh: Page
    page_en: Page


def load_segments(path: Path = DATASET_PATH, *, expected: int | None = 30) -> list[EvalSegment]:
    """Load and validate the JSONL dataset.

    Raises ValueError (naming the line number) on invalid JSON, a schema error or a
    duplicate id, and when the row count differs from ``expected`` (if not None).
    """
    segments: list[EvalSegment] = []
    seen: set[str] = set()
    with Path(path).open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                segment = EvalSegment.model_validate(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {lineno}: invalid JSON ({exc.msg})") from exc
            except ValidationError as exc:
                raise ValueError(f"line {lineno}: invalid segment ({exc})") from exc
            if segment.id in seen:
                raise ValueError(f"line {lineno}: duplicate id {segment.id}")
            seen.add(segment.id)
            segments.append(segment)
    if expected is not None and len(segments) != expected:
        raise ValueError(f"expected {expected} segments, found {len(segments)}")
    return segments


@dataclass(frozen=True)
class GlossaryEntry:
    """A source term with its French and English targets."""

    source: str
    target_fr: str
    target_en: str
    domain: str

    def target(self, tgt: str) -> str:
        """Return the target term for ``tgt`` ('fr' or 'en')."""
        if tgt == "fr":
            return self.target_fr
        if tgt == "en":
            return self.target_en
        raise ValueError(f"unsupported target language: {tgt!r}")


_GLOSSARY_FIELDS = ("source", "target_fr", "target_en", "domain")


def load_glossary(path: Path = DEFAULT_GLOSSARY_PATH) -> list[GlossaryEntry]:
    """Load the glossary CSV; an empty source or target is a ValueError."""
    entries: list[GlossaryEntry] = []
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or tuple(reader.fieldnames) != _GLOSSARY_FIELDS:
            raise ValueError(f"glossary header must be {','.join(_GLOSSARY_FIELDS)}")
        for row in reader:
            values = {k: (row.get(k) or "").strip() for k in _GLOSSARY_FIELDS}
            if not values["source"] or not values["target_fr"] or not values["target_en"]:
                raise ValueError(f"line {reader.line_num}: empty source or target")
            entries.append(GlossaryEntry(**values))
    return entries
