"""`agent-board board` / `jobs` -- one terminal glance at every active session
with a copy-paste resume command for each."""

from __future__ import annotations

import os

from claude_browse.board import store
from claude_browse.providers import get_provider


def _resume_cmd(provider, session_id: str) -> str:
    # yolo=True: the board's resume commands are for quickly re-entering your
    # own sessions, so include each provider's skip-permissions flag
    # (--dangerously-skip-permissions for claude, the equivalent for codex)
    # for one-paste resumption. User-requested.
    return " ".join(provider.native_resume_cmd(session_id, yolo=True))


def render_board(max_age_hours: float = 24) -> str:
    rows = store.active(max_age_hours=max_age_hours)
    if not rows:
        return "no active sessions"

    provider = get_provider("claude")  # loop-invariant -- resolved once, not per row

    enriched = []
    for row in rows:
        state = store.display_state(row)
        enriched.append((state, row))
    enriched.sort(key=lambda pair: store.STATE_ORDER.get(pair[0], 5))

    lines = []
    for state, row in enriched:
        name = row.get("name") or os.path.basename(row.get("cwd") or "") or row["session_id"]
        host = row.get("host") or "?"
        cwd_base = os.path.basename(row.get("cwd") or "") or (row.get("cwd") or "")
        icon = store.STATE_ICON.get(state, "?")
        resume = _resume_cmd(provider, row["session_id"])
        lines.append(f"{icon} {name:<32} {state:<12} {host:<10} {cwd_base:<20} {resume}")

    return "\n".join(lines)


def main() -> None:
    print(render_board())


if __name__ == "__main__":
    main()
