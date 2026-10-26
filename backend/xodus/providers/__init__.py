"""Model providers. build_provider() picks the adapter named in config ([provider] / provider = ...)."""

from __future__ import annotations

from typing import Any

from xodus.config.settings import Settings
from xodus.providers.base import ModelProvider


def build_provider(settings: Settings, **kwargs: Any) -> ModelProvider:
    if settings.provider == "ollama":
        from xodus.providers.ollama import OllamaProvider

        return OllamaProvider(settings.ollama, embedding=settings.embedding, **kwargs)
    raise ValueError(f"unknown provider {settings.provider!r}")  # unreachable: config validation restricts it
