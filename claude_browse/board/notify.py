"""macOS native notifications. Best-effort -- never raises to the caller."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _applescript_quote(s: str) -> str:
    """Escape for an AppleScript string literal.

    NOT json.dumps() -- AppleScript isn't JSON, and json.dumps() renders
    non-ASCII (including every emoji this module's callers use) as \\uXXXX
    escapes, which AppleScript's string syntax cannot parse. That produced a
    syntax error on every real call site here, silently swallowed by
    check=False -- verified by direct repro. Keep characters literal; only
    backslash and double-quote need escaping.
    """
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _notifications_disabled() -> bool:
    return os.environ.get("AGENT_BOARD_DISABLE_NOTIFICATIONS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _notifier_executable() -> Path:
    override = os.environ.get("AGENT_BOARD_NOTIFIER_EXECUTABLE")
    if override:
        return Path(override).expanduser()
    return (
        Path.home()
        / "Applications/Agent Board Notifier.app/Contents/MacOS/AgentBoardNotifier"
    )


def _launch_dedicated_notifier(title: str, message: str) -> bool:
    executable = _notifier_executable()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        return False
    try:
        subprocess.Popen(
            [str(executable), "--title", title, "--message", message],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return False
    return True


def notify(title: str, message: str) -> None:
    """Fire a native macOS notification with sound.

    Prefer the dedicated Agent Board app so macOS exposes an isolated
    Notification/Focus identity. Fall back to AppleScript when it is missing
    or cannot launch, keeping clone installs useful even without swiftc.
    """
    if _notifications_disabled():
        return

    if _launch_dedicated_notifier(title, message):
        return

    try:
        script = (
            f"display notification {_applescript_quote(message)} "
            f"with title {_applescript_quote(title)} "
            f'sound name "default"'
        )
        subprocess.run(
            ["osascript", "-e", script],
            timeout=5,
            capture_output=True,
            check=False,
        )
    except Exception:
        pass
