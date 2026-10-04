"""Validated `rag` section of config.yaml (glossary and translation memory)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]


class RagConfigError(ValueError):
    """Raised when the `rag` section of the config is invalid."""


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


class RagSettings(BaseModel):
    """Retrieval switches and parameters. Booleans are strict ('yes' is rejected)."""

    model_config = ConfigDict(extra="forbid")

    glossary: StrictBool = True
    glossary_path: str = Field(default="data/glossary/finance_zh.csv", min_length=1)
    translation_memory: StrictBool = False
    tm_path: str = Field(default="data/tm/seed_zh_en.jsonl", min_length=1)
    tm_top_k: int = Field(default=3, ge=1, le=20)
    tm_min_score: float = Field(default=0.3, ge=-1.0, le=1.0)
    embedder: Literal["hashing", "gateway"] = "hashing"
    embedding_model: str = Field(default="text-embedding-3-small", min_length=1)

    @property
    def glossary_file(self) -> Path:
        """Glossary CSV path; relative paths resolve against the repo root."""
        return _resolve(self.glossary_path)

    @property
    def tm_file(self) -> Path:
        """Translation memory JSONL path; relative paths resolve against the repo root."""
        return _resolve(self.tm_path)


def rag_settings(config: dict[str, Any]) -> RagSettings:
    """Validate config['rag']; a missing or empty section gives the defaults."""
    section = config.get("rag")
    if section is None:
        return RagSettings()
    try:
        return RagSettings.model_validate(section)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'rag'}: {item['msg']}"
            for item in error.errors()
        )
        raise RagConfigError(f"config.yaml: invalid 'rag' section: {details}") from None
