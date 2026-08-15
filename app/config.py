"""Loads config.yaml and caches it for the process lifetime."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# .env is loaded once, at import time. Every module reaches configuration
# through app.config, so environment variables are available process-wide
# without each module having to load the file itself. Values already present
# in the real environment win: load_dotenv does not override them.
load_dotenv()

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

_cache: dict[str, Any] | None = None


def load_config(path: str | Path | None = None, *, reload: bool = False) -> dict[str, Any]:
    """Return the parsed config.yaml, cached after the first call."""
    global _cache
    if _cache is not None and not reload and path is None:
        return _cache

    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

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
