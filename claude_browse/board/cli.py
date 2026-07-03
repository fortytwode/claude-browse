"""`agent-board board` / `jobs` -- one terminal glance at every active session
with a copy-paste resume command for each."""

from __future__ import annotations

import os

from claude_browse.board import store
from claude_browse.providers import get_provider

_SORT_ORDER = {"needs-input": 0, "working": 1, "idle": 2, "gone": 3, "ended": 4}
_ICON = {
    "needs-input": "⏸️",
    "working": "◇",
    "idle": "✓",
    "gone": "☠",
    "ended": "·",
}


def _resume_cmd(session_id: str) -> str:
    provider = get_provider("claude")
    return " ".join(provider.native_resume_cmd(session_id, yolo=False))


def render_board(max_age_hours: float = 24) -> str:
    rows = store.active(max_age_hours=max_age_hours)
    if not rows:
        return "no active sessions"

    enriched = []
    for row in rows:
        state = store.display_state(row)
        enriched.append((state, row))
    enriched.sort(key=lambda pair: _SORT_ORDER.get(pair[0], 5))

    lines = []
    for state, row in enriched:
        name = row.get("name") or os.path.basename(row.get("cwd") or "") or row["session_id"]
        host = row.get("host") or "?"
        cwd_base = os.path.basename(row.get("cwd") or "") or (row.get("cwd") or "")
        icon = _ICON.get(state, "?")
        resume = _resume_cmd(row["session_id"])
        lines.append(f"{icon} {name:<32} {state:<12} {host:<10} {cwd_base:<20} {resume}")

    return "\n".join(lines)


def main() -> None:
    print(render_board())


if __name__ == "__main__":
    main()
