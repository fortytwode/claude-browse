"""Safe focusing of an already-proven macOS Terminal tab."""

from __future__ import annotations

import subprocess
import threading

from . import presence, store, work_items

_title_locks: dict[str, threading.Lock] = {}
_title_locks_guard = threading.Lock()
MANAGED_TITLE_ENV = "AGENT_BOARD_MANAGED_TERMINAL_TITLE"

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

_TITLE_SCRIPT = """on run argv
set wantedTTY to \"/dev/\" & item 1 of argv
set wantedTitle to item 2 of argv
tell application \"Terminal\"
    repeat with terminalWindow in windows
        repeat with terminalTab in tabs of terminalWindow
            if (tty of terminalTab as text) is wantedTTY then
                set custom title of terminalTab to wantedTitle
                set title displays custom title of terminalTab to true
                if (custom title of terminalTab as text) is wantedTitle and (title displays custom title of terminalTab as boolean) then return \"updated\"
                return \"not-updated\"
            end if
        end repeat
    end repeat
end tell
return \"not-found\"
end run"""


def _terminal_title(value: str) -> str:
    """Return one bounded OSC-safe line for a Terminal tab title."""
    printable = "".join(char if char.isprintable() else " " for char in value)
    return " ".join(printable.split())[:200]


def _set_terminal_custom_title(tty: str, title: str) -> str:
    """Set and read back Terminal's persistent custom title for one exact TTY."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed native Terminal script
            ["osascript", "-e", _TITLE_SCRIPT, "--", tty, title],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return "error"
    if result.returncode != 0:
        return "error"
    return result.stdout.strip()


def set_session_title(session_id: str, provider: str, title: str) -> dict[str, object]:
    """Persist a title on the exact live Terminal tab, without shell input."""
    clean = _terminal_title(title)
    if not clean:
        return {"updated": False, "reason": "The title is empty after sanitization."}
    with _title_locks_guard:
        title_lock = _title_locks.setdefault(session_id, threading.Lock())
    with title_lock:
        current = store.get(session_id)
        if provider in {"claude", "codex"} and not bool(
            (current or {}).get("terminal_title_managed")
        ):
            return {
                "updated": False,
                "reason": (
                    "This terminal was opened before managed titles were enabled. "
                    "Reopen it through Agent Board to use its saved title."
                ),
            }
        tty, reason = presence.verified_terminal_tty(session_id, provider)
        if not tty:
            return {"updated": False, "reason": reason}
        requested = clean
        for _attempt in range(3):
            current = store.get(session_id)
            desired = _terminal_title(str((current or {}).get("name") or requested))
            result = _set_terminal_custom_title(tty, desired)
            if result == "not-found":
                return {
                    "updated": False,
                    "reason": "The verified Terminal tab disappeared before it could be retitled.",
                }
            if result != "updated":
                return {"updated": False, "reason": "The verified Terminal tab could not be retitled."}
            latest = store.get(session_id)
            latest_title = _terminal_title(str((latest or {}).get("name") or desired))
            if latest_title == desired:
                if desired != requested:
                    return {
                        "updated": False,
                        "reason": "A newer session title replaced this rename.",
                    }
                return {"updated": True, "reason": ""}
        return {
            "updated": False,
            "reason": "The session title changed repeatedly while Terminal was updating.",
        }


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


def main(argv: list[str] | None = None) -> int:
    import sys

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[1] not in work_items.PROVIDERS:
        return 2
    return 0 if focus_session(args[0], args[1])["focused"] else 1
