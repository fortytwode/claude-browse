"""Guarded, argv-safe Terminal launches for Agent Board surfaces."""

from __future__ import annotations

import os
import shlex
import shutil
import sqlite3
import subprocess
from pathlib import Path

from claude_browse import fts
from claude_browse.providers import get_provider

from . import store

_UNSET = object()


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
    session_id: str, indexed: dict | None | object = _UNSET
) -> dict | None:
    """Merge FTS context with hook truth, preferring the exact runtime fields."""
    runtime = store.get(session_id)
    indexed = _indexed_session(session_id) if indexed is _UNSET else indexed
    if runtime is None and indexed is None:
        return None
    session = dict(indexed or {})
    session["session_id"] = session_id
    if runtime:
        session["provider"] = store.provider_of(runtime)
        if runtime.get("cwd"):
            session["cwd"] = runtime["cwd"]
        if runtime.get("transcript_path"):
            session["path"] = runtime["transcript_path"]
    path = str(session.get("path") or "")
    if path:
        try:
            session["source_size"] = os.path.getsize(path)
        except OSError:
            session["source_size"] = int(session.get("source_size") or 0)
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
        # Dormant sessions attach natively; _native_resume still forks on an
        # active writer and takes the oversized-CodeX compact guard.
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
