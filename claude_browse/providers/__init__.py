"""Built-in provider registry for browser launch and session adapters.

This is intentionally internal-only. It centralizes provider metadata, launch
command construction, and session adapter hooks so future built-in providers do
not have to accrete inside `browse.py` or `core.py`.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from dataclasses import dataclass
from functools import cache

from .base import PROVIDER_API_VERSION, ProviderSpec
from .claude import PROVIDER as CLAUDE_PROVIDER
from .codex import PROVIDER as CODEX_PROVIDER
from .copilot import PROVIDER as COPILOT_PROVIDER
from .cursor import PROVIDER as CURSOR_PROVIDER
from .gemini import PROVIDER as GEMINI_PROVIDER


@dataclass(frozen=True)
class ProviderEntry:
    spec: ProviderSpec
    source_type: str
    origin: str


_BUILTIN_PROVIDERS: dict[str, ProviderEntry] = {
    "claude": ProviderEntry(CLAUDE_PROVIDER, "builtin", "builtin"),
    "codex": ProviderEntry(CODEX_PROVIDER, "builtin", "builtin"),
    "gemini": ProviderEntry(GEMINI_PROVIDER, "builtin", "builtin"),
    "copilot": ProviderEntry(COPILOT_PROVIDER, "builtin", "builtin"),
    "cursor": ProviderEntry(CURSOR_PROVIDER, "builtin", "builtin"),
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


def _external_provider_files(raw_dirs: str) -> tuple[str, ...]:
    files: list[str] = []
    seen: set[str] = set()
    for chunk in raw_dirs.split(":"):
        directory = chunk.strip()
        if not directory or not os.path.isdir(directory):
            continue
        for filename in sorted(os.listdir(directory)):
            if not filename.endswith(".py") or filename == "__init__.py":
                continue
            path = os.path.join(directory, filename)
            if path in seen:
                continue
            seen.add(path)
            files.append(path)
    return tuple(files)


def _validate_external_module(module, origin: str) -> ProviderSpec | None:
    api_version = getattr(module, "PROVIDER_API_VERSION", PROVIDER_API_VERSION)
    if not isinstance(api_version, int) or api_version != PROVIDER_API_VERSION:
        _warn(
            f"skipping external provider {origin}: "
            f"PROVIDER_API_VERSION {api_version!r} != {PROVIDER_API_VERSION}"
        )
        return None

    spec = getattr(module, "PROVIDER", None)
    if not isinstance(spec, ProviderSpec):
        _warn(
            f"skipping external provider {origin}: "
            "missing PROVIDER = ProviderSpec(...)"
        )
        return None

    return spec


def _load_external_provider_from_module(module_name: str) -> ProviderSpec | None:
    origin = f"module:{module_name}"
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        _warn(f"skipping external provider {origin}: {exc}")
        return None
    return _validate_external_module(module, origin)


def _load_external_provider_from_file(file_path: str, index: int) -> ProviderSpec | None:
    origin = f"file:{file_path}"
    module_name = f"claude_browse_external_provider_{index}"
    module_spec = importlib.util.spec_from_file_location(module_name, file_path)
    if module_spec is None or module_spec.loader is None:
        _warn(f"skipping external provider {origin}: could not create module spec")
        return None

    module = importlib.util.module_from_spec(module_spec)
    try:
        module_spec.loader.exec_module(module)
    except Exception as exc:
        _warn(f"skipping external provider {origin}: {exc}")
        return None
    return _validate_external_module(module, origin)


@cache
def _load_external_providers_cached(
    raw_modules: str,
    raw_dirs: str,
) -> tuple[ProviderEntry, ...]:
    entries: list[ProviderEntry] = []
    seen_ids = set(_BUILTIN_PROVIDERS)

    for module_name in _external_module_names(raw_modules):
        spec = _load_external_provider_from_module(module_name)
        if spec is None:
            continue

        provider_id = spec.provider_id.lower()
        if provider_id in seen_ids:
            _warn(
                f"skipping external provider module:{module_name}: "
                f"provider_id {provider_id!r} duplicates an existing provider"
            )
            continue

        entries.append(ProviderEntry(spec, "module", f"module:{module_name}"))
        seen_ids.add(provider_id)

    for index, file_path in enumerate(_external_provider_files(raw_dirs), 1):
        spec = _load_external_provider_from_file(file_path, index)
        if spec is None:
            continue

        provider_id = spec.provider_id.lower()
        if provider_id in seen_ids:
            _warn(
                f"skipping external provider file:{file_path}: "
                f"provider_id {provider_id!r} duplicates an existing provider"
            )
            continue

        entries.append(ProviderEntry(spec, "file", f"file:{file_path}"))
        seen_ids.add(provider_id)

    return tuple(entries)


def _all_provider_entries() -> dict[str, ProviderEntry]:
    providers = dict(_BUILTIN_PROVIDERS)
    raw_modules = os.environ.get("CLAUDE_BROWSE_PROVIDER_MODULES", "").strip()
    raw_dirs = os.environ.get("CLAUDE_BROWSE_PROVIDER_DIRS", "").strip()
    for entry in _load_external_providers_cached(raw_modules, raw_dirs):
        providers[entry.spec.provider_id.lower()] = entry
    return providers


def get_provider(provider: str | None) -> ProviderSpec:
    key = (provider or "claude").lower()
    providers = _all_provider_entries()
    if key not in providers:
        raise ValueError(f"Unknown provider: {provider}")
    return providers[key].spec


def provider_entries(
    *,
    source_capable: bool | None = None,
    target_capable: bool | None = None,
) -> tuple[ProviderEntry, ...]:
    entries: list[ProviderEntry] = []
    for entry in _all_provider_entries().values():
        spec = entry.spec
        if source_capable is not None and spec.source_capable != source_capable:
            continue
        if target_capable is not None and spec.target_capable != target_capable:
            continue
        entries.append(entry)
    return tuple(entries)


def provider_ids(
    *,
    source_capable: bool | None = None,
    target_capable: bool | None = None,
) -> tuple[str, ...]:
    return tuple(
        entry.spec.provider_id.lower()
        for entry in provider_entries(
            source_capable=source_capable,
            target_capable=target_capable,
        )
    )


__all__ = [
    "PROVIDER_API_VERSION",
    "ProviderEntry",
    "ProviderSpec",
    "get_provider",
    "provider_entries",
    "provider_ids",
]
