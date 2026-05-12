"""Built-in provider registry for browser launch and session adapters.

This is intentionally internal-only. It centralizes provider metadata, launch
command construction, and session adapter hooks so future built-in providers do
not have to accrete inside `browse.py` or `core.py`.
"""

from __future__ import annotations

import importlib
import os
import sys
from functools import cache

from .base import ProviderSpec
from .claude import PROVIDER as CLAUDE_PROVIDER
from .codex import PROVIDER as CODEX_PROVIDER
from .copilot import PROVIDER as COPILOT_PROVIDER
from .cursor import PROVIDER as CURSOR_PROVIDER
from .gemini import PROVIDER as GEMINI_PROVIDER

_BUILTIN_PROVIDERS: dict[str, ProviderSpec] = {
    "claude": CLAUDE_PROVIDER,
    "codex": CODEX_PROVIDER,
    "gemini": GEMINI_PROVIDER,
    "copilot": COPILOT_PROVIDER,
    "cursor": CURSOR_PROVIDER,
}


def _warn(message: str) -> None:
    print(f"claude-browse: {message}", file=sys.stderr)


def _external_module_names(raw_modules: str) -> tuple[str, ...]:
    names: list[str] = []
    for chunk in raw_modules.split(","):
        name = chunk.strip()
        if name:
            names.append(name)
    return tuple(names)


@cache
def _load_external_providers_cached(raw_modules: str) -> tuple[ProviderSpec, ...]:
    specs: list[ProviderSpec] = []
    seen_ids = set(_BUILTIN_PROVIDERS)
    for module_name in _external_module_names(raw_modules):
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            _warn(f"skipping external provider module {module_name!r}: {exc}")
            continue

        spec = getattr(module, "PROVIDER", None)
        if not isinstance(spec, ProviderSpec):
            _warn(
                f"skipping external provider module {module_name!r}: "
                "missing PROVIDER = ProviderSpec(...)"
            )
            continue

        provider_id = spec.provider_id.lower()
        if provider_id in seen_ids:
            _warn(
                f"skipping external provider module {module_name!r}: "
                f"provider_id {provider_id!r} duplicates an existing provider"
            )
            continue

        specs.append(spec)
        seen_ids.add(provider_id)
    return tuple(specs)


def _all_providers() -> dict[str, ProviderSpec]:
    providers = dict(_BUILTIN_PROVIDERS)
    raw_modules = os.environ.get("CLAUDE_BROWSE_PROVIDER_MODULES", "").strip()
    for spec in _load_external_providers_cached(raw_modules):
        providers[spec.provider_id.lower()] = spec
    return providers


def get_provider(provider: str | None) -> ProviderSpec:
    key = (provider or "claude").lower()
    providers = _all_providers()
    if key not in providers:
        raise ValueError(f"Unknown provider: {provider}")
    return providers[key]


def provider_ids(
    *,
    source_capable: bool | None = None,
    target_capable: bool | None = None,
) -> tuple[str, ...]:
    ids: list[str] = []
    for provider_id, spec in _all_providers().items():
        if source_capable is not None and spec.source_capable != source_capable:
            continue
        if target_capable is not None and spec.target_capable != target_capable:
            continue
        ids.append(provider_id)
    return tuple(ids)


__all__ = [
    "ProviderSpec",
    "get_provider",
    "provider_ids",
]
