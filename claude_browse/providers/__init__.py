"""Built-in provider registry for browser launch and session adapters.

This is intentionally internal-only. It centralizes provider metadata, launch
command construction, and session adapter hooks so future built-in providers do
not have to accrete inside `browse.py` or `core.py`.
"""

from __future__ import annotations

from .base import ProviderSpec
from .claude import PROVIDER as CLAUDE_PROVIDER
from .codex import PROVIDER as CODEX_PROVIDER
from .gemini import PROVIDER as GEMINI_PROVIDER

_PROVIDERS: dict[str, ProviderSpec] = {
    "claude": CLAUDE_PROVIDER,
    "codex": CODEX_PROVIDER,
    "gemini": GEMINI_PROVIDER,
}


def get_provider(provider: str | None) -> ProviderSpec:
    key = (provider or "claude").lower()
    if key not in _PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")
    return _PROVIDERS[key]


def provider_ids() -> tuple[str, ...]:
    return tuple(_PROVIDERS)


__all__ = [
    "ProviderSpec",
    "get_provider",
    "provider_ids",
]
