"""macOS native notifications. Best-effort -- never raises to the caller."""

from __future__ import annotations

import os
import subprocess


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


def notify(title: str, message: str) -> None:
    """Fire a native macOS notification with sound.

    `sound name "default"` plays the user's configured System Settings >
    Sound > Alert sound -- an audio cue matters here because the banner
    itself auto-dismisses after a few seconds by default (a visual-only
    notification is easy to miss if you're not looking at the screen right
    then). True persistence (the banner staying until manually dismissed)
    is a per-app Notification Center setting this code cannot set
    programmatically -- see README's Agent Board section for how to enable
    it for whichever app ends up registered as the notification sender.
    """
    if _notifications_disabled():
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
