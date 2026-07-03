"""Claude Code statusline renderer.

Local-read-only: never touches the network or calls the Haiku namer (naming
runs out-of-band in board/naming.py). Also doubles as the liveness heartbeat
-- Claude Code re-invokes this on a refreshInterval, so a live session keeps
heartbeat_at fresh; a killed one stops, which is what lets store.display_state
derive 'gone' everywhere else on the board.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from claude_browse.board import store

_RESET = "\033[0m"
COLORS = {
    "working": "\033[36m",      # cyan
    "idle": "\033[32m",         # green
    "needs-input": "\033[33m",  # yellow
    "gone": "\033[2m",          # dim
    "ended": "\033[2m",         # dim
}
ICON = "◇"  # ◇


def _basename(cwd: str | None) -> str:
    if not cwd:
        return "(unknown)"
    return Path(cwd).name or cwd


def render_line(session_id: str | None, cwd: str | None) -> str:
    row = store.get(session_id) if session_id else None
    if row is None:
        return f"{ICON} {_basename(cwd)}"

    state = store.display_state(row)
    name = row.get("name") or _basename(cwd)
    color = COLORS.get(state, "")
    return f"{ICON} {color}{name} · {state}{_RESET}"


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        session_id = payload.get("session_id")
        cwd = payload.get("cwd") or payload.get("workspace", {}).get("current_dir")

        if session_id:
            store.heartbeat(session_id)

        print(render_line(session_id, cwd))
    except Exception:
        print(f"{ICON}")


if __name__ == "__main__":
    main()
