"""Stable, lightweight project identity for Agent Board work items."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse


def _git(path: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _normalize_origin(origin: str) -> str:
    """Return a host/path identity shared by HTTPS and SSH clone URLs."""
    value = origin.strip().rstrip("/")
    if "://" in value:
        parsed = urlparse(value)
        value = f"{parsed.hostname or ''}/{parsed.path.lstrip('/')}"
    elif "@" in value and ":" in value:
        _user_host, _, path = value.partition(":")
        host = _user_host.rsplit("@", 1)[-1]
        value = f"{host}/{path.lstrip('/')}"
    if value.endswith(".git"):
        value = value[:-4]
    return value.lower()


@lru_cache(maxsize=512)
def resolve_project(cwd: str | None) -> dict[str, str]:
    """Resolve a cwd to a stable key plus this machine's usable root path."""
    if not cwd:
        return {"key": "inbox", "name": "Inbox", "path": ""}

    expanded = os.path.abspath(os.path.expanduser(cwd))
    canonical = os.path.realpath(expanded)
    probe = canonical if os.path.isdir(canonical) else str(Path(canonical).parent)
    git_root = _git(probe, "rev-parse", "--show-toplevel")
    root = os.path.realpath(git_root) if git_root else canonical
    origin = _git(root, "remote", "get-url", "origin") if git_root else None

    if origin:
        normalized = _normalize_origin(origin)
        name = normalized.rsplit("/", 1)[-1] or Path(root).name
        key = f"repo:{normalized}"
    else:
        name = Path(root).name or root
        key = f"path:{root}"
    return {"key": key, "name": name or "Inbox", "path": root}

