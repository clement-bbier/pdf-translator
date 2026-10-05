"""Verification settings accessor.

To be replaced by app.config.verification_settings at the Phase 1 merge.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_FIELDS = ("enabled", "max_retries")


@dataclass(frozen=True, slots=True)
class VerificationConfig:
    """Verification options (same attributes as Phase 1 VerificationSettings)."""

    enabled: bool = False
    max_retries: int = 1


def _invalid(field: str, reason: str) -> ValueError:
    return ValueError(f"config.yaml: invalid 'verification' section: {field}: {reason}")


def verification_config(config: Mapping[str, Any]) -> VerificationConfig:
    """Read and validate config['verification'] (a missing section gives defaults)."""
    section = config.get("verification") or {}
    if not isinstance(section, Mapping):
        raise _invalid("verification", "must be a mapping")
    for name in section:
        if name not in _FIELDS:
            raise _invalid(str(name), "unknown field")
    defaults = VerificationConfig()
    enabled = section.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        raise _invalid("enabled", "must be a boolean")
    max_retries = section.get("max_retries", defaults.max_retries)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        raise _invalid("max_retries", "must be an integer")
    if not 0 <= max_retries <= 3:
        raise _invalid("max_retries", "must be between 0 and 3")
    return VerificationConfig(enabled=enabled, max_retries=max_retries)
