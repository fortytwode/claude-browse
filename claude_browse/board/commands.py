"""Guarded, argv-safe Terminal launches for Agent Board surfaces."""

from __future__ import annotations

import fcntl
import glob
import hashlib
import os
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from claude_browse import fts
from claude_browse.providers import claude as claude_provider
from claude_browse.providers import codex as codex_provider
from claude_browse.providers import get_provider

from . import store

_UNSET = object()
_RAW_PATH_CACHE_TTL_S = 10.0
# Provider discovery returns filenames, not transcript contents. A single
# provider/root snapshot lets a board poll answer many missing IDs without
# re-enumerating the same local history directory for each task.
_raw_path_cache: dict[tuple[str, str], tuple[float, dict[str, tuple[str, ...]]]] = {}
_raw_path_cache_lock = threading.Lock()


def _safe_session_id(session_id: str) -> bool:
    """Accept only plain provider IDs before comparing local filenames."""
    return bool(
        session_id
        and len(session_id) <= 255
        and ".." not in session_id
        and all(char.isascii() and (char.isalnum() or char in "._-") for char in session_id)
    )


def _raw_provider_path(provider: str, session_id: str) -> str | None:
    """Find one exact local Claude/CodeX transcript without parsing its body.

    FTS is deliberately asynchronous. The board receives a hook row before
    that derived cache is refreshed, so exact filename identity is the safe
    narrow bridge for a just-created thread. Keep it built-in-only: external
    providers do not promise filename == session-id semantics.
    """
    if not _safe_session_id(session_id):
        return None
    if provider == "claude":
        root = claude_provider.SESSIONS_DIR

        def paths(_root: str):
            # Claude's history is an additional, local index of exact session
            # filenames.  It catches a project directory that a plain glob
            # misses, without accepting a path outside the approved root.
            return claude_provider.list_session_files()

        def path_id(path: str) -> str:
            return Path(path).stem

    elif provider == "codex":
        root = codex_provider.CODEX_SESSIONS_DIR

        def paths(root: str):
            return glob.iglob(os.path.join(root, "**", "*.jsonl"), recursive=True)

        path_id = codex_provider._session_id_from_path

    else:
        return None

    root = os.path.realpath(root)
    key = (provider, root)
    now = time.monotonic()
    with _raw_path_cache_lock:
        cached = _raw_path_cache.get(key)
        if cached is None or now >= cached[0]:
            candidates: dict[str, list[str]] = {}
            for path in paths(root):
                path = os.path.abspath(path)
                try:
                    under_root = os.path.commonpath((root, path)) == root
                except ValueError:
                    under_root = False
                if under_root:
                    candidate_id = path_id(path)
                    if _safe_session_id(candidate_id):
                        candidates.setdefault(candidate_id, []).append(path)
            cached = (
                now + _RAW_PATH_CACHE_TTL_S,
                {sid: tuple(sorted(values)) for sid, values in candidates.items()},
            )
            _raw_path_cache[key] = cached
        paths_for_id = cached[1].get(session_id, ())

    for candidate in paths_for_id:
        real_path = os.path.realpath(candidate)
        try:
            under_root = os.path.commonpath((root, real_path)) == root
        except ValueError:
            under_root = False
        if under_root and os.path.isfile(real_path) and path_id(real_path) == session_id:
            return real_path

        # A disappearing or replaced transcript invalidates only this exact
        # candidate. Other session IDs keep their shared discovery snapshot.
        with _raw_path_cache_lock:
            cached = _raw_path_cache.get(key)
            if cached is not None:
                candidates = cached[1]
                remaining = tuple(path for path in candidates.get(session_id, ()) if path != candidate)
                if remaining:
                    candidates[session_id] = remaining
                else:
                    candidates.pop(session_id, None)
    return None


def _valid_path(value: object) -> str | None:
    path = str(value or "")
    return path if path and os.path.isfile(path) else None


def _reserve_native_launch(session_id: str, provider: str) -> tuple[int | None, bool]:
    """Reserve a native session until the provider process exits.

    The descriptor is inheritable across ``exec``. A simultaneous launcher
    that cannot take the lock must fork instead of racing a second attach.
    """
    digest = hashlib.sha256(f"{provider}\0{session_id}".encode()).hexdigest()[:24]
    lock_dir = store._DB_PATH.parent / "launch-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_dir / f"{digest}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None, False
    os.set_inheritable(fd, True)
    return fd, True


def _agent_board_executable() -> str:
    installed = shutil.which("agent-board")
    if installed:
        return installed
    checkout_script = Path(__file__).resolve().parents[2] / "agent-board"
    return str(checkout_script) if checkout_script.is_file() else "agent-board"


def direct_session_command(
    session_id: str, target_provider: str, *, full_access: bool
) -> str:
    """Build the sole server-authorized Terminal command shape."""
    if target_provider not in {"claude", "codex"}:
        raise ValueError("provider must be claude or codex")
    argv = [
        _agent_board_executable(),
        "direct-session",
        str(session_id),
        target_provider,
        "true" if full_access else "false",
    ]
    return shlex.join(argv)


def _indexed_session(session_id: str) -> dict | None:
    try:
        conn = fts.open_db(read_only=True)
    except (OSError, sqlite3.Error):
        return None
    try:
        return fts.get_by_sid(conn, session_id)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def session_for_launch(
    session_id: str,
    indexed: dict | None | object = _UNSET,
    runtime: dict | None | object = _UNSET,
) -> dict | None:
    """Resolve a hook-backed session while FTS catches up with local files."""
    # Board polling has already read every runtime row. Preserve an explicit
    # ``None`` (a task whose runtime disappeared) rather than fetching it
    # again; direct callers retain the exact lazy lookup behavior.
    runtime = store.get(session_id) if runtime is _UNSET else runtime
    indexed = _indexed_session(session_id) if indexed is _UNSET else indexed
    if runtime is None and indexed is None:
        return None
    source_provider = store.provider_of(runtime) if runtime else str(
        (indexed or {}).get("provider") or store.DEFAULT_PROVIDER
    )
    indexed_provider = str((indexed or {}).get("provider") or store.DEFAULT_PROVIDER)
    # A coincidental ID in another provider's corpus must not lend this hook
    # row a transcript from a different conversation.
    session = dict(indexed or {}) if indexed_provider == source_provider else {}
    session["session_id"] = session_id
    session["provider"] = source_provider
    path = None
    if runtime:
        if runtime.get("cwd"):
            session["cwd"] = runtime["cwd"]
        if runtime.get("name") and runtime.get("name_source") == "manual":
            session["name"] = runtime["name"]
        path = _valid_path(runtime.get("transcript_path"))
    if path is None and indexed_provider == source_provider:
        path = _valid_path((indexed or {}).get("path"))
    if path is None:
        path = _raw_provider_path(source_provider, session_id)
    if path:
        session["path"] = path
        try:
            session["source_size"] = os.path.getsize(path)
        except OSError:
            session["source_size"] = int(session.get("source_size") or 0)
    else:
        session.pop("path", None)
    return session


def action_status(
    session: dict, target_provider: str, *, availability_check=None
) -> dict:
    """Return the truthful, scoped availability of one launch action."""
    source_provider = str(session.get("provider") or store.DEFAULT_PROVIDER)
    source_name = get_provider(source_provider).display_name
    target_spec = get_provider(target_provider)
    label = (
        f"Resume {source_name}"
        if target_provider == source_provider
        else f"Continue in {target_spec.display_name}"
    )
    cwd = str(session.get("cwd") or "")
    reason = None
    if not cwd or not os.path.isdir(cwd):
        reason = "Working directory is unavailable on this Mac."
    available = availability_check or (lambda _provider: target_spec.is_available())
    if reason is None and not available(target_provider):
        reason = f"{target_spec.display_name} is not installed on this Mac."
    elif reason is None and target_provider != source_provider:
        path = str(session.get("path") or "")
        if not path or not os.path.isfile(path):
            reason = "Thread transcript is unavailable for provider handoff."
    return {"label": label, "available": reason is None, "reason": reason}


def launch_direct_session(
    session_id: str, target_provider: str, *, full_access: bool
) -> None:
    """Run the picker-equivalent policy for one explicit session."""
    if target_provider not in {"claude", "codex"}:
        raise ValueError("provider must be claude or codex")
    session = session_for_launch(session_id)
    if session is None:
        raise ValueError(f"session not found: {session_id}")
    status = action_status(session, target_provider)
    if not status["available"]:
        raise ValueError(str(status["reason"]))

    cwd = str(session["cwd"])
    source_provider = str(session.get("provider") or store.DEFAULT_PROVIDER)
    os.chdir(cwd)

    # Import lazily to keep hooks and simple board commands cheap.
    from claude_browse import browse

    browse._open_in_target_provider(
        session,
        source_provider,
        target_provider,
        session_id,
        cwd,
        (),
        full_access,
        # Dormant sessions attach; _native_resume owns the reservation at the
        # exact point where it knows an attach (rather than a fork) will occur.
        fork=None,
    )


def open_in_terminal(command: str) -> None:
    """Open a server-generated command in a new macOS Terminal window."""
    if not command:
        raise ValueError("command is required")
    script = (
        "on run argv\n"
        'tell application "Terminal"\n'
        "activate\n"
        "do script item 1 of argv\n"
        "end tell\n"
        "end run"
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script, "--", command],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"could not open Terminal: {exc}") from exc
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or "could not open Terminal")
