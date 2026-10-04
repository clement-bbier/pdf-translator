"""Builds the provider stack (base provider, then cache, then tracing) from config."""

from __future__ import annotations

import os
from typing import Any

from app import config as app_config
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMProvider
from app.llm.caching import CachedProvider
from app.llm.errors import TranslationError
from app.llm.fake_provider import FakeProvider
from app.llm.gateway_provider import GatewayProvider
from app.llm.tracing import TracingProvider


def build_provider(name: str, config: dict[str, Any]) -> LLMProvider:
    """Build the base provider `name`, wrapped by the cache then by tracing when enabled."""
    llm = app_config.llm_settings(config)
    provider: LLMProvider
    if name == "fake":
        provider = FakeProvider()
    elif name == "anthropic":
        provider = AnthropicProvider(timeout=llm.timeout)
    elif name == "gateway":
        base_url = os.environ.get("GATEWAY_BASE_URL")
        if not base_url:
            raise TranslationError("GATEWAY_BASE_URL is not set")
        provider = GatewayProvider(base_url, timeout=llm.timeout)
    else:
        raise ValueError(f"unknown LLM provider: {name!r}")

    cache = app_config.cache_settings(config)
    if cache.enabled:
        provider = CachedProvider(provider, cache.path)
    tracing = app_config.tracing_settings(config)
    if tracing.enabled:
        provider = TracingProvider(provider, tracing.path, llm.prices)
    return provider
