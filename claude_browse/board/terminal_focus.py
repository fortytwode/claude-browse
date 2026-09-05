"""Safe focusing of an already-proven macOS Terminal tab."""

from __future__ import annotations

import subprocess

from . import presence

_FOCUS_SCRIPT = """on run argv
set wantedTTY to \"/dev/\" & item 1 of argv
tell application \"Terminal\"
    repeat with terminalWindow in windows
        repeat with terminalTab in tabs of terminalWindow
            if (tty of terminalTab as text) is wantedTTY then
                set selected tab of terminalWindow to terminalTab
                set index of terminalWindow to 1
                activate
                return \"focused\"
            end if
        end repeat
    end repeat
end tell
return \"not-found\"
end run"""


def focus_session(session_id: str, provider: str) -> dict[str, object]:
    """Focus the exact terminal tab for an open provider session, or no-op."""
    tty, reason = presence.verified_terminal_tty(session_id, provider)
    if not tty:
        return {"focused": False, "reason": reason}
    try:
        result = subprocess.run(  # noqa: S603 - fixed native inspection command
            ["osascript", "-e", _FOCUS_SCRIPT, "--", tty],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return {"focused": False, "reason": "Terminal could not be focused; no new window was opened."}
    if result.returncode == 0 and result.stdout.strip() == "focused":
        return {"focused": True, "reason": ""}
    return {"focused": False, "reason": "The verified Terminal tab disappeared before it could be focused."}
