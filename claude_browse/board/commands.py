"""Provider-aware, shell-safe commands displayed by Agent Board surfaces."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from claude_browse.providers import get_provider


def _in_project(argv: list[str], cwd: str | None) -> str:
    command = shlex.join(argv)
    return f"cd -- {shlex.quote(cwd)} && {command}" if cwd else command


def resume_command(
    session_id: str, provider: str | None, cwd: str | None, *, full_access: bool
) -> str:
    try:
        spec = get_provider(provider or "claude")
    except Exception:
        spec = get_provider("claude")
    return _in_project(spec.native_resume_cmd(session_id, yolo=full_access), cwd)


def start_command(
    task_id: str,
    prompt: str,
    provider: str | None,
    cwd: str | None,
    *,
    full_access: bool,
) -> str:
    try:
        spec = get_provider(provider or "claude")
    except Exception:
        spec = get_provider("claude")
    argv = spec.handoff_cmd(None, prompt, yolo=full_access)
    command = f"AGENT_BOARD_TASK_ID={shlex.quote(task_id)} {shlex.join(argv)}"
    return f"cd -- {shlex.quote(cwd)} && {command}" if cwd else command


def continue_command(
    session: dict,
    target_provider: str,
    *,
    full_access: bool,
    task_id: str | None = None,
) -> str:
    """Build a native resume or context-preserving cross-provider continuation."""
    source_provider = str(session.get("provider") or "claude")
    session_id = str(session.get("session_id") or session.get("sid") or "")
    cwd = str(session.get("cwd") or "")
    if target_provider == source_provider:
        return resume_command(session_id, source_provider, cwd, full_access=full_access)

    # Reuse claude-browse's established import format so starting this work
    # in the other agent preserves the conversation instead of sending only
    # a vague task title.
    from claude_browse.browse import write_import_file

    spec = get_provider(target_provider)
    import_path = write_import_file(session, target_provider, "")
    prompt = (
        f"Continue the imported {source_provider} session context from {import_path}. "
        "Treat it as prior conversation state, read that file first, prioritize "
        "the end-of-thread state and most recent turns, then continue the work."
    )
    argv = spec.handoff_cmd(str(Path(import_path).parent), prompt, full_access)
    prefix = f"AGENT_BOARD_TASK_ID={shlex.quote(task_id)} " if task_id else ""
    return _in_project(["sh", "-lc", prefix + shlex.join(argv)], cwd)


def open_in_terminal(command: str) -> None:
    """Open a server-generated command in a new macOS Terminal window."""
    if not command:
        raise ValueError("command is required")
    # Pass the command as argv, not interpolated AppleScript source. Task
    # titles can contain quotes/newlines; argv keeps those data, not code.
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
    except subprocess.SubprocessError as exc:
        raise OSError(f"could not open Terminal: {exc}") from exc
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or "could not open Terminal")
