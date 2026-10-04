"""Loads config.yaml and caches it for the process lifetime."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.rag.settings import RagSettings

# .env is loaded once, at import time. Every module reaches configuration
# through app.config, so environment variables are available process-wide
# without each module having to load the file itself. Values already present
# in the real environment win: load_dotenv does not override them.
load_dotenv()

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

REPO_ROOT = DEFAULT_CONFIG_PATH.parent

PROVIDER_ENV = "APP_PROVIDER"

_cache: dict[str, Any] | None = None


class ConfigError(ValueError):
    """config.yaml holds an invalid value."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelPrice(_Strict):
    """USD per million tokens."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)


class CompareModel(_Strict):
    """A model to compare against the main one (evaluation only)."""

    model: str = Field(min_length=1)
    temperature: float | None = None  # None = not sent


class LLMSettings(_Strict):
    """The pinned LLM call parameters."""

    model: str = Field(default="claude-haiku-4-5-20251001", min_length=1)
    temperature: float | None = Field(default=0, ge=0, le=1)  # None = not sent
    max_tokens: int = Field(default=4096, gt=0)
    timeout: float = Field(default=60, gt=0)
    prompt_version: str = Field(default="v1", pattern=r"^v[0-9]+$")
    compare_models: list[CompareModel] = Field(
        default_factory=lambda: [CompareModel(model="claude-sonnet-5-5", temperature=None)]
    )
    prices: dict[str, ModelPrice] = Field(
        default_factory=lambda: {
            "claude-haiku-4-5-20251001": ModelPrice(input=1.0, output=5.0),
            "claude-sonnet-5-5": ModelPrice(input=2.0, output=10.0),
        }
    )


class VerificationSettings(_Strict):
    """Output verification; the runtime reads it through app.verification.settings."""

    enabled: bool = False
    max_retries: int = Field(default=1, ge=0, le=3)


class TracingSettings(_Strict):
    """JSONL trace of every LLM call.

    Stores ids, counters, cost and the model output (translated text). Local
    only and gitignored; input messages are not stored.
    """

    enabled: bool = True
    path: str = "traces/llm-calls.jsonl"


class CacheSettings(_Strict):
    """Request-level LLM response cache."""

    enabled: bool = True
    path: str = "tmp/llm-cache.json"


_SECTIONS: dict[str, type[BaseModel]] = {
    "llm": LLMSettings,
    "rag": RagSettings,
    "verification": VerificationSettings,
    "tracing": TracingSettings,
    "cache": CacheSettings,
}


def resolve_path(path: str | Path) -> Path:
    """Resolve a relative path against the repo root."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else REPO_ROOT / candidate


def _section(config: dict[str, Any], name: str) -> Any:
    """Validate config[name] (defaults when absent); raise ConfigError on bad input."""
    raw = config.get(name)
    if raw is None:
        raw = {}
    try:
        return _SECTIONS[name].model_validate(raw)
    except ValidationError as error:
        first = error.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "<section>"
        raise ConfigError(
            f"config.yaml: invalid '{name}' section: {location}: {first['msg']}"
        ) from error


def llm_settings(config: dict[str, Any]) -> LLMSettings:
    """Validated `llm` section."""
    return _section(config, "llm")


def rag_settings(config: dict[str, Any]) -> RagSettings:
    """Validated `rag` section (the model lives in app.rag.settings)."""
    return _section(config, "rag")


def verification_settings(config: dict[str, Any]) -> VerificationSettings:
    """Validated `verification` section."""
    return _section(config, "verification")


def tracing_settings(config: dict[str, Any]) -> TracingSettings:
    """Validated `tracing` section, path resolved against the repo root."""
    settings = _section(config, "tracing")
    return settings.model_copy(update={"path": str(resolve_path(settings.path))})


def cache_settings(config: dict[str, Any]) -> CacheSettings:
    """Validated `cache` section, path resolved against the repo root."""
    settings = _section(config, "cache")
    return settings.model_copy(update={"path": str(resolve_path(settings.path))})


def validate_config(config: dict[str, Any]) -> None:
    """Validate every typed section; raises ConfigError."""
    for name in _SECTIONS:
        _section(config, name)


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """APP_PROVIDER (when non-empty) overrides config.yaml's provider."""
    provider = os.environ.get(PROVIDER_ENV, "").strip()
    if provider:
        data["provider"] = provider
    return data


def load_config(path: str | Path | None = None, *, reload: bool = False) -> dict[str, Any]:
    """Return the parsed config.yaml (validated, still a plain dict), cached after first call."""
    global _cache
    if _cache is not None and not reload and path is None:
        return _cache

    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    data = _apply_env_overrides(data)

    validate_config(data)

    if path is None:
        _cache = data
    return data


def get(key: str, default: Any = None) -> Any:
    """Return a single top-level config value."""
    return load_config().get(key, default)


def force_utf8_stdout() -> None:
    """Force UTF-8 on stdout: Windows consoles default to cp1252 and crash on CJK."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):  # pragma: no cover - non-standard stream
        pass
